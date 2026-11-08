//! `run_job`, the one path every submission takes. The insert is the acquire, so two callers
//! in different processes get the same answer without a check-then-act window.

mod common;

use std::collections::HashSet;
use std::sync::Mutex;

use awm_plugin_dvc::jobs::{self, JobError, Submitter, CACHE_SYNC, JOBS, WORKSPACE_BACKUP};
use awm_plugin_dvc::{globus, runs};
use serde_json::{json, Value};

/// Both jobs replaced by a recording stub. Nothing touches Globus.
#[derive(Default)]
struct Stub {
    calls: Mutex<Vec<(String, bool)>>,
    seen_submitting: Mutex<Vec<HashSet<String>>>,
}

impl Submitter for Stub {
    fn submit(&self, job: &str, dry_run: bool) -> anyhow::Result<Value> {
        let mut calls = self.calls.lock().unwrap();
        calls.push((job.into(), dry_run));
        self.seen_submitting.lock().unwrap().push(jobs::submitting());
        if dry_run {
            return Ok(json!({"dry_run": true, "transfer_items": [{}]}));
        }
        // A fresh id per call, as Globus mints one per submission.
        Ok(json!({"submitted": true, "task_id": format!("task-{}", calls.len())}))
    }
}

struct Boom;

impl Submitter for Boom {
    fn submit(&self, _: &str, _: bool) -> anyhow::Result<Value> {
        anyhow::bail!("globus: not logged in")
    }
}

fn run(w: &common::Ws, dao: &runs::RunsDao, sub: &dyn Submitter, job: &str, trigger: &str, force: bool) -> Value {
    jobs::run_job(&w.paths, dao, sub, job, trigger, force, false).unwrap()
}

#[test]
fn a_submission_is_recorded_as_a_run() {
    let w = common::workspace();
    let dao = w.dao();
    let stub = Stub::default();

    let result = run(&w, &dao, &stub, CACHE_SYNC, "schedule", false);

    let row = dao.get_run(result["run_id"].as_str().unwrap()).unwrap().unwrap();
    assert_eq!(result["task_id"], "task-1");
    assert_eq!((row["status"].as_str(), row["task_id"].as_str(), row["trigger"].as_str()), (Some("running"), Some("task-1"), Some("schedule")));
    assert_eq!(row["label"], jobs::spec_for(CACHE_SYNC).unwrap().label);
}

#[test]
fn a_second_caller_is_declined_with_the_task_it_would_have_duplicated() {
    let w = common::workspace();
    let dao = w.dao();
    let stub = Stub::default();
    run(&w, &dao, &stub, CACHE_SYNC, "manual", false);

    let second = run(&w, &dao, &stub, CACHE_SYNC, "manual", false);

    assert_eq!(second["submitted"], false);
    assert_eq!(second["in_flight"], "task-1");
    assert!(second["note"].as_str().unwrap().contains("force=true"));
    assert_eq!(stub.calls.lock().unwrap().len(), 1);
}

#[test]
fn a_declined_run_still_leaves_a_row() {
    let w = common::workspace();
    let dao = w.dao();
    let stub = Stub::default();
    run(&w, &dao, &stub, CACHE_SYNC, "manual", false);
    run(&w, &dao, &stub, CACHE_SYNC, "schedule", false);

    let skipped = dao.history(Some(CACHE_SYNC), 20, Some("skipped"), None).unwrap();

    assert_eq!(skipped.len(), 1);
    assert!(skipped[0]["note"].as_str().unwrap().contains("still running"));
}

#[test]
fn force_supersedes_the_live_row_and_submits() {
    let w = common::workspace();
    let dao = w.dao();
    let stub = Stub::default();
    let first = run(&w, &dao, &stub, CACHE_SYNC, "manual", false);

    let second = run(&w, &dao, &stub, CACHE_SYNC, "manual", true);

    assert_eq!(second["submitted"], true);
    assert_eq!(dao.get_run(first["run_id"].as_str().unwrap()).unwrap().unwrap()["status"], "skipped");
    assert_eq!(stub.calls.lock().unwrap().len(), 2);
}

#[test]
fn the_two_jobs_do_not_block_each_other() {
    let w = common::workspace();
    let dao = w.dao();
    let stub = Stub::default();
    assert_eq!(run(&w, &dao, &stub, CACHE_SYNC, "manual", false)["submitted"], true);
    assert_eq!(run(&w, &dao, &stub, WORKSPACE_BACKUP, "manual", false)["submitted"], true);
}

