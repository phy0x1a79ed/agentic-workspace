//! The exact section through a kb source on awm-index: phrase matching, filters, `$sort`, the
//! sections of an answer, and sections from elsewhere. Ported from `test_exact.py`; the feed
//! reads are gone, so the source is written directly.

mod common;

use common::*;
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
async fn phrases() {
    let r = loaded().await;
    for literal in ["3f9a2c1d7e", "/etc/awm/env", "[Errno 111] Connection refused", "AWM_GATEWAY_PORT=8443"] {
        let got = r.ask(json!({"query": contains(literal)})).await;
        assert_eq!(ids(&got["journals"]["exact"]), ["j1"], "{literal}");
        let keys: BTreeSet<&String> = got["journals"]["exact"][0].as_object().unwrap().keys().collect();
        assert_eq!(keys, ["id", "title", "ts"].iter().map(|k| k.to_string()).collect::<BTreeSet<_>>().iter().collect());
        assert!(got["journals"].get("related").is_none());
    }
    assert_eq!(exact(&r, contains("3f9a2c1d7e.\n\n## Fix"), 10).await, s(&["j1"]));
    assert_eq!(exact(&r, contains("Connection-refused"), 10).await, s(&["j1"]));
    for literal in ["ConnectionRefusedError", "connectionrefusederror"] {
        assert_eq!(exact(&r, contains(literal), 10).await, s(&["j1", "j3"]));
    }
    assert_eq!(exact(&r, contains("3f9a"), 10).await, s(&[]));
    assert_eq!(exact(&r, contains("8443 port"), 10).await, s(&[]));
    assert_eq!(exact(&r, contains("Errno"), 10).await, s(&["j1"]));
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
async fn filters_follow_mongodb_semantics() {
    let r = loaded().await;
    let cases = [
        (json!({"tag": "precedent"}), vec!["p1"]),
        (json!({"tag": "PRECEDENT"}), vec![]),
        (json!({"tag": "journal"}), vec!["j1", "j3"]),
        (json!({"tag": "jour*"}), vec!["j1", "j3"]),
        (json!({"tag": {"$in": ["message", "precedent"]}}), vec!["j2", "p1"]),
        (json!({"tag": {"$all": ["journal", "closed"]}}), vec!["j3"]),
        (json!({"tag": {"$all": ["journal", "message"]}}), vec![]),
        (json!({"tag": {"$nin": ["closed"]}}), vec!["j1", "j2", "p1", "w1"]),
        (json!({"tag": {"$ne": "journal"}}), vec!["j2", "p1", "w1"]),
        (json!({"$nor": [{"tag": "journal"}]}), vec!["j2", "p1", "w1"]),
        (json!({"$or": [{"tag": "message"}, {"tag": "closed"}]}), vec!["j2", "j3"]),
        (json!({"$and": [{"tag": "journal"}, {"tag": {"$nin": ["closed"]}}]}), vec!["j1"]),
        (json!({"project": {"$ne": "awm"}}), vec!["j3", "p1", "w1"]),
        (json!({"ts": {"$gte": "2026-04-01", "$lt": "2026-06-01"}}), vec!["j2", "p1"]),
        (json!({"ts": {"$gt": "2026-04-01T10:00:00Z"}}), vec!["j2", "j3"]),
    ];
    for (filter, want) in cases {
        assert_eq!(exact(&r, json!({"journals": filter}), 50).await, s(&want), "{filter}");
    }
    for filter in [json!({"tag": ["journal", "closed"]}), json!({"tag": {"$regex": "jour"}}), json!({"tag": {"$in": "journal"}}),
                   json!({"nonesuch": 1}), json!({"text": "x"}), json!({"$sort": "nonesuch"}), json!({"$and": [{"$sort": "ts"}]})] {
        assert_eq!(r.search.query(&json!({"query": {"journals": filter}}), None, None).await.unwrap_err().class, "QueryError");
    }
}

#[tokio::test]
async fn a_comparison_on_a_categorical_is_an_error_on_its_source() {
    let r = loaded().await;
    let got = r.ask(json!({"query": {"journals": {"tag": {"$gt": "a"}}}})).await;
    assert_eq!(got["journals"]["exact"], json!([]));
    let e = got["journals"]["error"].as_str().unwrap();
    assert!(e.starts_with("query.journals: ") && e.contains("$gt"), "{e}");
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
async fn sort_on_content_orders_by_match_score() {
    let r = loaded().await;
    let text = json!({"$or": [{"$contains": "gateway"}, {"$contains": "port"}]});
    let best = ids(&r.ask(json!({"query": {"text": text, "journals": {"$sort": "-text"}}, "max": 50})).await["journals"]["exact"]);
    let mut worst = ids(&r.ask(json!({"query": {"text": text, "journals": {"$sort": "+text"}}, "max": 50})).await["journals"]["exact"]);
    worst.reverse();
    assert_eq!(best, worst);
    let unscored = r.ask(json!({"query": {"journals": {"$sort": "-text"}}})).await;
    assert_eq!(unscored["journals"]["exact"], json!([]));
    assert!(unscored["journals"]["error"].as_str().unwrap().contains("needs a $contains"));
}

#[tokio::test]
async fn contains_nests_under_and_or_nor() {
    let r = loaded().await;
    let cases = [
        (json!({"$and": [{"$contains": "gateway"}, {"$contains": "8443"}]}), vec!["j1", "j2", "p1", "w1"]),
        (json!({"$and": [{"$contains": "gateway"}, {"$contains": "LISTENS"}]}), vec!["w1"]),
        (json!({"$or": [{"$contains": "listens"}, {"$contains": "caps"}]}), vec!["j3", "w1"]),
        (json!({"$nor": [{"$contains": "gateway"}]}), vec!["j3"]),
        (json!({"$and": [{"$contains": "8443"}, {"$nor": [{"$contains": "port moved"}]}]}), vec!["j1", "p1", "w1"]),
        (json!({"$or": [{"$contains": "caps"}, {"$and": [{"$contains": "port"}, {"$contains": "prefer"}]}]}), vec!["j3", "p1"]),
        (json!({"$or": [{"$contains": "listens"}, {"$nor": [{"$contains": "gateway"}]}]}), vec!["j3", "w1"]),
    ];
    for (text, want) in cases {
        assert_eq!(exact(&r, json!({"text": text}), 50).await, s(&want), "{text}");
    }
    assert_eq!(exact(&r, json!({"text": {"$contains": "gateway"}, "journals": {"tag": "journal"}}), 10).await, s(&["j1"]));
}

#[tokio::test]
async fn scope_covers_nested_scopes_and_globs_and_replies_list() {
    let r = rig().await;
    r.journals
        .put(&[
            ("a", with(doc("A", "x", "2026-01-01T00:00:00Z"), json!({"project": "awm", "scope": "redux"}))),
            ("b", with(doc("B", "x", "2026-01-02T00:00:00Z"), json!({"project": "awm", "scope": "redux/search"}))),
            ("c", with(doc("C", "x", "2026-01-03T00:00:00Z"), json!({"project": "awm", "scope": "redux-dev"}))),
            ("d", with(doc("D", "x", "2026-01-04T00:00:00Z"), json!({"project": "other", "scope": "redux"}))),
            ("root", doc("Root", "question", "2025-01-01T00:00:00Z")),
            ("r1", with(doc("Re", "answer", "2025-01-02T00:00:00Z"), json!({"replies_to": ["root"]}))),
            ("r2", with(doc("Re2", "another", "2025-01-03T00:00:00Z"), json!({"replies_to": ["root", "r1"]}))),
            ("r3", with(doc("Re3", "elsewhere", "2025-01-04T00:00:00Z"), json!({"replies_to": ["r1"]}))),
        ])
        .await;
    let nested = r.ask(json!({"query": {"journals": {"scope": "redux", "project": "awm", "$sort": "-ts"}}})).await;
    assert_eq!(ids(&nested["journals"]["exact"]), ["b", "a"]);
    assert_eq!(exact(&r, json!({"journals": {"scope": "redux*", "project": "awm"}}), 10).await, s(&["a", "b", "c"]));
    assert_eq!(exact(&r, json!({"journals": {"scope": "redux/*"}}), 10).await, s(&["b"]));
    assert_eq!(exact(&r, json!({"journals": {"scope": {"$ne": "redux"}, "project": {"$in": ["awm", "other"]}}}), 10).await, s(&["c"]));
    let replies = r.ask(json!({"query": {"journals": {"replies_to": "root", "$sort": "-ts"}}})).await;
    assert_eq!(ids(&replies["journals"]["exact"]), ["r2", "r1"]);
    assert_eq!(exact(&r, json!({"text": {"$contains": "answer"}, "journals": {"replies_to": {"$in": ["root"]}}}), 10).await, s(&["r1"]));
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

#[tokio::test]
async fn an_edit_replaces_what_a_phrase_finds() {
    let r = rig().await;
    r.journals.put(&[("j1", doc("Notes", LOG, "2026-03-01T10:00:00Z"))]).await;
    r.journals.put(&[("j1", doc("Notes", "replaced body with deadbeefcafe", "2026-03-01T10:00:00Z"))]).await;
    assert_eq!(exact(&r, contains("3f9a2c1d7e"), 10).await, s(&[]));
    assert_eq!(exact(&r, contains("deadbeefcafe"), 10).await, s(&["j1"]));
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
async fn a_failing_live_source_reports_its_error_and_the_rest_answer() {
    let r = loaded().await;
    r.gateway.live("papers", "kb", json!([{"name": "kb", "mode": "live", "query": "kb_query"}]));
    r.gateway.on("kb", "query", |_| Err(awm_kb_search::CallError::new("RuntimeError", "kb is down")));
    r.search.refresh().await;
    let got = r.ask(json!({"query": {"text": "port", "kb": {}, "journals": {}}})).await;
    assert_eq!(got["kb"]["exact"], json!([]));
    assert!(got["kb"]["error"].as_str().unwrap().contains("kb is down"));
    assert!(got["journals"].get("error").is_none() && !got["journals"]["exact"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn a_live_source_takes_only_what_it_serves() {
    let r = loaded().await;
    r.gateway.live("papers", "kb", json!([{"name": "kb", "mode": "live", "query": "kb_query"}]));
    r.gateway.on("kb", "query", |_| Ok(json!({"hits": []})));
    r.search.refresh().await;
    let got = r.ask(json!({"query": {"text": {"$contains": "x"}, "kb": {"author": "A", "ts": {"$gte": "2024-01-01", "$lt": "2025-01-01"}}}})).await;
    assert_eq!(got["kb"], json!({"exact": []}));
    let call = r.gateway.calls.lock().unwrap().iter().rev().find(|c| c.0 == "kb").cloned().unwrap();
    assert_eq!(call.2, json!({"query": "x", "limit": 10, "author": "A", "after": "2024-01-01T00:00:00Z", "before": "2025-01-01T00:00:00Z"}));
    let got = r.ask(json!({"query": {"text": {"$contains": "x"}, "kb": {"tag": "t"}}})).await;
    assert_eq!(got["kb"]["error"], "query.kb.tag: not yet served on kb; served: author, ts with $gte and $lt");
    let got = r.ask(json!({"query": {"kb": {}}})).await;
    assert_eq!(got["kb"]["error"], "query.kb: kb needs text; it lists nothing by its fields alone yet");
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
async fn a_reply_in_another_shape_is_an_error_on_its_source() {
    let r = loaded().await;
    r.peers.add("capella", true, "homebase", |_| Ok(json!({"hits": [{"source": "journals", "id": "r1", "snippet": "from afar"}]})));
    let got = r.ask(json!({"query": {"text": "zzzz", "journals@capella": {}}})).await;
    assert_eq!(got["journals@capella"]["exact"], json!([]));
    assert_eq!(got["journals@capella"]["error"], "RuntimeError: reply names none of the sources asked");
}

#[tokio::test]
async fn a_station_and_a_node_without_an_edge_are_never_asked() {
    let r = loaded().await;
    r.peers.add("antenna", true, "station", |_| unreachable!());
    r.peers.add("faraway", false, "homebase", |_| unreachable!());
    let got = r.ask(json!({"query": {"text": "x", "journals@antenna": {}, "journals@faraway": {}}})).await;
    assert_eq!(got["journals@antenna"]["error"], "a station runs no search");
    assert_eq!(got["journals@faraway"]["error"], "no edge from local to faraway");
    assert!(r.peers.calls.lock().unwrap().is_empty());
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

#[tokio::test]
async fn grep_parity_on_sampled_phrases() {
    let r = rig().await;
    // A small deterministic generator in place of Python's random.Random(7).
    let mut seed: u64 = 7;
    let mut next = move |n: u64| {
        seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (seed >> 33) % n
    };
    let vocab = ["tap", "flow", "sink", "gateway", "capella", "error", "path", "hash", "build", "merge"];
    let mut docs = Vec::new();
    for i in 0..40 {
        let mut body = Vec::new();
        for _ in 0..(3 + next(10)) {
            let words: Vec<&str> = (0..(4 + next(17))).map(|_| vocab[next(10) as usize]).collect();
            body.push(format!("{} {:016x} /srv/{}/{i}.log", words.join(" "), next(u64::MAX), vocab[next(10) as usize]));
            if next(10) < 3 {
                body.push(String::new());
            }
        }
        docs.push((format!("d{i:02}"), format!("Doc {i}"), body.join("\n")));
    }
    let rows: Vec<(&str, Value)> = docs.iter().map(|(id, t, b)| (id.as_str(), doc(t, b, "2026-01-01T00:00:00Z"))).collect();
    r.journals.put(&rows).await;
    let words = |t: &str| awm_index::fts::words(t);
    let phrase_in = |needle: &[String], field: &str| format!(" {} ", words(field).join(" ")).contains(&format!(" {} ", needle.join(" ")));
    for _ in 0..200 {
        let (key, title, body) = &docs[next(docs.len() as u64) as usize];
        let tokens = words(body);
        let n = 1 + next(6) as usize;
        let at = next((tokens.len() - n + 1) as u64) as usize;
        let needle = tokens[at..at + n].to_vec();
        let found = exact(&r, contains(&needle.join(" ")), 100).await;
        let grep: BTreeSet<String> = docs.iter().filter(|(_, t, b)| phrase_in(&needle, t) || phrase_in(&needle, b)).map(|(k, _, _)| k.clone()).collect();
        assert!(grep.contains(key) && found == grep, "{needle:?} {title}");
    }
}
