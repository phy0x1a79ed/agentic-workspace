//! The run table, against a real SQLite file. The partial unique index is the single-flight
//! guard, so a fake DAO would test nothing that matters.

use crate::common;

use awm_plugin_dvc::now;
use serde_json::json;

fn id(r: &awm_plugin_dvc::runs::Row) -> String {
    r["id"].as_str().unwrap().to_string()
}

#[test]
fn a_second_run_of_the_same_job_cannot_claim_the_slot() {
    let w = common::workspace();
    let dao = w.dao();
    assert!(dao.begin_run("cache_sync", "schedule", "").unwrap().is_some());
    assert!(dao.begin_run("cache_sync", "manual", "").unwrap().is_none());
    assert!(dao.begin_run("workspace_backup", "manual", "").unwrap().is_some(), "the guard is per job");

    let live = dao.live_run("cache_sync").unwrap().unwrap();
    dao.mark_submitted(&id(&live), "task-1", "").unwrap();
    assert_eq!(dao.close_live("cache_sync", "superseded by a forced run").unwrap(), 1);
    assert_eq!(dao.get_run(&id(&live)).unwrap().unwrap()["status"], "skipped");
    assert!(dao.begin_run("cache_sync", "manual", "").unwrap().is_some(), "force retires the live row");

    dao.close_live("workspace_backup", "x").unwrap();
    dao.record_skip("workspace_backup", "schedule", "declined").unwrap();
    assert!(dao.begin_run("workspace_backup", "manual", "").unwrap().is_some(), "a skipped row does not hold the slot");
}

#[test]
fn the_slot_frees_once_the_run_reaches_a_verdict() {
    let w = common::workspace();
    let dao = w.dao();
    let first = dao.begin_run("cache_sync", "manual", "").unwrap().unwrap();
    dao.mark_submitted(&id(&first), "task-1", "").unwrap();
    dao.finish(&id(&first), "SUCCEEDED", &json!({"files": 5, "bytes_transferred": 99})).unwrap();

    let second = dao.begin_run("cache_sync", "manual", "").unwrap().unwrap();
    assert_ne!(id(&second), id(&first));
    let done = dao.get_run(&id(&first)).unwrap().unwrap();
    assert_eq!((done["files"].as_i64(), done["bytes_transferred"].as_i64()), (Some(5), Some(99)));
}

#[test]
fn two_runs_cannot_record_the_same_globus_task() {
    let w = common::workspace();
    let dao = w.dao();
    let a = dao.begin_run("cache_sync", "manual", "").unwrap().unwrap();
    dao.mark_submitted(&id(&a), "task-1", "").unwrap();
    dao.finish(&id(&a), "SUCCEEDED", &json!({})).unwrap();
    let b = dao.begin_run("cache_sync", "manual", "").unwrap().unwrap();

    let err = dao.mark_submitted(&id(&b), "task-1", "").unwrap_err();
    assert!(err.to_string().contains("UNIQUE"), "{err}");
}

#[test]
fn a_poll_records_counters_without_ending_the_run() {
    let w = common::workspace();
    let dao = w.dao();
    let run = dao.begin_run("workspace_backup", "manual", "").unwrap().unwrap();
    dao.mark_submitted(&id(&run), "task-9", "").unwrap();

    dao.record_poll(
        &id(&run),
        &json!({"files": 100, "files_transferred": 40, "files_skipped": 1, "bytes_transferred": 4096, "faults": 2, "nice_status": "PERMISSION_DENIED"}),
    )
    .unwrap();
    let row = dao.get_run(&id(&run)).unwrap().unwrap();

    assert_eq!(row["status"], "running");
    assert_eq!((row["files_transferred"].as_i64(), row["faults"].as_i64()), (Some(40), Some(2)));
    assert_eq!(row["nice_status"], "PERMISSION_DENIED");
    assert!(row["polled_at"].as_f64().unwrap() > 0.0);
    assert_eq!(row["finished_at"].as_f64(), Some(0.0));
}

