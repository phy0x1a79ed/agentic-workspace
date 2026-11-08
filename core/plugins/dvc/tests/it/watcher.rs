//! The watcher: a Globus task polled to a verdict, against a scripted sequence. Every poll
//! reaches the run row before it reaches a subscriber, because emit is lossy; these tests
//! check the row, not just the events. A watch that attaches to a never-polled run first
//! emits `submitted`.

use crate::common;

use std::sync::{Arc, Mutex};

use awm_plugin_dvc::now;
use common::{doc, statuses};
use serde_json::{json, Value};

fn rid(r: &awm_plugin_dvc::runs::Row) -> String {
    r["id"].as_str().unwrap().to_string()
}

/// Attach the watcher to the one live run and wait for its verdict.
async fn watch(dao: &Arc<awm_plugin_dvc::runs::RunsDao>, api: Arc<common::FakeApi>, emit: awm_plugin_dvc::watcher::Emit) {
    let w = common::watcher(dao.clone(), api, emit);
    w.sweep().await.unwrap();
    common::settle(&w).await;
}

#[tokio::test]
async fn a_task_that_succeeds_ends_the_run_with_its_counters() {
    let ws = common::workspace();
    let dao = ws.dao();
    let run = common::live_run(&dao, "cache_sync", "task-1");
    let api = common::task_api(
        vec![doc("ACTIVE", json!({"files_transferred": 3})), doc("SUCCEEDED", json!({"files_transferred": 10, "bytes_transferred": 4096}))],
        None,
        vec![],
    );
    let (events, emit) = common::recorder();

    watch(&dao, api, emit).await;
    let row = dao.get_run(&rid(&run)).unwrap().unwrap();

    assert_eq!(row["status"], "SUCCEEDED");
    assert_eq!((row["files_transferred"].as_i64(), row["bytes_transferred"].as_i64()), (Some(10), Some(4096)));
    assert!(row["finished_at"].as_f64().unwrap() > 0.0);
    assert_eq!(statuses(&events), ["submitted", "progress", "succeeded"]);
}

#[tokio::test]
async fn a_task_that_fails_is_recorded_as_failed_not_as_silence() {
    let ws = common::workspace();
    let dao = ws.dao();
    let run = common::live_run(&dao, "cache_sync", "task-1");
    let api = common::task_api(vec![doc("FAILED", json!({"nice_status": "PERMISSION_DENIED", "faults": 3}))], None, vec![]);
    let (events, emit) = common::recorder();

    watch(&dao, api, emit).await;
    let row = dao.get_run(&rid(&run)).unwrap().unwrap();

    assert_eq!(row["status"], "FAILED");
    assert_eq!(row["nice_status"], "PERMISSION_DENIED");
    assert_eq!(row["faults"].as_i64(), Some(3));
    assert_eq!(statuses(&events), ["submitted", "failed"]);
}

#[tokio::test]
async fn a_steady_task_does_not_produce_an_event_per_poll() {
    let ws = common::workspace();
    let dao = ws.dao();
    common::live_run(&dao, "cache_sync", "task-1");
    let api = common::task_api(vec![doc("ACTIVE", json!({})), doc("ACTIVE", json!({})), doc("ACTIVE", json!({})), doc("SUCCEEDED", json!({}))], None, vec![]);
    let (events, emit) = common::recorder();

    watch(&dao, api, emit).await;

    assert_eq!(statuses(&events), ["submitted", "progress", "succeeded"]);

    // Counters that move emit progress again; a run already polled does not announce itself again.
    let run = common::live_run(&dao, "workspace_backup", "task-2");
    dao.record_poll(&rid(&run), &doc("ACTIVE", json!({}))).unwrap();
    let api = common::task_api(
        vec![doc("ACTIVE", json!({"files_transferred": 1})), doc("ACTIVE", json!({"files_transferred": 2})), doc("SUCCEEDED", json!({}))],
        None,
        vec![],
    );
    let (events, emit) = common::recorder();
    watch(&dao, api, emit).await;
    assert_eq!(statuses(&events), ["progress", "progress", "succeeded"]);
}

