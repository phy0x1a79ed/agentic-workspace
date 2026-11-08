//! The adopt sweep, the one rule that covers every way a run loses its watcher. Adoption
//! never resubmits: the Globus task id is authoritative.

mod common;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use awm_plugin_dvc::jobs::{self, Submitter};
use awm_plugin_dvc::runs::RunsDao;
use awm_plugin_dvc::watcher::ORPHAN_AFTER_S;
use awm_plugin_dvc::now;
use common::{doc, statuses};
use serde_json::{json, Value};

fn succeeding() -> Arc<common::FakeApi> {
    common::task_api(vec![], Some(doc("SUCCEEDED", json!({"files": 1, "files_transferred": 1, "bytes_transferred": 8}))), vec![])
}

fn age(dao: &RunsDao, id: &str) {
    dao.execute("UPDATE dvc_runs SET started_at = ? WHERE id = ?", &[&(now() - ORPHAN_AFTER_S - 1.0), &id]).unwrap();
}

fn rid(r: &awm_plugin_dvc::runs::Row) -> String {
    r["id"].as_str().unwrap().to_string()
}

#[tokio::test]
async fn a_live_run_left_by_a_dead_process_is_adopted_to_a_verdict() {
    let ws = common::workspace();
    let dao = ws.dao();
    let run = common::live_run(&dao, "cache_sync", "task-abc");
    let api = succeeding();
    let (_events, emit) = common::recorder();
    let w = common::watcher(dao.clone(), api.clone(), emit);

    w.sweep().await.unwrap();
    common::settle(&w).await;

    assert_eq!(dao.get_run(&rid(&run)).unwrap().unwrap()["status"], "SUCCEEDED");
    assert!(api.calls().iter().all(|c| c.method == "GET" && c.path.starts_with("/task/task-abc")));
}

#[tokio::test]
async fn adoption_does_not_stack_a_second_watcher_on_one_run() {
    let ws = common::workspace();
    let dao = ws.dao();
    common::live_run(&dao, "cache_sync", "task-abc");
    // A task that never ends, with counters that never move: one watcher emits one progress.
    let api = common::task_api(vec![], Some(doc("ACTIVE", json!({}))), vec![]);
    let (events, emit) = common::recorder();
    let w = common::watcher(dao.clone(), api, emit);

    w.sweep().await.unwrap();
    tokio::time::sleep(Duration::from_millis(30)).await;
    w.sweep().await.unwrap();
    tokio::time::sleep(Duration::from_millis(30)).await;

    assert_eq!(w.health()["watching"], 1);
    assert_eq!(w.health()["sweeps"], 2);
    assert_eq!(statuses(&events), ["submitted", "progress"]);
}

#[tokio::test]
async fn a_finished_run_is_not_adopted() {
    let ws = common::workspace();
    let dao = ws.dao();
    let run = common::live_run(&dao, "cache_sync", "task-abc");
    dao.finish(&rid(&run), "SUCCEEDED", &json!({})).unwrap();
    let api = succeeding();
    let (events, emit) = common::recorder();
    let w = common::watcher(dao.clone(), api.clone(), emit);

    w.sweep().await.unwrap();

    assert_eq!(w.health()["watching"], 0);
    assert!(api.calls().is_empty());
    assert!(events.lock().unwrap().is_empty());
}

#[tokio::test]
async fn a_row_that_never_reached_globus_becomes_an_error_not_a_zombie() {
    let ws = common::workspace();
    let dao = ws.dao();
    let run = dao.begin_run("cache_sync", "manual", "").unwrap().unwrap();
    age(&dao, &rid(&run));
    let (events, emit) = common::recorder();
    let w = common::watcher(dao.clone(), succeeding(), emit);

    w.sweep().await.unwrap();
    let row = dao.get_run(&rid(&run)).unwrap().unwrap();

    assert_eq!(row["status"], "error");
    assert!(row["error"].as_str().unwrap().contains("no Globus task"));
    assert_eq!(statuses(&events), ["error"]);
    // ...and the job's slot is free again, so tonight's run can proceed.
    assert!(dao.begin_run("cache_sync", "manual", "").unwrap().is_some());
}

#[tokio::test]
async fn a_run_still_submitting_is_given_time_before_being_declared_orphaned() {
    let ws = common::workspace();
    let dao = ws.dao();
    let run = dao.begin_run("workspace_backup", "manual", "").unwrap().unwrap();
    let (events, emit) = common::recorder();
    let w = common::watcher(dao.clone(), succeeding(), emit);

    w.sweep().await.unwrap();

    assert_eq!(dao.get_run(&rid(&run)).unwrap().unwrap()["status"], "submitting");
    assert_eq!(w.health()["watching"], 0);
    assert!(events.lock().unwrap().is_empty());
}

/// Sweeps from inside its own submit, with its row aged past the orphan limit.
struct SweepWhileSubmitting {
    dao: Arc<RunsDao>,
    seen: Mutex<Option<(Value, usize)>>,
}

impl Submitter for SweepWhileSubmitting {
    fn submit(&self, job: &str, _dry_run: bool) -> anyhow::Result<Value> {
        let run = self.dao.live_run(job)?.expect("the slot is claimed before the submit");
        age(&self.dao, &rid(&run));
        let (events, emit) = common::recorder();
        let w = common::watcher(self.dao.clone(), succeeding(), emit);
        tokio::runtime::Builder::new_current_thread().enable_all().build()?.block_on(w.sweep())?;
        let status = self.dao.get_run(&rid(&run))?.unwrap()["status"].clone();
        *self.seen.lock().unwrap() = Some((status, events.lock().unwrap().len()));
        Ok(json!({"submitted": true, "task_id": "task-mirror"}))
    }
}

#[test]
fn a_run_this_process_is_still_submitting_is_never_declared_orphaned() {
    let ws = common::workspace();
    let dao = ws.dao();
    let sub = SweepWhileSubmitting { dao: dao.clone(), seen: Mutex::new(None) };

    let out = jobs::run_job(&ws.paths, &dao, &sub, jobs::WORKSPACE_BACKUP, "manual", false, false).unwrap();

    assert_eq!(*sub.seen.lock().unwrap(), Some((json!("submitting"), 0)));
    assert_eq!(dao.get_run(out["run_id"].as_str().unwrap()).unwrap().unwrap()["status"], "running");
}
