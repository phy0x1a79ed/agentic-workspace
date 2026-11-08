//! The exact section through a kb source on awm-index: phrase matching, filters, `$sort`, the
//! sections of an answer, and sections from elsewhere. Ported from `test_exact.py`; the feed
//! reads are gone, so the source is written directly.

use crate::common::*;
use serde_json::{json, Value};
use std::collections::BTreeSet;

const LOG: &str = "# Deploy notes

The gateway restart on capella failed with
`ConnectionRefusedError: [Errno 111] Connection refused` after commit 3f9a2c1d7e.

## Fix

Edit /etc/awm/env and set AWM_GATEWAY_PORT=8443, then restart the unit.
";

async fn loaded() -> Rig {
    let r = rig().await;
    r.journals
        .put(&[
            ("j1", with(doc("Deploy notes", LOG, "2026-03-01T10:00:00Z"), json!({"project": "awm", "tag": ["journal"]}))),
            ("j2", with(doc("Gateway port", "the gateway port moved to 8443 last week", "2026-05-01T10:00:00Z"), json!({"project": "awm", "tag": ["message"]}))),
            ("j3", with(doc("CONNECTIONREFUSEDERROR in caps", "an old CONNECTIONREFUSEDERROR log", "2026-06-01T10:00:00Z"), json!({"tag": ["journal", "closed"]}))),
            ("p1", with(doc("Ports", "prefer port 8443 for the gateway", "2026-04-01T10:00:00Z"), json!({"tag": ["precedent"]}))),
            ("w1", doc("Ports again", "the gateway listens on port 8443", "2026-02-01T10:00:00Z")),
        ])
        .await;
    r
}

fn s(v: &[&str]) -> BTreeSet<String> {
    v.iter().map(|x| x.to_string()).collect()
}

async fn exact(r: &Rig, query: Value, max: usize) -> BTreeSet<String> {
    set(&r.ask(json!({"query": query, "max": max})).await["journals"]["exact"])
}

fn contains(t: &str) -> Value {
    json!({"text": {"$contains": t}})
}

#[tokio::test]
async fn exact_matching_cases() {
    let r = loaded().await;
    let cases: Vec<(Value, Vec<&str>)> = vec![
        // phrases: literals, punctuation, case, and what must not match
        (contains("3f9a2c1d7e"), vec!["j1"]),
        (contains("/etc/awm/env"), vec!["j1"]),
        (contains("[Errno 111] Connection refused"), vec!["j1"]),
        (contains("AWM_GATEWAY_PORT=8443"), vec!["j1"]),
        (contains("3f9a2c1d7e.\n\n## Fix"), vec!["j1"]),
        (contains("Connection-refused"), vec!["j1"]),
        (contains("connectionrefusederror"), vec!["j1", "j3"]),
        (contains("3f9a"), vec![]),
        (contains("8443 port"), vec![]),
        // filters
        (json!({"journals": {"tag": "precedent"}}), vec!["p1"]),
        (json!({"journals": {"tag": "PRECEDENT"}}), vec![]),
        (json!({"journals": {"tag": "jour*"}}), vec!["j1", "j3"]),
        (json!({"journals": {"tag": {"$all": ["journal", "closed"]}}}), vec!["j3"]),
        (json!({"journals": {"tag": {"$nin": ["closed"]}}}), vec!["j1", "j2", "p1", "w1"]),
        (json!({"journals": {"$nor": [{"tag": "journal"}]}}), vec!["j2", "p1", "w1"]),
        (json!({"journals": {"$and": [{"tag": "journal"}, {"tag": {"$nin": ["closed"]}}]}}), vec!["j1"]),
        (json!({"journals": {"project": {"$ne": "awm"}}}), vec!["j3", "p1", "w1"]),
        (json!({"journals": {"ts": {"$gte": "2026-04-01", "$lt": "2026-06-01"}}}), vec!["j2", "p1"]),
        // text operators nested under and/or/nor
        (json!({"text": {"$and": [{"$contains": "gateway"}, {"$contains": "LISTENS"}]}}), vec!["w1"]),
        (json!({"text": {"$or": [{"$contains": "listens"}, {"$contains": "caps"}]}}), vec!["j3", "w1"]),
        (json!({"text": {"$nor": [{"$contains": "gateway"}]}}), vec!["j3"]),
        (json!({"text": {"$and": [{"$contains": "8443"}, {"$nor": [{"$contains": "port moved"}]}]}}), vec!["j1", "p1", "w1"]),
        (json!({"text": {"$contains": "gateway"}, "journals": {"tag": "journal"}}), vec!["j1"]),
    ];
    for (query, want) in cases {
        assert_eq!(exact(&r, query.clone(), 50).await, s(&want), "{query}");
    }
    for filter in [json!({"tag": ["journal", "closed"]}), json!({"tag": {"$regex": "jour"}}), json!({"nonesuch": 1}), json!({"$sort": "nonesuch"})] {
        assert_eq!(r.search.query(&json!({"query": {"journals": filter}}), None, None).await.unwrap_err().class, "QueryError");
    }
}

