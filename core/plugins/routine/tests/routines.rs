//! The registry, runs, the failure chain, conditions, the verbs and their timers.

mod common;

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use awm_plugin_routine::runner::RoutineError;
use awm_plugin_routine::service::{caller_label, functions, RoutineService};
use common::{fixture, git, project_repo, write_routine};
use serde_json::{json, Value};

const BASIC: &str = r#"
[routine]
name = "nightly"
description = "two steps"

[trigger]
timer = "*-*-* 03:00:00"

[[step]]
name = "one"
run = "echo one > out.txt"

[[step]]
name = "two"
run = "echo two >> out.txt"

[fixer]
peer = "alcor"
prompt = "Check the disk first."
"#;

fn failing() -> String {
    BASIC.replace(r#"run = "echo two >> out.txt""#, r#"run = "echo boom; exit 3""#)
}

fn ids(v: &Value, key: &str) -> Vec<String> {
    v[key].as_array().unwrap().iter().map(|s| s["step"].as_str().unwrap_or("").to_string()).collect()
}

// -- install --------------------------------------------------------------

#[test]
fn install_pins_commit() {
    let f = fixture();
    let repo = project_repo(f.tmp.path());
    let rec = f.runner.install("proj", &write_routine(&repo, "nightly", BASIC).to_string_lossy()).unwrap();
    assert_eq!(rec["name"], "nightly");
    assert_eq!(rec["root"], repo.to_string_lossy().as_ref());
    assert_eq!(rec["commit_sha"], git(&repo, &["rev-parse", "HEAD"]).as_str());
    assert_eq!(rec["scheduled"], false);
    assert_eq!(f.store.list_routines(Some("proj")).unwrap().len(), 1);
}

#[test]
fn install_refuses_uncommitted() {
    let f = fixture();
    let repo = project_repo(f.tmp.path());
    let p = write_routine(&repo, "nightly", BASIC);
    std::fs::write(&p, BASIC.replace("two", "three")).unwrap();
    let e = f.runner.install("proj", &p.to_string_lossy()).unwrap_err();
    assert!(matches!(e, RoutineError::Value(_)) && e.to_string().contains("uncommitted"), "{e}");
}

// -- runs -----------------------------------------------------------------

#[tokio::test]
async fn trigger_runs_steps_in_order() {
    let f = fixture();
    let repo = project_repo(f.tmp.path());
    f.runner.install("proj", &write_routine(&repo, "nightly", BASIC).to_string_lossy()).unwrap();
    let run = Value::Object(f.runner.trigger("proj", "nightly", "call", "job abc", true).await.unwrap());
    assert_eq!((run["state"].as_str(), run["caller"].as_str()), (Some("ok"), Some("job abc")));
    assert_eq!(ids(&run, "steps"), ["one", "two"]);
    assert_eq!(std::fs::read_to_string(repo.join("out.txt")).unwrap(), "one\ntwo\n");
    assert!(f.out.starts.lock().unwrap().is_empty());
}

#[tokio::test]
async fn one_live_run() {
    let f = fixture();
    let repo = project_repo(f.tmp.path());
    let body = BASIC.replace(r#"run = "echo one > out.txt""#, r#"run = "sleep 5""#);
    f.runner.install("proj", &write_routine(&repo, "nightly", &body).to_string_lossy()).unwrap();
    let first = f.runner.trigger("proj", "nightly", "call", "", false).await.unwrap();
    assert!(matches!(f.runner.trigger("proj", "nightly", "call", "", false).await, Err(RoutineError::LiveRunExists(_))));
    let t0 = Instant::now();
    let res = f.runner.cancel("proj", "nightly").await.unwrap();
    assert!(t0.elapsed() < Duration::from_secs(4));
    assert_eq!((res["cancelled"].as_bool(), res["run"]["state"].as_str()), (Some(true), Some("cancelled")));
    assert_eq!(f.store.get_run(first["id"].as_str().unwrap()).unwrap().unwrap()["state"], "cancelled");
    // The guard clears once the run ends.
    let second = f.runner.trigger("proj", "nightly", "call", "", false).await.unwrap();
    f.runner.cancel("proj", "nightly").await.unwrap();
    assert_ne!(second["id"], first["id"]);
    assert_eq!(f.runner.cancel("proj", "nightly").await.unwrap(), json!({"cancelled": false, "reason": "no live run"}));
}

async fn child_pid(pidfile: &std::path::Path) -> i32 {
    for _ in 0..100 {
        if let Ok(t) = std::fs::read_to_string(pidfile) {
            if let Ok(p) = t.trim().parse() {
                return p;
            }
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("the step never wrote its child's pid");
}

fn gone(pid: i32) -> bool {
    match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Err(_) => true,
        Ok(stat) => stat.rsplit_once(')').and_then(|(_, rest)| rest.split_whitespace().next()) == Some("Z"),
    }
}

#[tokio::test]
async fn cancel_kills_children() {
    let f = fixture();
    let repo = project_repo(f.tmp.path());
    let pidfile = f.tmp.path().join("child.pid");
    let body = BASIC.replace(r#"run = "echo one > out.txt""#, &format!(r#"run = "sleep 30 & echo $! > {}; wait""#, pidfile.display()));
    f.runner.install("proj", &write_routine(&repo, "nightly", &body).to_string_lossy()).unwrap();
    f.runner.trigger("proj", "nightly", "call", "", false).await.unwrap();
    let child = child_pid(&pidfile).await;
    f.runner.cancel("proj", "nightly").await.unwrap();
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(gone(child));
}

#[tokio::test]
async fn shutdown_kills_each_step_and_fails_the_run() {
    let f = fixture();
    let repo = project_repo(f.tmp.path());
    let pidfile = f.tmp.path().join("child.pid");
    let body = BASIC.replace(r#"run = "echo one > out.txt""#, &format!(r#"run = "sleep 30 & echo $! > {}; wait""#, pidfile.display()));
    f.runner.install("proj", &write_routine(&repo, "nightly", &body).to_string_lossy()).unwrap();
    let run = f.runner.trigger("proj", "nightly", "call", "", false).await.unwrap();
    let child = child_pid(&pidfile).await;
    let t0 = Instant::now();
    f.runner.shutdown().await;
    assert!(t0.elapsed() < Duration::from_secs(6));
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(gone(child));
    let after = f.store.get_run(run["id"].as_str().unwrap()).unwrap().unwrap();
    assert_eq!((after["state"].as_str(), after["failed_step"].as_str()), (Some("failed"), Some("one")));
    assert_eq!(after["error"], "the routine service stopped mid-run");
    assert!(f.out.starts.lock().unwrap().is_empty(), "a shutdown starts no fixer");
}

#[tokio::test]
async fn timer_only_refuses_call() {
    let f = fixture();
    let repo = project_repo(f.tmp.path());
    let body = BASIC.replace(r#"timer = "*-*-* 03:00:00""#, "timer = \"*-*-* 03:00:00\"\ncall = false");
    f.runner.install("proj", &write_routine(&repo, "nightly", &body).to_string_lossy()).unwrap();
    let e = f.runner.trigger("proj", "nightly", "call", "", false).await.unwrap_err();
    assert!(e.to_string().contains("timer only"));
    assert_eq!(f.runner.trigger("proj", "nightly", "timer", "", true).await.unwrap()["state"], "ok");
}

// -- the failure chain (R47, R48) ---------------------------------------------

#[tokio::test]
async fn failure_starts_fixer_on_declared_peer() {
    let f = fixture();
    let repo = project_repo(f.tmp.path());
    f.runner.install("proj", &write_routine(&repo, "nightly", &failing()).to_string_lossy()).unwrap();
    let run = Value::Object(f.runner.trigger("proj", "nightly", "call", "job abc", true).await.unwrap());
    assert_eq!((run["state"].as_str(), run["failed_step"].as_str()), (Some("failed"), Some("two")));
    assert!(run["error"].as_str().unwrap().contains("boom"));
    let starts = f.out.starts.lock().unwrap();
    let [start] = starts.as_slice() else { panic!("{starts:?}") };
    assert_eq!((start.peer.as_str(), start.project.as_str(), start.scope.as_str()), ("alcor", "proj", "fix-nightly"));
    let rid = run["id"].as_str().unwrap();
    assert!(start.started_by.contains(rid) && start.started_by.contains("job abc"));
    assert!(start.prompt.contains("boom") && start.prompt.contains("Check the disk first."));
    assert_eq!(run["dispatched"], json!([{"for": "two", "role": "fixer", "via": "agent", "job": "job1", "peer": "alcor", "errors": []}]));
    assert!(f.out.dms.lock().unwrap().is_empty());
}

#[tokio::test]
async fn failure_falls_back_to_a_notice() {
    let f = fixture();
    *f.out.fail_start.lock().unwrap() = true;
    let repo = project_repo(f.tmp.path());
    f.runner.install("proj", &write_routine(&repo, "nightly", &failing()).to_string_lossy()).unwrap();
    let run = f.runner.trigger("proj", "nightly", "call", "", true).await.unwrap();
    let d = &run["dispatched"][0];
    assert_eq!(d["via"], "notice");
    assert!(d["errors"][0].as_str().unwrap().contains("peer unreachable"));
    let dms = f.out.dms.lock().unwrap();
    assert_eq!(dms.len(), 1);
    assert!(dms[0].contains("failed at two"));
}

#[tokio::test]
async fn failure_falls_back_to_log() {
    let f = fixture();
    *f.out.fail_start.lock().unwrap() = true;
    *f.out.fail_dm.lock().unwrap() = true;
    let repo = project_repo(f.tmp.path());
    f.runner.install("proj", &write_routine(&repo, "nightly", &failing()).to_string_lossy()).unwrap();
    let run = f.runner.trigger("proj", "nightly", "call", "", true).await.unwrap();
    let d = &run["dispatched"][0];
    assert_eq!((d["via"].as_str(), d["errors"].as_array().unwrap().len()), (Some("log"), 2));
}

#[tokio::test]
async fn a_routine_without_a_fixer_posts_a_notice() {
    let f = fixture();
    let body = failing();
    let body = &body[..body.find("[fixer]").unwrap()];
    let repo = project_repo(f.tmp.path());
    f.runner.install("proj", &write_routine(&repo, "nightly", body).to_string_lossy()).unwrap();
    let run = f.runner.trigger("proj", "nightly", "call", "", true).await.unwrap();
    assert!(f.out.starts.lock().unwrap().is_empty());
    assert_eq!(run["dispatched"], json!([{"for": "two", "role": "notice", "via": "notice", "job": "", "peer": "", "errors": []}]));
    let dms = f.out.dms.lock().unwrap();
    assert!(dms.len() == 1 && dms[0].contains("failed at two") && dms[0].contains("boom"));
}

#[tokio::test]
async fn step_timeout_is_a_failure() {
    let f = fixture();
    let body = BASIC.replace(r#"run = "echo one > out.txt""#, "run = \"sleep 10\"\ntimeout = 0.3");
    let repo = project_repo(f.tmp.path());
    f.runner.install("proj", &write_routine(&repo, "nightly", &body).to_string_lossy()).unwrap();
    let run = f.runner.trigger("proj", "nightly", "call", "", true).await.unwrap();
    assert_eq!((run["state"].as_str(), run["failed_step"].as_str()), (Some("failed"), Some("one")));
    assert!(run["error"].as_str().unwrap().starts_with("timed out after 0.3s"));
    assert_eq!(run["steps"][0]["timed_out"], true);
    assert_eq!(f.out.starts.lock().unwrap().len(), 1);
}

// -- conditions (R34) -----------------------------------------------------

const CONDITIONS: &str = r#"
[routine]
name = "watch"

[[step]]
name = "holds"
kind = "condition"
run = "echo disk 95%; exit 0"
prompt = "Free some disk."

[[step]]
name = "quiet"
kind = "condition"
run = "exit 1"
prompt = "Never sent."

[agent]
peer = "alcor"
scope = "disk-care"
"#;

#[tokio::test]
async fn condition_dispatches_agent() {
    let f = fixture();
    let repo = project_repo(f.tmp.path());
    f.runner.install("proj", &write_routine(&repo, "watch", CONDITIONS).to_string_lossy()).unwrap();
    let run = f.runner.trigger("proj", "watch", "call", "", true).await.unwrap();
    assert_eq!(run["state"], "ok");
    let held: Vec<Value> = run["steps"].as_array().unwrap().iter().map(|s| s["held"].clone()).collect();
    assert_eq!(held, [json!(true), json!(false)]);
    let starts = f.out.starts.lock().unwrap();
    let [start] = starts.as_slice() else { panic!() };
    assert_eq!((start.peer.as_str(), start.scope.as_str()), ("alcor", "disk-care"));
    assert!(start.prompt.contains("Free some disk.") && start.prompt.contains("disk 95%"));
    assert_eq!(run["dispatched"][0]["role"], "agent");
}

#[tokio::test]
async fn condition_error_code_is_failure() {
    let f = fixture();
    let repo = project_repo(f.tmp.path());
    f.runner.install("proj", &write_routine(&repo, "watch", &CONDITIONS.replace("exit 1", "exit 7")).to_string_lossy()).unwrap();
    let run = f.runner.trigger("proj", "watch", "call", "", true).await.unwrap();
    assert_eq!((run["state"].as_str(), run["failed_step"].as_str()), (Some("failed"), Some("quiet")));
    let roles: Vec<&str> = run["dispatched"].as_array().unwrap().iter().map(|d| d["role"].as_str().unwrap()).collect();
    assert_eq!(roles, ["agent", "notice"]);
}

#[test]
fn orphaned_runs_close_on_restart() {
    let f = fixture();
    f.store.put_routine("p", "r", "/", "/x", "", &json!({"name": "r"})).unwrap();
    let run = f.store.start_run("p", "r", "call", "").unwrap();
    assert_eq!(f.store.orphan_live().unwrap(), 1);
    assert_eq!(f.store.get_run(run["id"].as_str().unwrap()).unwrap().unwrap()["state"], "failed");
    f.store.start_run("p", "r", "call", "").unwrap();
}

// -- the verbs ----------------------------------------------------------------

const TICK: &str = "\n[routine]\nname = \"tick\"\n[trigger]\ntimer = \"hourly\"\n[[step]]\nrun = \"true\"\n";

fn service(f: &common::Fixture) -> (RoutineService, Arc<Mutex<Vec<String>>>) {
    let calls = Arc::new(Mutex::new(Vec::<String>::new()));
    let rec = calls.clone();
    let svc = RoutineService {
        runner: f.runner.clone(),
        unit_dir: f.tmp.path().join("units"),
        systemctl: Box::new(move |a: &[&str]| {
            rec.lock().unwrap().push(a.join(" "));
            Ok(())
        }),
    };
    (svc, calls)
}

#[test]
fn every_verb_publishes_as_a_routine_tool() {
    let names: Vec<&str> = functions().iter().map(|(n, ..)| *n).collect();
    assert_eq!(names, ["install", "uninstall", "list", "trigger", "cancel", "schedule", "unschedule", "runs"]);
    assert!(!names.contains(&"deploy"), "the retired deploy stub goes");
}

#[test]
fn the_caller_is_the_principal_then_the_session_then_the_pid() {
    assert_eq!(caller_label(Some("peer:altair"), Some("s1"), Some(7)), "peer:altair");
    assert_eq!(caller_label(None, Some("s1"), Some(7)), "session s1");
    assert_eq!(caller_label(None, None, Some(42)), "pid 42");
    assert_eq!(caller_label(None, None, None), "");
}

#[tokio::test]
async fn lifecycle_through_handlers() {
    let f = fixture();
    let (svc, calls) = service(&f);
    let repo = project_repo(f.tmp.path());
    let path = write_routine(&repo, "tick", TICK);
    svc.install(&json!({"project": "proj", "path": path})).unwrap();
    let res = svc.schedule(&json!({"project": "proj", "name": "tick"})).unwrap();
    assert_eq!((res["jitter_s"].as_i64(), res["catchup_s"].is_null()), (Some(30), true));
    assert!(f.tmp.path().join("units/awm-routine-proj-tick.timer").exists());
    let run = svc.trigger(&json!({"project": "proj", "name": "tick", "wait": true}), "pid 7".into()).await.unwrap();
    assert_eq!((run["state"].as_str(), run["caller"].as_str()), (Some("ok"), Some("pid 7")));
    let listed = svc.list(&json!({})).unwrap();
    let r = &listed["routines"][0];
    assert_eq!((r["scheduled"].as_bool(), r["last_run"]["state"].as_str()), (Some(true), Some("ok")));
    assert_eq!(svc.runs(&json!({"id": run["id"]})).unwrap()["id"], run["id"]);
    assert_eq!(svc.runs(&json!({"project": "proj"})).unwrap()["runs"].as_array().unwrap().len(), 1);
    assert_eq!(svc.uninstall(&json!({"project": "proj", "name": "tick"})).unwrap(), json!({"removed": true}));
    assert!(!f.tmp.path().join("units/awm-routine-proj-tick.timer").exists());
    assert!(calls.lock().unwrap().contains(&"disable --now awm-routine-proj-tick.timer".to_string()));
    assert_eq!(f.store.timer("proj", "tick").unwrap(), None, "uninstall drops the timer slot");
}

#[tokio::test]
async fn bad_trigger_kind() {
    let f = fixture();
    let (svc, _) = service(&f);
    let e = svc.trigger(&json!({"project": "p", "name": "n", "trigger": "cron"}), String::new()).await.unwrap_err();
    assert_eq!((e.error_class.as_str(), e.message.as_str()), ("ValueError", "trigger must be `call` or `timer`"));
    let e = svc.runs(&json!({"id": "nope"})).unwrap_err();
    assert_eq!(e.error_class, "KeyError");
}

#[test]
fn schedule_takes_jitter_and_catchup() {
    let f = fixture();
    let (svc, _) = service(&f);
    let repo = project_repo(f.tmp.path());
    svc.install(&json!({"project": "proj", "path": write_routine(&repo, "tick", TICK)})).unwrap();
    svc.schedule(&json!({"project": "proj", "name": "tick", "timer": "daily", "jitter_s": 60, "catchup_s": 7200})).unwrap();
    let timer = std::fs::read_to_string(f.tmp.path().join("units/awm-routine-proj-tick.timer")).unwrap();
    assert!(timer.contains("OnCalendar=daily\nPersistent=true\nRandomizedDelaySec=60\n"), "{timer}");
    let slot = f.store.timer("proj", "tick").unwrap().unwrap();
    assert_eq!((slot.on_calendar.as_str(), slot.jitter_s, slot.catchup_s), ("daily", 60, Some(7200)));
    let e = svc.schedule(&json!({"project": "proj", "name": "tick", "jitter_s": 60, "catchup_s": 30})).unwrap_err();
    assert!(e.message.contains("catchup_s"), "{e}");
    // A reschedule without catchup_s clears it, and keeps the last slot served.
    f.store.set_last_slot("proj", "tick", "2026-11-01T07:00:00+00:00").unwrap();
    svc.schedule(&json!({"project": "proj", "name": "tick"})).unwrap();
    let slot = f.store.timer("proj", "tick").unwrap().unwrap();
    assert_eq!((slot.on_calendar.as_str(), slot.jitter_s, slot.catchup_s, slot.last_slot.as_str()), ("hourly", 30, None, "2026-11-01T07:00:00+00:00"));
    // A seed from another clock moves the slot forward, never back.
    svc.schedule(&json!({"project": "proj", "name": "tick", "last_slot": "2026-11-02T04:00:00.0076+00:00"})).unwrap();
    assert_eq!(f.store.timer("proj", "tick").unwrap().unwrap().last_slot, "2026-11-02T04:00:00+00:00");
    svc.schedule(&json!({"project": "proj", "name": "tick", "last_slot": "2026-11-01T21:00:00-07:00"})).unwrap();
    assert_eq!(f.store.timer("proj", "tick").unwrap().unwrap().last_slot, "2026-11-02T04:00:00+00:00");
    assert!(svc.schedule(&json!({"project": "proj", "name": "tick", "last_slot": "yesterday"})).unwrap_err().message.contains("last_slot"));
}

fn has_base_time() -> bool {
    awm_plugin_routine::timers::next_elapse("*-*-* *:*:00", chrono::Utc::now()).is_some()
}

#[tokio::test]
async fn a_late_fire_past_catchup_is_skipped() {
    if !has_base_time() {
        eprintln!("no systemd-analyze --base-time here: skipped");
        return;
    }
    let f = fixture();
    let (svc, _) = service(&f);
    let repo = project_repo(f.tmp.path());
    svc.install(&json!({"project": "proj", "path": write_routine(&repo, "tick", TICK)})).unwrap();
    // A slot two hours ago, with one hour of catch-up: the fire comes too late.
    let slot = chrono::Utc::now() - chrono::Duration::hours(2);
    let spec = format!("*-*-* {} UTC", slot.format("%H:%M:00"));
    f.store.put_timer("proj", "tick", &spec, 0, Some(3600)).unwrap();
    let run = svc.trigger(&json!({"project": "proj", "name": "tick", "trigger": "timer"}), String::new()).await.unwrap();
    assert_eq!((run["state"].as_str(), run["trigger"].as_str()), (Some("skipped"), Some("timer")));
    assert!(run["error"].as_str().unwrap().contains("later than catchup_s"), "{run}");
    assert!(run["ended_at"].as_str().is_some_and(|e| !e.is_empty()));
    // A call is no timer fire: it runs.
    assert_eq!(svc.trigger(&json!({"project": "proj", "name": "tick", "wait": true}), String::new()).await.unwrap()["state"], "ok");
}

#[tokio::test]
async fn a_fire_within_catchup_runs_once_per_slot() {
    if !has_base_time() {
        eprintln!("no systemd-analyze --base-time here: skipped");
        return;
    }
    let f = fixture();
    let (svc, _) = service(&f);
    let repo = project_repo(f.tmp.path());
    svc.install(&json!({"project": "proj", "path": write_routine(&repo, "tick", TICK)})).unwrap();
    let slot = chrono::Utc::now() - chrono::Duration::minutes(10);
    let spec = format!("*-*-* {} UTC", slot.format("%H:%M:00"));
    f.store.put_timer("proj", "tick", &spec, 0, Some(3600)).unwrap();
    let args = json!({"project": "proj", "name": "tick", "trigger": "timer", "wait": true});
    assert_eq!(svc.trigger(&args, String::new()).await.unwrap()["state"], "ok");
    let served = f.store.timer("proj", "tick").unwrap().unwrap().last_slot;
    assert!(served.ends_with(&slot.format("%H:%M:00+00:00").to_string()), "{served}");
    // A second fire for the same slot, such as a Persistent catch-up after a reschedule.
    let again = svc.trigger(&args, String::new()).await.unwrap();
    assert_eq!(again["state"], "skipped");
    assert!(again["error"].as_str().unwrap().contains("already ran"));
}
