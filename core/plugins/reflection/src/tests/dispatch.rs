//! `tests/test_reflection_dispatch.py`, one Rust test per Python test: reflection against fake
//! harness plugins, the contract seen from reflection's side. A parametrized Python test runs
//! each of its cases here.

use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};

use super::fake::*;
use crate::caller::Frame;
use crate::providers::Provider;
use crate::surface::specs;

async fn no_wake(_row: Value) -> Result<Value, String> {
    panic!("woke a waiter")
}

fn ok(v: &Value) -> bool {
    v["ok"] == json!(true)
}

fn refused(v: &Value) -> bool {
    v["ok"] == json!(false)
}

fn error(v: &Value) -> String {
    v["error"].as_str().unwrap_or_default().to_string()
}

fn reason(v: &Value) -> String {
    v["reason"].as_str().unwrap_or_default().to_string()
}

#[tokio::test]
async fn test_every_published_verb_has_a_handler_and_a_reflection_name() {
    let fx = Fx::new();
    let table: Value = serde_json::from_str(include_str!("../surface.json")).unwrap();
    for f in table["functions"].as_array().unwrap() {
        assert_eq!(f["tool"], json!(format!("reflection_{}", f["name"].as_str().unwrap())), "{f}");
    }
    for s in specs() {
        let out = fx.run(&s.verb, json!({})).await;
        assert!(!error(&out).starts_with("unknown verb"), "{}: {out}", s.verb);
    }
}

#[tokio::test]
async fn test_message_self_goes_to_the_harness_that_owns_the_caller() {
    let fx = Fx::new();
    let out = fx.run("message_self", json!({"_caller_pid": ME, "text": "/compact", "followup": "go on"})).await;
    assert!(ok(&out) && out["ticket"] == "t-1", "{out}");
    let (verb, args) = fx.cc().last();
    assert!(verb == "send" && args["pid"] == ME && args["submit"] == true, "{args}");
    assert!(fx.ds().verbs().iter().all(|v| v == "resolve"));
    let tickets = fx.app.open_tickets().await;
    assert_eq!(tickets.len(), 1);
    assert_eq!((tickets[0]["session_id"].as_str(), tickets[0]["tool"].as_str()), (Some(my_id().as_str()), Some("harness-cc")));
}

#[tokio::test]
async fn test_a_caller_no_harness_owns_is_refused() {
    let fx = Fx::new();
    let out = fx.run("compact", json!({"_caller_pid": 99})).await;
    assert!(refused(&out) && error(&out).contains("pid 99"), "{out}");
    assert!(!fx.cc().verbs().contains(&"compact".to_string()));
}

#[tokio::test]
async fn test_a_call_with_no_caller_pid_is_refused() {
    let fx = Fx::new();
    let out = fx.run("compact", json!({})).await;
    assert!(refused(&out) && error(&out).contains("could not tell which session"), "{out}");
}

#[tokio::test]
async fn test_a_name_two_harnesses_answer_to_is_refused() {
    let fx = Fx::new();
    fx.ds().set("d1", json!({"session_id": format!("ds{}", "b".repeat(32)), "name": "me"}));
    let out = fx.run("interrupt", json!({"_caller_pid": ME, "to": {"name": "me"}})).await;
    assert!(refused(&out) && error(&out).contains(&my_id()) && error(&out).contains(&"b".repeat(32)), "{out}");
}

#[tokio::test]
async fn test_a_call_from_a_peer_does_not_fan_out_again() {
    let fx = Fx::new();
    fx.set_peers(&["altair"]);
    let out = fx.run("message", json!({"to": {"name": "nobody"}, "text": "hi"})).await;
    assert!(error(&out).contains("no session on this node"), "{out}");
    assert!(fx.gw().peer_calls().is_empty(), "fanned out");
}

/// Only a session a harness recorded, by hand with no level, reads trusted (R103 item 2).
#[tokio::test]
async fn test_a_session_no_harness_knows_reads_untrusted() {
    let fx = Fx::new();
    assert_eq!(fx.app.level_of(Some(&my_id()), false, None).await, "trusted");
    fx.cc().answer("status", json!({"ok": false, "reason": "no such session"}));
    assert_eq!(fx.app.level_of(Some(&format!("cc{}", "9".repeat(32))), false, None).await, "untrusted");
    assert_eq!(fx.app.level_of(None, false, None).await, "trusted", "a caller that is no session");
}