#[tokio::test]
async fn a_string_query_splits_by_phrase_into_exact_and_related() {
    let r = loaded().await;
    let a = &r.ask(json!({"query": "port 8443"})).await["journals"];
    assert_eq!(set(&a["exact"]), s(&["j1", "p1", "w1"]));
    assert_eq!(set(&a["related"]), s(&["j2", "j3"]));
    let a = &r.ask(json!({"query": "gateway restart unit caps"})).await["journals"];
    assert_eq!(a["exact"], json!([]));
    assert!(set(&a["related"]).is_subset(&s(&["j1", "j2", "j3", "p1", "w1"])));
}

#[tokio::test]
async fn no_text_stops_at_max_and_sort_orders_by_a_field() {
    let r = loaded().await;
    let got = r.ask(json!({"query": {"journals": {}}, "max": 3})).await;
    assert_eq!(got["journals"]["exact"].as_array().unwrap().len(), 3);
    assert!(got["journals"].get("related").is_none());
    let newest = r.ask(json!({"query": {"journals": {"$sort": "-ts"}}, "max": 3})).await;
    assert_eq!(ids(&newest["journals"]["exact"]), ["j3", "j2", "p1"]);
    let oldest = r.ask(json!({"query": {"journals": {"$sort": "+ts", "project": "awm"}}})).await;
    assert_eq!(ids(&oldest["journals"]["exact"]), ["j1", "j2"]);
}

#[tokio::test]
async fn about_sections() {
    let r = loaded().await;
    let text = json!({"$or": [{"$about": "gateway port"}, {"$contains": "8443"}]});
    let a = &r.ask(json!({"query": {"text": text}})).await["journals"];
    assert_eq!(set(&a["exact"]), s(&["j1", "j2", "p1", "w1"]));
    assert_eq!(ids(&a["related"]), ["j3"]);
    let n = &r.ask(json!({"query": {"text": text, "journals": {"tag": "journal"}}})).await["journals"];
    assert_eq!((ids(&n["exact"]), ids(&n["related"])), (vec!["j1".to_string()], vec!["j3".to_string()]));

    let and = &r.ask(json!({"query": {"text": {"$and": [{"$about": "gateway port"}, {"$contains": "8443"}]}}})).await["journals"];
    assert_eq!(set(&and["exact"]), s(&["j1", "j2", "p1", "w1"]));
    assert!(and.get("related").is_none());

    for t in [json!({"$about": "gateway port 8443"}), json!({"$or": [{"$about": "gateway port 8443"}]})] {
        let a = &r.ask(json!({"query": {"text": t}, "max": 50})).await["journals"];
        assert_eq!(set(&a["exact"]), s(&["j1", "j2", "j3", "p1", "w1"]));
        assert!(a.get("related").is_none());
    }
    let two = &r.ask(json!({"query": {"text": {"$about": "gateway port 8443"}}, "max": 2})).await["journals"]["exact"];
    assert_eq!(two.as_array().unwrap().len(), 2);

    let top = set(&r.ask(json!({"query": {"text": {"$about": "gateway port 8443"}}, "max": 3})).await["journals"]["exact"]);
    let sorted = r.ask(json!({"query": {"text": {"$about": "gateway port 8443"}, "journals": {"$sort": "-ts"}}, "max": 3})).await;
    let hits = sorted["journals"]["exact"].as_array().unwrap().clone();
    assert_eq!(set(&json!(hits)), top);
    let ts: Vec<&str> = hits.iter().map(|h| h["ts"].as_str().unwrap()).collect();
    let mut want = ts.clone();
    want.sort_by(|a, b| b.cmp(a));
    assert_eq!(ts, want);
}

