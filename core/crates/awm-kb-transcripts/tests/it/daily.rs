//! The in-plugin sweep: when it runs, and what it records.

use awm_kb_transcripts::{daily, runs};
use crate::common::*;
use serde_json::json;

#[test]
fn a_sweep_is_due_with_none_yet_or_past_the_gap() {
    let gap = daily::SWEEP_GAP_S;
    assert!(daily::due(None, 1000.0));
    assert!(daily::due(Some(0.0), gap + 1.0));
    assert!(!daily::due(Some(0.0), gap - 1.0));
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