#[tokio::test]
async fn test_start_other_passes_the_spec_through_and_records_trust() {
    let fx = Fx::new();
    let out = fx
        .run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x", "task_id": "t9", "role": "worker",
                                    "trust": "cooperative", "name": "seat:ana"}))
        .await;
    let starts = fx.cc().calls_of("start");
    assert_eq!(starts.len(), 1);
    assert!(starts[0]["task_id"] == "t9" && starts[0]["pid"] == ME, "{}", starts[0]);
    assert_eq!(out["trust"], "cooperative", "{out}");
    assert_eq!(fx.app.level_of(Some(&new_id()), false, None).await, "cooperative");
    assert_eq!(fx.trust_file().await[new_id()]["origin"], "seat:ana");
}

#[tokio::test]
async fn test_a_clear_event_records_the_old_session_as_the_parent() {
    let fx = Fx::new();
    let old = "cc0e380e380e380e380e380e380e380e38";
    let new = format!("cc{}", "2".repeat(32));
    let out = fx.run("harness_event", json!({"session_id": new, "kind": "clear", "parent": old})).await;
    assert_eq!(out, json!({"ok": true, "recorded": true}));
    assert_eq!(fx.links(), [(new.clone(), my_id(), "clear".to_string())]);
    assert!(refused(&fx.run("harness_event", json!({"session_id": new, "kind": "clear"})).await));
}

#[tokio::test]
async fn test_an_isolated_start_is_untrusted_unless_trust_is_named() {
    let fx = Fx::new();
    let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x", "isolated": true})).await;
    assert_eq!(fx.cc().calls_of("start")[0]["isolated"], true);
    assert_eq!(out["trust"], "untrusted");
}

#[tokio::test]
async fn test_a_start_above_the_callers_trust_is_refused() {
    let fx = Fx::new();
    fx.app.record_trust(&my_id(), "cooperative", "test", None).await;
    let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x", "trust": "trusted"})).await;
    assert!(refused(&out) && error(&out).contains("grant"), "{out}");
    assert!(!fx.cc().verbs().contains(&"start".to_string()));
}

#[tokio::test]
async fn test_a_steer_the_callers_trust_forbids_is_refused() {
    let fx = Fx::new();
    fx.cc().set("j2", peer_row());
    fx.deny_steers("no");
    let out = fx.run("interrupt", json!({"_caller_pid": ME, "to": {"id": other()}})).await;
    assert_eq!(out, json!({"ok": false, "error": "no"}));
    assert!(!fx.cc().verbs().contains(&"interrupt".to_string()));
}

#[tokio::test]
async fn test_a_trusted_sessions_stop_of_another_job_still_runs_the_check() {
    for verb in ["stop", "stop_other"] {
        let fx = Fx::new();
        fx.cc().set("j2", peer_row());
        fx.deny_steers("no");
        let out = fx.run(verb, json!({"_caller_pid": ME, "to": {"id": other()}, "confirm": true})).await;
        assert_eq!(out, json!({"ok": false, "error": "no"}), "{verb}");
        assert_eq!(fx.asked(), [("trusted".to_string(), verb.to_string(), Some("trusted".to_string()))]);
        assert!(!fx.cc().verbs().contains(&"stop".to_string()));
    }
}

/// A cleared or respawned conversation keeps its job, and so its level.
#[tokio::test]
async fn test_a_level_follows_the_job_to_a_new_session_id() {
    for level in ["cooperative", "untrusted"] {
        let fx = Fx::new();
        fx.cc().set("j2", peer_row());
        fx.app.record_trust(&format!("cc{}", "5".repeat(32)), level, "test", Some("j1")).await;
        assert_eq!(fx.run("whoami", json!({"_caller_pid": ME})).await["trust"], level);
        assert!(refused(&fx.run("stop", json!({"_caller_pid": ME, "to": {"id": other()}, "confirm": true})).await), "{level}");
        assert!(ok(&fx.run("stop", json!({"_caller_pid": ME, "to": {"id": my_id()}, "confirm": true})).await), "{level}");
    }
}

#[tokio::test]
async fn test_cd_claims_the_new_scope_and_releases_the_old() {
    let fx = Fx::new();
    *fx.gw().scope_fixed.lock().unwrap() = Some(json!({"ok": true, "claimed": true}));
    let out = fx.run("cd", json!({"_caller_pid": ME, "path": "/w/projects/awm/new"})).await;
    assert!(ok(&out) && out["claim"]["claimed"] == true, "{out}");
    let calls = fx.gw().calls_to("scope");
    assert_eq!(calls.iter().map(|c| c.0.as_str()).collect::<Vec<_>>(), ["claim", "release"]);
    assert!(calls[0].1["scope"] == "new" && calls[1].1["scope"] == "old");
    assert_eq!(fx.cc().last(), ("set".to_string(), json!({"pid": ME, "cwd": "/w/projects/awm/new"})));
}