#[test]
fn a_failed_submission_is_an_error_row_not_silence() {
    let w = common::workspace();
    let dao = w.dao();
    let run = dao.begin_run("cache_sync", "manual", "").unwrap().unwrap();

    dao.fail(&id(&run), "GlobusError: not logged in").unwrap();
    let row = dao.get_run(&id(&run)).unwrap().unwrap();

    assert_eq!(row["status"], "error");
    assert!(row["error"].as_str().unwrap().contains("not logged in"));
    assert!(dao.begin_run("cache_sync", "manual", "").unwrap().is_some());
}

#[test]
fn live_runs_finds_exactly_what_the_adopt_sweep_must_pick_up() {
    let w = common::workspace();
    let dao = w.dao();
    let submitting = dao.begin_run("cache_sync", "manual", "").unwrap().unwrap();
    let running = dao.begin_run("workspace_backup", "manual", "").unwrap().unwrap();
    dao.mark_submitted(&id(&running), "task-2", "").unwrap();
    dao.record_skip("cache_sync", "manual", "x").unwrap();

    let mut ids: Vec<String> = dao.live_runs().unwrap().iter().map(id).collect();
    ids.sort();
    let mut want = vec![id(&submitting), id(&running)];
    want.sort();
    assert_eq!(ids, want);
}

#[test]
fn history_filters_by_job_status_and_time() {
    let w = common::workspace();
    let dao = w.dao();
    let old = dao.begin_run("cache_sync", "manual", "").unwrap().unwrap();
    dao.finish(&id(&old), "FAILED", &json!({})).unwrap();
    dao.execute("UPDATE dvc_runs SET started_at = ? WHERE id = ?", &[&(now() - 10.0), &id(&old)]).unwrap();
    let new = dao.begin_run("cache_sync", "manual", "").unwrap().unwrap();
    dao.finish(&id(&new), "SUCCEEDED", &json!({})).unwrap();
    dao.record_skip("workspace_backup", "manual", "x").unwrap();

    let by_job: Vec<String> = dao.history(Some("cache_sync"), 20, None, None).unwrap().iter().map(id).collect();
    assert_eq!(by_job, vec![id(&new), id(&old)]);
    let ok: Vec<String> = dao.history(None, 20, Some("SUCCEEDED"), None).unwrap().iter().map(|r| r["status"].as_str().unwrap().to_string()).collect();
    assert_eq!(ok, vec!["SUCCEEDED"]);
    assert!(dao.history(None, 20, None, Some(now() + 60.0)).unwrap().is_empty());
    assert_eq!(dao.history(None, 1, None, None).unwrap().len(), 1);
}

#[test]
fn a_seeded_schedule_edit_survives_the_next_restart() {
    let w = common::workspace();
    let dao = w.dao();
    dao.seed_job("cache_sync", "0 4 * * *", 72000, true).unwrap();
    dao.set_schedule("cache_sync", Some("@every 5m"), Some(false)).unwrap();

    dao.seed_job("cache_sync", "0 4 * * *", 72000, true).unwrap();
    let row = dao.get_job("cache_sync").unwrap().unwrap();

    assert_eq!(row["cron"], "@every 5m");
    assert_eq!(row["enabled"].as_i64(), Some(0));
}

#[test]
fn a_submit_that_lands_after_an_orphan_verdict_clears_the_verdict() {
    let w = common::workspace();
    let dao = w.dao();
    let run = dao.begin_run("workspace_backup", "manual", "").unwrap().unwrap();
    dao.fail(&id(&run), "orphaned: no Globus task").unwrap();

    dao.mark_submitted(&id(&run), "task-abc", "").unwrap();
    let row = dao.get_run(&id(&run)).unwrap().unwrap();

    assert_eq!(row["status"], "running");
    assert_eq!(row["error"], "");
    assert_eq!(row["finished_at"].as_f64(), Some(0.0));
}