// -- sections from elsewhere ----------------------------------------------

#[tokio::test]
async fn a_live_source_answering_in_sections_has_its_passages_dropped() {
    let r = loaded().await;
    r.gateway.live("papers", "kb", json!([{"name": "kb", "mode": "live", "query": "kb_query", "get": "kb_get", "hit": ["title", "ts"],
        "fields": {"title": "content", "text": "content", "ts": "datetime", "author": "categorical"}}]));
    r.gateway.on("kb", "query", |_| Ok(json!({
        "exact": [{"id": "doi:1", "title": "A paper", "passages": [{"text": "a quoted line", "start": 10, "injected": true}]}],
        "graph": [{"id": "doi:2", "title": "Linked paper"}]})));
    r.search.refresh().await;
    let got = r.ask(json!({"query": {"text": "quoted line", "kb": {}}})).await;
    assert_eq!(ids(&got["kb"]["exact"]), ["doi:1"]);
    assert_eq!(got["kb"]["exact"][0], json!({"id": "doi:1", "title": "A paper", "ts": ""}));
    assert!(!every_ids(&got).contains("doi:2"));
    let call = r.gateway.calls.lock().unwrap().iter().find(|c| c.0 == "kb").cloned().unwrap();
    assert_eq!((call.1.as_str(), call.2.clone()), ("query", json!({"query": "quoted line", "limit": 10})));
}

#[tokio::test]
async fn a_peer_answers_in_sections_and_is_asked_for_its_own_sources() {
    let r = loaded().await;
    r.peers.add("capella", true, "homebase", |_| {
        Ok(json!({"journals": {"exact": [{"id": "r1", "title": "from afar", "ts": "2026-01-01T00:00:00Z", "passages": ["injected"], "extra": 1}], "related": []}}))
    });
    let got = r.ask(json!({"query": {"text": "zzzz", "journals@capella": {}}})).await;
    assert_eq!(got["journals@capella"]["exact"], json!([{"id": "r1", "title": "from afar", "ts": "2026-01-01T00:00:00Z"}]));
    assert_eq!(r.peers.calls.lock().unwrap()[0].2, json!({"query": {"text": "zzzz", "journals": {}}, "max": 10}));
    assert_eq!(r.peers.calls.lock().unwrap()[0].1, "search_query");
}

#[tokio::test]
async fn a_get_from_another_node_is_stamped_with_that_node() {
    let r = loaded().await;
    r.peers.add("capella", true, "homebase", |_| Ok(json!({"document": {"title": "far", "text": "body"}})));
    let got = r.search.get(&json!({"source": "journals", "id": "r1", "node": "capella"}), None, None).await.unwrap();
    assert_eq!(got["document"]["origin"], json!({"node": "capella"}));
    assert_eq!(got["document"]["title"], "far");
    assert_eq!(r.peers.calls.lock().unwrap()[0].2, json!({"source": "journals", "id": "r1"}));
}

