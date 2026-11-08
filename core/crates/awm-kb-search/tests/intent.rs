//! The reference calls the signature was validated on, against a synthetic journal corpus.
//! Ported from `test_intent.py`: only the journals half of each call is asserted here.

mod common;

use common::*;
use serde_json::{json, Value};

fn corpus() -> Vec<(&'static str, Value)> {
    let j = |p: &str, s: &str, tags: Value, ts: &str, title: &str, body: &str| {
        json!({"project": p, "scope": s, "tag": tags, "ts": ts, "title": title, "text": body})
    };
    vec![
        ("J1", j("awm", "redux-dev", json!(["debrief"]), "2026-10-12T09:00:00Z", "Trigram budget cut to 3 per query", "The trigram budget now caps fan-out.")),
        ("J2", j("awm", "svc-search", json!(["issue"]), "2026-10-20T14:00:00Z", "p95 regression after reindex", "p95 rose to 900 ms once the trigram budget ran out.")),
        ("J3", j("awm", "svc-search", json!(["issue", "closed"]), "2026-10-05T11:00:00Z", "Timeout on peer fan-out", "A timeout fires when the peer is slow.")),
        ("J4", j("awm", "svc-journal", json!(["journal"]), "2026-10-31T18:30:00Z", "Deploy notes", "Saw one timeout in the replay gate.")),
        ("J5", j("awm", "svc-journal", json!(["issue"]), "2026-11-01T08:00:00Z", "Fetch fails again", "Another timeout on fetch.")),
        ("J6", j("awm", "svc-search/bench", json!(["journal"]), "2026-10-15T10:00:00Z", "Benchmark run", "p95 steady at 120 ms.")),
        ("J7", j("awm", "svc-search", json!(["journal"]), "2026-10-18T10:00:00Z", "Refactor ranking", "Search latency is flat after the refactor.")),
        ("J8", j("metasmith", "solver", json!(["issue"]), "2026-10-10T10:00:00Z", "Solver stalls", "A timeout in the gurobi call.")),
        ("J9", j("awm", "redux-dev", json!(["issue"]), "2026-09-28T10:00:00Z", "Search slow at scale", "p95 doubles at 10x load.")),
        ("J10", j("awm", "redux-dev", json!(["draft"]), "2026-10-22T10:00:00Z", "HNSW against IVF, notes", "HNSW index build is slow.")),
        ("J11", j("awm", "svc-search", json!(["journal"]), "2026-10-25T10:00:00Z", "HNSW recall check", "HNSW recall 0.97, FAISS baseline 0.95.")),
        ("J12", j("awm", "svc-search", json!(["journal"]), "2025-03-02T10:00:00Z", "Adopt HNSW", "We move the vector index to HNSW.")),
        ("J13", j("awm", "svc-search", json!(["journal"]), "2024-11-30T10:00:00Z", "First HNSW trial", "HNSW prototype works.")),
        ("J14", j("awm", "svc-kb", json!(["issue", "closed"]), "2026-10-08T10:00:00Z", "p95 spike on ingest", "Fixed by batching.")),
    ]
}

fn reference() -> Vec<Value> {
    vec![
        json!({"query": "trigram budget"}),
        json!({"query": {"journals": {"project": "awm", "tag": {"$all": ["issue"], "$nin": ["closed"]}}}}),
        json!({"query": {"text": {"$and": [{"$about": "search latency"}, {"$or": [{"$contains": "p95"}, {"$contains": "timeout"}]}]},
                         "journals": {"project": "awm", "scope": "svc-*", "ts": {"$gte": "2026-10-01", "$lt": "2026-11-01"}}}}),
        json!({"query": {"text": {"$about": "metagenome binning"}, "kb@altair": {"cites": "P1", "ts": {"$gte": "2024-01-01"}}}}),
        json!({"query": {"text": {"$and": [{"$contains": "HNSW"}, {"$nor": [{"$contains": "FAISS"}]}]},
                         "journals": {"ts": {"$gte": "2025-01-01"}, "tag": {"$ne": "draft"}},
                         "journals@altair": {"ts": {"$gte": "2025-01-01"}, "tag": {"$ne": "draft"}},
                         "transcripts": {"ts": {"$gte": "2025-01-01"}}, "kb@altair": {"ts": {"$gte": "2025-01-01"}}}}),
    ]
}

/// The reference call without its kb, transcripts and `@node` sources.
fn journals_only(call: &Value) -> Value {
    let mut call = call.clone();
    if let Some(q) = call["query"].as_object_mut() {
        q.retain(|k, _| k == "text" || k == "journals");
    }
    call
}

async fn loaded() -> Rig {
    let r = rig().await;
    r.journals.put(&corpus()).await;
    r
}

#[tokio::test]
async fn every_reference_call_parses_and_drops_what_is_not_here() {
    let r = rig().await;
    for call in reference() {
        let req = r.search.parse(&call, None).await.unwrap();
        assert!(!req.sources.is_empty() || !req.remote.is_empty());
    }
    let req = r.search.parse(&reference()[4], None).await.unwrap();
    assert_eq!(req.sources.iter().map(|s| s.name.as_str()).collect::<Vec<_>>(), ["journals", "transcripts"]);
    assert_eq!(req.remote.keys().collect::<Vec<_>>(), ["altair"]);
    let req = r.search.parse(&reference()[3], None).await.unwrap();
    assert_eq!(req.remote["altair"].iter().map(|s| s.name.as_str()).collect::<Vec<_>>(), ["kb@altair"]);
}

#[tokio::test]
async fn the_tasks() {
    let r = loaded().await;
    let refs = reference();
    let got = r.ask(journals_only(&refs[0])).await;
    assert!(set(&got["journals"]["exact"]).is_superset(&["J1", "J2"].map(String::from).into()));

    let got = r.ask(journals_only(&refs[1])).await;
    assert_eq!(set(&got["journals"]["exact"]), ["J2", "J5", "J9"].map(String::from).into());
    assert!(got["journals"].get("related").is_none());

    let mut call = journals_only(&refs[1]);
    call["query"]["journals"]["$sort"] = json!("-ts");
    assert_eq!(ids(&r.ask(call).await["journals"]["exact"]), ["J5", "J2", "J9"]);

    let got = r.ask(journals_only(&refs[2])).await;
    assert_eq!(set(&got["journals"]["exact"]), ["J2", "J3", "J4", "J6", "J14"].map(String::from).into());
    assert!(got["journals"].get("related").is_none());

    let got = r.ask(journals_only(&refs[4])).await;
    assert_eq!(ids(&got["journals"]["exact"]), ["J12"]);
    assert!(got["journals"].get("related").is_none());
}
