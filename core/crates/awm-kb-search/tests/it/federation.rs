//! The federation: a stalled source and a down one each answer with an error in their own
//! section within the timeout while the others answer, and a peer caller never sees a section
//! from a source that did not apply the veil.

use crate::common::*;
use serde_json::json;
use std::time::{Duration, Instant};

const PEER: Option<&str> = Some("peer:altair");

fn hits() -> serde_json::Value {
    json!([{"id": "f1", "title": "from the fake", "ts": "2026-01-01T00:00:00Z", "secret": "dropped"}])
}

async fn loaded(extra: Vec<std::sync::Arc<dyn awm_kb_source::Source>>) -> Rig {
    let mut r = rig_with(extra).await;
    std::sync::Arc::get_mut(&mut r.search).unwrap().timeout = Duration::from_millis(500);
    r.journals.put(&[("j1", doc("Sink", "the kitchen sink", "2026-03-01T10:00:00Z"))]).await;
    r
}

#[tokio::test]
async fn a_stalled_source_errs_in_its_own_section_within_the_timeout() {
    let r = loaded(vec![Fake::new("slow", Act::Stall)]).await;
    let t = Instant::now();
    let got = r.ask(json!({"query": {"text": "kitchen sink", "journals": {}, "slow": {}}})).await;
    assert!(t.elapsed() < Duration::from_secs(3), "{:?}", t.elapsed());
    assert_eq!(got["slow"], json!({"exact": [], "error": "timeout after 0.5s"}));
    assert_eq!(ids(&got["journals"]["exact"]), ["j1"]);
    let status = r.search.status(None).await;
    assert_eq!(status["answers"]["slow"]["error"], "timeout after 0.5s");
    assert!(status["answers"]["journals"]["error"].is_null() && status["answers"]["journals"]["at"].is_string());
}

#[tokio::test]
async fn a_down_source_errs_in_its_own_section() {
    let r = loaded(vec![Fake::new("broken", Act::Down("the table is gone".into()))]).await;
    let got = r.ask(json!({"query": {"text": "kitchen sink", "journals": {}, "broken": {}}})).await;
    assert_eq!(got["broken"], json!({"exact": [], "error": "query.broken: the table is gone"}));
    assert_eq!(ids(&got["journals"]["exact"]), ["j1"]);
    let e = r.search.get(&json!({"source": "broken", "id": "x"}), None, None).await.unwrap_err();
    assert_eq!((e.class.as_str(), e.message.as_str()), ("RuntimeError", "the table is gone"));
}

#[tokio::test]
async fn hits_from_a_source_are_cut_to_the_declared_fields() {
    let r = loaded(vec![Fake::new("fake", Act::Hits(hits()))]).await;
    let got = r.ask(json!({"query": {"text": "x", "fake": {}}})).await;
    assert_eq!(got["fake"], json!({"exact": [{"id": "f1", "title": "from the fake", "ts": "2026-01-01T00:00:00Z"}]}));
}

#[tokio::test]
async fn a_source_that_omits_the_veil_is_refused_to_a_peer_and_served_locally() {
    let fake = Fake::new("careless", Act::Unveiled(hits()));
    let r = loaded(vec![fake.clone()]).await;
    let local = r.ask(json!({"query": {"text": "x", "careless": {}}})).await;
    assert_eq!(ids(&local["careless"]["exact"]), ["f1"]);
    let peer = r.ask_as(json!({"query": {"text": "kitchen sink", "careless": {}, "journals": {}}}), PEER).await.unwrap();
    assert_eq!(peer["careless"]["exact"], json!([]));
    assert!(peer["careless"]["error"].as_str().unwrap().starts_with("refused: "));
    assert_eq!(ids(&peer["journals"]["exact"]), ["j1"], "a source that applies the veil still answers");
    assert!(peer["journals"].get("veiled").is_none(), "the veil mark stays inside the node");
    // The source was handed the peer's veil.
    let asked = fake.asked.lock().unwrap();
    assert_eq!(asked[0].veil, awm_index::Veil::None);
    assert_ne!(asked[1].veil, awm_index::Veil::None);
}

#[tokio::test]
async fn a_peer_that_stalls_or_fails_errs_on_its_sources_only() {
    let r = loaded(vec![]).await;
    r.peers.add("vega", true, "homebase", |_| Err(awm_kb_search::CallError::new("ConnectionError", "unreachable")));
    let got = r.ask(json!({"query": {"text": "kitchen sink", "journals": {}, "journals@vega": {}, "kb@vega": {}}})).await;
    assert_eq!(got["journals@vega"]["error"], "ConnectionError: unreachable");
    assert_eq!(got["kb@vega"]["error"], "ConnectionError: unreachable");
    assert_eq!(ids(&got["journals"]["exact"]), ["j1"]);
    assert_eq!(r.peers.calls.lock().unwrap().len(), 1, "one call per node");
}

#[tokio::test]
async fn a_peer_reply_names_a_source_it_did_not_answer() {
    let r = loaded(vec![]).await;
    r.peers.add("vega", true, "homebase", |_| Ok(json!({"journals": {"exact": [], "error": "x".repeat(400)}})));
    let got = r.ask(json!({"query": {"text": "s", "journals@vega": {}, "kb@vega": {}}})).await;
    assert_eq!(got["journals@vega"]["error"].as_str().unwrap().len(), 300);
    assert_eq!(got["kb@vega"], json!({"exact": [], "error": "the node did not answer for this source"}));
}