#[test]
fn a_dry_run_records_nothing_and_holds_no_slot() {
    let w = common::workspace();
    let dao = w.dao();
    let stub = Stub::default();

    let result = jobs::run_job(&w.paths, &dao, &stub, WORKSPACE_BACKUP, "manual", false, true).unwrap();

    assert_eq!(result["dry_run"], true);
    assert_eq!(result["job"], WORKSPACE_BACKUP);
    assert!(dao.history(Some(WORKSPACE_BACKUP), 20, None, None).unwrap().is_empty());
    assert!(dao.begin_run(WORKSPACE_BACKUP, "manual", "").unwrap().is_some());
}

#[test]
fn a_submit_that_raises_leaves_an_error_row_and_frees_the_slot() {
    let w = common::workspace();
    let dao = w.dao();

    let err = jobs::run_job(&w.paths, &dao, &Boom, CACHE_SYNC, "manual", false, false).unwrap_err();
    assert!(matches!(err, JobError::Submit("RuntimeError", _)), "{err:?}");

    let row = &dao.history(Some(CACHE_SYNC), 20, None, None).unwrap()[0];
    assert_eq!(row["status"], "error");
    assert!(row["error"].as_str().unwrap().contains("not logged in"));
    assert!(dao.begin_run(CACHE_SYNC, "manual", "").unwrap().is_some());
}

#[test]
fn an_unknown_job_is_rejected_by_name() {
    let w = common::workspace();
    let dao = w.dao();
    let err = jobs::run_job(&w.paths, &dao, &Stub::default(), "nope", "manual", false, false).unwrap_err();
    assert!(matches!(&err, JobError::Unknown(m) if m.contains("unknown job")), "{err:?}");
}

#[test]
fn record_status_leaves_a_running_task_live_for_the_adopt_sweep() {
    let w = common::workspace();
    let dao = w.dao();
    let run = dao.begin_run(CACHE_SYNC, "manual", "").unwrap().unwrap();
    let rid = run["id"].as_str().unwrap();
    dao.mark_submitted(rid, "task-7", "").unwrap();

    let status = jobs::record_status(&dao, rid, &json!({"status": "ACTIVE", "done": false, "files_transferred": 4})).unwrap();

    assert_eq!(status, "running");
    assert_eq!(dao.get_run(rid).unwrap().unwrap()["status"], "running");
    assert_eq!(dao.live_run(CACHE_SYNC).unwrap().unwrap()["id"], rid);
}

#[test]
fn seeding_creates_a_row_for_every_job_we_know_about() {
    let w = common::workspace();
    let dao = w.dao();
    jobs::ensure_seeded(&w.paths, &dao).unwrap();

    let rows = dao.list_jobs().unwrap();
    let names: HashSet<&str> = rows.iter().map(|r| r["name"].as_str().unwrap()).collect();
    assert_eq!(names, JOBS.iter().map(|j| j.name).collect::<HashSet<_>>());
    assert!(rows.iter().all(|r| !r["cron"].as_str().unwrap().is_empty()));
}

#[test]
fn a_sync_in_flight_at_cutover_is_migrated_into_the_run_table() {
    let w = common::workspace();
    let dao = w.dao();
    let state = w.paths.state_dir().join("last_sync.json");
    std::fs::create_dir_all(state.parent().unwrap()).unwrap();
    std::fs::write(&state, r#"{"task_id": "old-task"}"#).unwrap();

    jobs::ensure_seeded(&w.paths, &dao).unwrap();

    let row = dao.live_run(CACHE_SYNC).unwrap().unwrap();
    assert_eq!(row["task_id"], "old-task");
    assert_eq!(row["trigger"], "adopted");
    assert!(!state.exists());
    // The legacy file lives under this workspace's state dir, never the live one.
    assert!(state.with_extension("json.migrated").exists());

    jobs::ensure_seeded(&w.paths, &dao).unwrap();
    assert_eq!(dao.history(Some(CACHE_SYNC), 20, None, None).unwrap().len(), 1);
}

#[test]
fn the_jobs_and_the_run_status_domain_agree() {
    assert_eq!(runs::LIVE, ["submitting", "running"]);
    assert_eq!(globus::TERMINAL, ["SUCCEEDED", "FAILED"]);
}

#[test]
fn a_run_is_marked_submitting_only_while_its_submit_is_in_flight() {
    let w = common::workspace();
    let dao = w.dao();
    let stub = Stub::default();

    let result = run(&w, &dao, &stub, WORKSPACE_BACKUP, "manual", false);
    let rid = result["run_id"].as_str().unwrap().to_string();

    let seen = stub.seen_submitting.lock().unwrap();
    assert_eq!(seen.len(), 1);
    assert!(seen[0].contains(&rid));
    assert!(!jobs::submitting().contains(&rid));
}

#[test]
fn the_two_default_schedules_parse() {
    for j in &JOBS {
        awm_plugin_dvc::cron::parse(j.cron).unwrap();
    }
}