#[tokio::test]
async fn test_a_background_session_does_not_move_into_a_held_scope() {
    let fx = Fx::new();
    *fx.gw().scope_fixed.lock().unwrap() = Some(json!({"ok": false, "claimed": false, "holder": "cc99"}));
    let out = fx.run("cd", json!({"_caller_pid": ME, "path": "/w/projects/awm/new"})).await;
    assert!(refused(&out) && reason(&out).contains("cc99"), "{out}");
    assert!(!fx.cc().verbs().contains(&"set".to_string()));
}

#[tokio::test]
async fn test_a_lost_event_raises_the_alarm_and_closes_its_ticket() {
    let fx = Fx::new();
    fx.run("compact", json!({"_caller_pid": ME, "followup": "go on"})).await;
    let out = fx.run("harness_event", json!({"session_id": my_id(), "ticket": "t-1", "kind": "lost", "reason": "worker swapped twice"})).await;
    assert!(ok(&out) && out["alarm"]["journal"] == true, "{out}");
    assert!(fx.app.open_tickets().await.is_empty());
    let lost = fx.app.recent_lost().await;
    assert_eq!(lost.len(), 1);
    assert!(lost[0]["reason"] == "worker swapped twice" && lost[0]["command"] == "/compact", "{}", lost[0]);
    let posts = fx.posts();
    assert_eq!(posts.len(), 1);
    assert_eq!((&posts[0]["project"], &posts[0]["scope"]), (&json!("awm"), &json!("redux-harness")));
    fx.app.settle().await;
}

#[tokio::test]
async fn test_an_event_that_beats_its_ticket_keeps_it_closed() {
    let fx = Fx::new();
    fx.run("harness_event", json!({"session_id": my_id(), "ticket": "t-1", "kind": "done"})).await;
    fx.app.settle().await;
    fx.run("compact", json!({"_caller_pid": ME, "followup": "go on"})).await;
    assert!(fx.app.open_tickets().await.is_empty());
}

#[tokio::test]
async fn test_the_sweep_alarms_only_for_a_ticket_its_plugin_dropped() {
    use crate::ledger::Ticket;
    let fx = Fx::new();
    for t in ["held", "dropped", "unasked"] {
        let tool = if t == "unasked" { "harness-ds" } else { "harness-cc" };
        let ticket = Ticket { ticket: t.into(), session_id: my_id(), tool: tool.into(), command: "/compact".into(), followup: "go on".into(), ..Ticket::default() };
        fx.app.open_ticket(&ticket).await;
    }
    let later = 10_i64.pow(13);
    let held = |tool: String| async move { (tool == "harness-cc").then(|| vec!["held".to_string()]) };
    assert_eq!(fx.app.sweep(held, later).await, ["dropped"]);
    let open: BTreeSet<String> = fx.app.open_tickets().await.iter().map(|t| t["ticket"].as_str().unwrap().to_string()).collect();
    assert_eq!(open, BTreeSet::from(["held".to_string(), "unasked".to_string()]));
    assert!(fx.app.recent_lost().await[0]["reason"].as_str().unwrap().contains("no longer holds"));
}

#[tokio::test]
async fn test_a_failure_is_logged_without_the_callers_text() {
    let logs = Captured::default();
    let _guard = logs.install();
    let fx = Fx::new();
    fx.cc().clear();
    fx.run("message_self", json!({"_caller_pid": ME, "text": "secret words"})).await;
    let text = logs.text();
    assert!(!text.contains("secret words"), "{text}");
    assert!(text.contains("message_self failed for caller pid 4242"), "{text}");
}

/// No provider is named in reflection: the table lists them, each says its code.
#[tokio::test]
async fn test_the_providers_come_from_the_live_contract_table() {
    let fx = Fx::new();
    *fx.gw().rows.lock().unwrap() = Some(Ok(vec![
        json!({"name": "harness-cc", "major": 1, "plugin": "cch", "peer": null}),
        json!({"name": "harness-ds", "major": 2, "plugin": "dsh", "peer": null}),
        json!({"name": "harness-x", "major": 1, "plugin": "x", "peer": "altair"}),
    ]));
    {
        let mut codes = fx.gw().codes.lock().unwrap();
        codes.remove("harness-ds");
        codes.insert("harness-x".into(), "ds".into());
    }
    assert_eq!(fx.app.providers().await, [Provider::new("harness-cc", Some("cc"), None), Provider::new("harness-x", Some("ds"), Some("altair"))]);
    assert_eq!(fx.gw().table_reads.load(std::sync::atomic::Ordering::SeqCst), 1);
    assert_eq!(crate::providers::CONTRACT, "harness@1");
}

