//! The sweep's live list comes from cch, and no answer stops the sweep, ported from
//! `test_live.py`. Also the run history a sweep and a prune write.

mod common;

use awm_kb_transcripts::live_ids;
use common::*;
use serde_json::json;

#[test]
fn the_live_sessions_are_cchs_answer() {
    let ids = live_ids(&json!({"ok": true, "session_ids": ["a", "b"]})).unwrap();
    assert_eq!(ids, ["a".to_string(), "b".to_string()].into_iter().collect());
}

#[test]
fn a_refusal_stops_the_sweep() {
    for (answer, why) in [
        (json!({"ok": false, "reason": "unreadable /x/state.json"}), "unreadable /x/state.json"),
        (json!(null), "None"),
        (json!("nonsense"), "nonsense"),
    ] {
        assert_eq!(live_ids(&answer).unwrap_err(), format!("cch cannot list the live sessions: {why}"));
    }
}

#[tokio::test]
async fn no_answer_stops_the_sweep_and_moves_nothing() {
    let r = rig_with(Err("cch did not answer live_session_ids: gateway down".into()));
    make_session(&r.projects(), "proj", "sess", 30.0, true, true);
    let e = r.app.sweep(&json!({}), None, "manual").await.unwrap_err();
    assert_eq!((e.class, e.message.as_str()), ("Unknown", "cch did not answer live_session_ids: gateway down"));
    assert!(r.projects().join("proj/sess.jsonl").exists());
    assert_eq!(r.app.runs(&json!({})).await.unwrap()["runs"], json!([]));
}

/// A sweep writes its run row; the history reads newest first, in the table's column order.
#[tokio::test]
async fn a_sweep_records_its_run() {
    let r = rig();
    make_session(&r.projects(), "proj", "sess", 30.0, true, true);
    let out = r.app.sweep(&json!({"days": "7"}), None, "manual").await.unwrap();
    let pruned = r.app.prune(&json!({"days": 1, "_trigger": "test"}), None).await.unwrap();
    assert_eq!(pruned["dry_run"], true);
    let runs = r.app.runs(&json!({"limit": 5})).await.unwrap()["runs"].clone();
    let rows = runs.as_array().unwrap();
    assert_eq!(rows.len(), 2);
    let sweep = rows.iter().find(|r| r["kind"] == "sweep").unwrap();
    assert_eq!(sweep["id"], out["run"]);
    let keys: Vec<&str> = sweep.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(keys, ["id", "kind", "trigger", "dry_run", "retention_days", "sessions", "orphans", "files", "vanished", "bytes_before", "bytes_after", "failures", "detail", "started_at", "seconds"]);
    assert_eq!(
        (sweep["trigger"].clone(), sweep["dry_run"].clone(), sweep["retention_days"].clone(), sweep["sessions"].clone(), sweep["files"].clone()),
        (json!("manual"), json!(0), json!(7.0), json!(1), json!(3))
    );
    assert!(sweep["started_at"].as_str().unwrap().ends_with("+00:00"));
    assert_eq!(rows.iter().find(|r| r["kind"] == "prune").unwrap()["trigger"], "test");
    let e = r.app.prune(&json!({}), None).await.unwrap_err();
    assert_eq!((e.class, e.message.as_str()), ("KeyError", "'days'"));
}