#[tokio::test]
async fn the_row_is_updated_before_any_event_is_emitted() {
    let ws = common::workspace();
    let dao = ws.dao();
    let run = common::live_run(&dao, "cache_sync", "task-1");
    let seen: Arc<Mutex<Vec<i64>>> = Arc::default();
    let (d, s, id) = (dao.clone(), seen.clone(), rid(&run));
    let emit: awm_plugin_dvc::watcher::Emit = Arc::new(move |_p: Value| {
        s.lock().unwrap().push(d.get_run(&id).unwrap().unwrap()["files_transferred"].as_i64().unwrap());
    });
    let api = common::task_api(vec![doc("ACTIVE", json!({"files_transferred": 7})), doc("SUCCEEDED", json!({"files_transferred": 10}))], None, vec![]);

    watch(&dao, api, emit).await;

    // 0 is the `submitted` event, before any poll.
    assert_eq!(*seen.lock().unwrap(), [0, 7, 10]);
}

#[tokio::test]
async fn a_success_that_transferred_nothing_says_why() {
    let ws = common::workspace();
    let dao = ws.dao();
    let run = common::live_run(&dao, "cache_sync", "task-1");
    let skipped = vec![
        json!({"source_path": "/ws/keep/", "error_code": "PERMISSION_DENIED"}),
        json!({"source_path": "/ws/top.txt", "error_code": "PERMISSION_DENIED"}),
    ];
    let api = common::task_api(vec![doc("SUCCEEDED", json!({"files_transferred": 0}))], None, skipped);
    let (events, emit) = common::recorder();

    watch(&dao, api, emit).await;
    let row = dao.get_run(&rid(&run)).unwrap().unwrap();

    assert_eq!(row["status"], "SUCCEEDED");
    let note = row["note"].as_str().unwrap();
    assert!(note.contains("2 source path(s) skipped"), "{note}");
    assert!(note.contains("PERMISSION_DENIED×2"), "{note}");
    let last = events.lock().unwrap().last().cloned().unwrap();
    assert!(last["message"].as_str().unwrap().contains("skipped"), "{last}");

    let run = common::live_run(&dao, "workspace_backup", "task-2");
    let api = common::task_api(vec![doc("SUCCEEDED", json!({"files_transferred": 9999}))], None, vec![json!({"source_path": "/ws/gone", "error_code": "NOT_FOUND"})]);
    let (_events, emit) = common::recorder();
    watch(&dao, api, emit).await;
    assert!(dao.get_run(&rid(&run)).unwrap().unwrap()["note"].as_str().unwrap().contains("1 source path(s) skipped"));
}

#[tokio::test]
async fn an_unqueryable_task_eventually_gets_an_error_row() {
    let ws = common::workspace();
    let dao = ws.dao();
    let run = common::live_run(&dao, "cache_sync", "task-1");
    let api = common::FakeApi::new(|_, _, _| Err(awm_plugin_dvc::globus::GlobusError("no such task".into())));
    let (events, emit) = common::recorder();

    watch(&dao, api.clone(), emit).await;
    let row = dao.get_run(&rid(&run)).unwrap().unwrap();

    assert_eq!(row["status"], "error");
    assert!(row["error"].as_str().unwrap().contains("no such task"));
    assert_eq!(statuses(&events), ["submitted", "error"]);
    assert_eq!(api.calls().len() as u32, awm_plugin_dvc::watcher::MAX_POLL_FAILURES);
}

#[tokio::test]
async fn a_long_run_is_alerted_once_and_keeps_polling() {
    let ws = common::workspace();
    let dao = ws.dao();
    let run = common::live_run(&dao, "cache_sync", "task-1");
    let old = now() - awm_plugin_dvc::watcher::STALL_AFTER_S - 60.0;
    dao.execute("UPDATE dvc_runs SET started_at = ? WHERE id = ?", &[&old, &rid(&run)]).unwrap();
    let api = common::task_api(
        vec![doc("ACTIVE", json!({"files_transferred": 1})), doc("ACTIVE", json!({"files_transferred": 2})), doc("SUCCEEDED", json!({}))],
        None,
        vec![],
    );
    let (events, emit) = common::recorder();

    watch(&dao, api, emit).await;
    let row = dao.get_run(&rid(&run)).unwrap().unwrap();

    assert_eq!(statuses(&events).iter().filter(|s| *s == "stalled").count(), 1);
    assert_eq!(row["status"], "SUCCEEDED");
    assert!(row["alerted_at"].as_f64().unwrap() > 0.0);
}