// --- wait (sessions.md R51) -----------------------------------------------------

#[tokio::test]
async fn test_wait_records_the_wait_then_stops_the_caller_keeping_its_job() {
    let fx = Fx::new();
    let out = fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    assert!(ok(&out) && out["waiting"] == true, "{out}");
    assert_eq!(fx.cc().last(), ("stop".to_string(), json!({"session_id": my_id(), "pid": ME, "keep": true, "confirm": true})));
    let waits = fx.run("waits", json!({})).await["waits"].clone();
    assert_eq!(waits.as_array().unwrap().len(), 1);
    let row = &waits[0];
    assert_eq!((&row["session_id"], &row["until"], &row["job"]), (&json!(my_id()), &json!([other()]), &json!("j1")));
    assert_eq!((&row["project"], &row["scope"]), (&json!("awm"), &json!("redux-harness")));
}

#[tokio::test]
async fn test_a_wait_whose_stop_fails_is_not_left_behind() {
    let fx = Fx::new();
    fx.cc().answer("stop", json!({"ok": false, "reason": "claude stop failed"}));
    let out = fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    assert!(refused(&out));
    assert_eq!(fx.run("waits", json!({})).await["waits"], json!([]));
}

#[tokio::test]
async fn test_the_last_retire_resumes_the_waiter_with_the_wake_line() {
    let fx = Fx::new();
    fx.run("wait", json!({"_caller_pid": ME, "until": [other(), third()]})).await;
    fx.cc().set("w2", json!({"session_id": other(), "name": "w2", "job": "w2"}));
    fx.cc().answer("stop", json!({"ok": true, "session_id": other()}));
    assert_eq!(fx.run("stop", json!({"to": {"id": other()}})).await["woken"], json!([]));
    assert!(!fx.cc().verbs().contains(&"resume".to_string()));
    assert_eq!(fx.run("waits", json!({})).await["waits"][0]["retired"], json!([other()]));
    fx.cc().answer("stop", json!({"ok": true, "session_id": third()}));
    assert_eq!(fx.run("stop", json!({"to": {"id": third()}})).await["woken"], json!([my_id()]));
    let (verb, args) = fx.cc().last();
    assert!(verb == "resume" && args["session_id"] == my_id() && args["prompt"].as_str().unwrap().contains(&other()), "{args}");
    assert_eq!(fx.run("waits", json!({})).await["waits"], json!([]));
    fx.app.settle().await;
}

