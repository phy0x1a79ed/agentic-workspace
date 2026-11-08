//! The in-plugin daily sweep: when it runs, and what it records.

mod common;

use awm_kb_transcripts::{daily, runs};
use common::*;
use serde_json::json;

#[test]
fn the_next_run_is_the_next_local_0330() {
    std::env::set_var("TZ", "UTC");
    let day = 86400.0;
    let base = 20_000.0 * day; // a midnight UTC
    let at = 3.0 * 3600.0 + 1800.0;
    assert_eq!(daily::next_at(base, 3, 30), base + at);
    assert_eq!(daily::next_at(base + at, 3, 30), base + day + at, "at 03:30 exactly, the next is tomorrow's");
    assert_eq!(daily::next_at(base + 12.0 * 3600.0, 3, 30), base + day + at);
}

#[test]
fn a_start_catches_up_only_after_26_hours() {
    let now = 1_000_000.0;
    assert!(!daily::due(None, now), "a node that never swept waits for 03:30");
    assert!(!daily::due(Some(now - 25.0 * 3600.0), now));
    assert!(daily::due(Some(now - 27.0 * 3600.0), now));
}

#[tokio::test]
async fn a_timer_sweep_is_recorded_as_one() {
    let r = rig();
    make_session(&r.projects(), "proj", "sess", 30.0, true, true);
    assert_eq!(runs::last_timer(&r.app.store).await.unwrap(), None);
    let env = awm_plugin_sdk::Env {
        hub_url: "http://127.0.0.1:1".into(),
        service: "transcripts".into(),
        plugin: "transcripts".into(),
        node: "here".into(),
        awm_dir: r.dir.path().into(),
        token_file: None,
    };
    let svc = awm_plugin_sdk::Service::new(env);
    daily::run_once(&svc.context(), &r.app).await;
    let rows = r.app.runs(&json!({})).await.unwrap()["runs"].clone();
    assert_eq!((rows[0]["kind"].clone(), rows[0]["trigger"].clone(), rows[0]["sessions"].clone()), (json!("sweep"), json!("timer"), json!(1)));
    let last = runs::last_timer(&r.app.store).await.unwrap().unwrap();
    assert!((awm_kb_transcripts::sweep::now() - last).abs() < 5.0);
    assert!(!r.projects().join("proj/sess.jsonl").exists());
}