#[tokio::test]
async fn test_a_failed_wake_raises_an_alarm_and_keeps_the_wait() {
    let fx = Fx::new();
    fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    fx.cc().answer("resume", json!({"ok": false, "reason": "no transcript"}));
    assert_eq!(fx.run("retired", json!({"session_id": other()})).await["woken"], json!([]));
    assert!(fx.app.recent_lost().await[0]["reason"].as_str().unwrap().contains("no transcript"));
    assert_eq!(fx.run("waits", json!({})).await["waits"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn test_the_start_check_alarms_once_for_a_lost_session() {
    let fx = Fx::new();
    fx.run("wait", json!({"_caller_pid": ME, "until": [other(), third()]})).await;
    let (o, t) = (other(), third());
    let state = |sid: String| {
        let (o, t) = (o.clone(), t.clone());
        async move {
            if sid == o {
                Some("gone".to_string())
            } else if sid == t {
                Some("live".to_string())
            } else {
                None
            }
        }
    };
    let lost = fx.app.check_waits(state, |_ids| async { vec![] }, no_wake).await;
    assert_eq!(lost, [other()]);
    assert!(fx.app.recent_lost().await[0]["reason"].as_str().unwrap().contains(&other()));
    assert_eq!(fx.run("waits", json!({})).await["waits"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn test_the_start_check_counts_a_retire_it_missed() {
    let fx = Fx::new();
    fx.run("wait", json!({"_caller_pid": ME, "until": [other()]})).await;
    let woke = Arc::new(Mutex::new(vec![]));
    let w = woke.clone();
    let wake = move |row: Value| {
        w.lock().unwrap().push(row["session_id"].as_str().unwrap().to_string());
        async { Ok(json!({"ok": true})) }
    };
    let o = other();
    let state = |_sid: String| async { panic!("asked") };
    fx.app.check_waits(state, move |_ids| {
        let o = o.clone();
        async move { vec![o] }
    }, wake).await;
    assert_eq!(*woke.lock().unwrap(), [my_id()]);
    assert_eq!(fx.run("waits", json!({})).await["waits"], json!([]));
}

#[tokio::test]
async fn test_session_of_gives_the_gate_the_mode_and_the_trust() {
    let fx = Fx::new();
    let mut me = fx.cc().row("j1").unwrap();
    me["mode"] = json!("worker");
    fx.cc().set("j1", me);
    let out = fx.run("session_of", json!({"pid": ME})).await;
    assert!(out["known"] == true && out["session_id"] == json!(my_id()), "{out}");
    assert_eq!((&out["mode"], &out["trust"]), (&json!("worker"), &json!("trusted")));
}

/// Tony's own terminal sessions have no ledger entry: the gateway must not lock them out now
/// that a failed lookup is untrusted (fleet.md R103 item 2).
#[tokio::test]
async fn test_a_hand_started_session_reads_as_trusted_and_a_recorded_one_keeps_its_level() {
    let fx = Fx::new();
    assert_eq!(fx.run("session_of", json!({"pid": ME})).await["trust"], "trusted");
    fx.app.record_trust(&my_id(), "cooperative", "test", None).await;
    assert_eq!(fx.run("session_of", json!({"pid": ME})).await["trust"], "cooperative");
}

#[tokio::test]
async fn test_a_start_into_a_held_scope_is_refused_before_it_launches() {
    let fx = Fx::new();
    fx.gw().use_claims();
    fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x"})).await;
    let starts = fx.cc().calls_of("start").len();
    let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x", "name": "two"})).await;
    assert!(refused(&out) && reason(&out).contains(&new_id()), "{out}");
    assert_eq!(fx.cc().calls_of("start").len(), starts);
}

#[tokio::test]
async fn test_a_start_that_loses_the_claim_stops_the_new_session() {
    let fx = Fx::new();
    fx.gw().use_claims();
    fx.gw().with_claims(|c| {
        c.unlisted = true;
        c.hold("awm", "x", json!({"session_id": format!("cc{}", "9".repeat(32)), "pid": 1, "job": "other"}));
    });
    let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x"})).await;
    assert!(refused(&out) && out["stopped"] == "abcd1234", "{out}");
    let (verb, args) = fx.cc().last();
    assert!(verb == "stop" && args["session_id"] == json!(new_id()) && args["keep"] == false, "{args}");
}

#[tokio::test]
async fn test_a_stop_releases_the_sessions_claims() {
    for verb in ["stop", "stop_other"] {
        let fx = Fx::new();
        fx.gw().use_claims();
        fx.gw().with_claims(|c| {
            c.hold("awm", "x", json!({"session_id": my_id(), "pid": ME, "job": "j1"}));
            c.hold("awm", "y", json!({"session_id": format!("cc{}", "9".repeat(32)), "pid": 1, "job": "other"}));
        });
        fx.cc().answer("stop", json!({"ok": true, "job": "j1", "session_id": my_id()}));
        let out = fx.run(verb, json!({"_caller_pid": ME, "to": {"id": my_id()}, "confirm": true})).await;
        assert!(ok(&out), "{verb}: {out}");
        let held: Vec<(String, String)> = fx.gw().with_claims(|c| c.held.iter().map(|(k, _)| k.clone()).collect());
        assert_eq!(held, [("awm".to_string(), "y".to_string())], "{verb}");
        fx.app.settle().await;
    }
}

#[tokio::test]
async fn test_start_other_without_model_and_effort_is_refused_before_it_launches() {
    let fx = Fx::new();
    for missing in [json!({"effort": "high"}), json!({"model": "opus"}), json!({})] {
        let args = crate::app::merged(&json!({"_caller_pid": ME, "project": "awm", "scope": "x"}), &missing);
        let out = fx.app.handle("start_other", &Frame::from_args(&args, None, None), &args).await;
        assert!(refused(&out) && reason(&out).contains("model and effort are required"), "{missing}: {out}");
    }
}

#[tokio::test]
async fn test_effort_above_high_is_refused_on_start_and_on_the_effort_verb() {
    for level in ["xhigh", "max"] {
        let fx = Fx::new();
        let out = fx.run("start_other", json!({"_caller_pid": ME, "project": "awm", "scope": "x", "model": "opus", "effort": level})).await;
        assert!(refused(&out) && reason(&out).contains("upper limit"), "{level}: {out}");
        assert!(reason(&fx.run("effort", json!({"level": level})).await).ends_with("upper limit for any model."), "{level}");
    }
}
