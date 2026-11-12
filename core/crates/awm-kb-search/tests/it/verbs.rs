//! The verbs as the gateway sees them, the judgements, and the usage log and score. Ported from
//! `test_hub_adapter.py`, `test_store_v9.py` and `test_journal_reads.py`.

use awm_kb_search::store;
use awm_plugin_sdk::{Caller, Env, Service};
use crate::common::*;
use serde_json::{json, Value};

fn env(dir: &std::path::Path) -> Env {
    Env { hub_url: "http://127.0.0.1:1".into(), service: "kb".into(), plugin: "kb".into(), node: HERE.into(), awm_dir: dir.into(), token_file: None }
}

#[tokio::test]
async fn describe_matches_the_baseline_with_the_listed_changes() {
    let r = rig().await;
    let api = awm_kb_search::register(Service::new(env(r.dir.path())), r.search.clone()).manifest();
    let functions = api["functions"].as_array().unwrap();
    let mut verbs: Vec<Value> = functions
        .iter()
        .map(|f| {
            let verb = f["tool"].as_str().unwrap().strip_prefix("search_").unwrap();
            let mut v = json!({"description": f["description"], "params": f["params"], "verb": verb});
            if let Some(s) = f.get("surfaces") {
                v["surfaces"] = s.clone();
            }
            v
        })
        .collect();
    verbs.sort_by(|a, b| a["verb"].as_str().cmp(&b["verb"].as_str()));
    let mut want: Value = serde_json::from_str(include_str!("../../../../../docs/plugin-redux/ports/baseline/search.describe.json")).unwrap();
    // The listed changes: reindex and api_export go; sources and status describe the live
    // sources; query and get name guide where they named the retired awm source, and query routes a
    // bare kind to a peer; query's first
    // line is its summary sentence.
    let list = want["verbs"].as_array_mut().unwrap();
    list.retain(|v| v["verb"] != "reindex" && v["verb"] != "api_export");
    for v in list.iter_mut() {
        let d = v["description"].as_str().unwrap().to_string();
        let d = match v["verb"].as_str().unwrap() {
            "query" => d
                .replace("such as journals, or awm for the node's tools; @node for another node.", "such as journals, or guide for how to use awm; @node for another node.\n   A bare kind this node lacks goes to the first peer that serves it.")
                .replace("Returns {source: {exact, related}}.", "Returns {source: {exact, related, node?}}; node names the peer a bare kind went to.")
                .replace("search query \u{2014} rank items from named sources.", "Rank hits from named sources. Open a hit with `get`."),
            "get" => d.replace("and an awm hit is its verb's describe.", "and a guide hit is its section of a doc or its verb's describe."),
            "sources" | "status" => functions.iter().find(|f| f["tool"] == format!("search_{}", v["verb"].as_str().unwrap())).unwrap()["description"].as_str().unwrap().to_string(),
            _ => d,
        };
        v["description"] = json!(d);
    }
    assert_eq!(json!({"plugin": "search", "tool": "search", "verbs": verbs}), want);
    let text = functions[0]["description"].as_str().unwrap();
    assert_eq!(text.lines().count(), 14);
    assert!(text.starts_with("Rank hits from named sources. Open a hit with `get`.\n") && text.ends_with("Open one with `get`.\n"));
    for name in ["sources", "status"] {
        let d = functions.iter().find(|f| f["tool"] == format!("search_{name}")).unwrap()["description"].as_str().unwrap();
        for gone in ["sync", "frozen", "sweep", "subscription"] {
            assert!(!d.contains(gone), "{name}: {d}");
        }
        assert!(d.contains("last answer and error"));
    }
    assert!(functions.iter().find(|f| f["tool"] == "search_sources").unwrap()["description"].as_str().unwrap().contains("`default`"));
    assert_eq!(functions[0]["timeout"], json!(60.0));
    let mcp: Vec<&str> = functions.iter().filter(|f| f.get("surfaces").is_none()).map(|f| f["name"].as_str().unwrap()).collect();
    assert_eq!(mcp, ["query", "get", "sources"]);
}

#[tokio::test]
async fn the_handlers_answer_through_the_service() {
    let r = rig().await;
    r.journals.put(&[("j1", doc("Sink", "kitchen sink journal", "2026-03-01T10:00:00Z"))]).await;
    let t = awm_kb_search::register(Service::new(env(r.dir.path())), r.search.clone()).into_test().await.unwrap();
    let out = t.call("query", json!({"query": {"text": "sink", "journals": {"tag": {"$ne": "x"}}}, "max": 3})).await.unwrap();
    assert_eq!(ids(&out["journals"]["exact"]), ["j1"]);
    let e = t.call("query", json!({"query": {"journals": {"nonesuch": 1}}})).await.unwrap_err();
    assert!(e.error_class == "QueryError" && e.message.contains("journals"), "{e:?}");
    let e = t.call("query", json!({"query": "sink", "sources": ["journals"], "limit": 5})).await.unwrap_err();
    assert_eq!(e.error_class, "QueryError");

    let peer = Caller { node: "shaula".into(), ..Caller::default() };
    let out = t.call_as("query", json!({"query": "kitchen sink"}), peer.clone(), Some("peer:shaula")).await.unwrap();
    assert_eq!(ids(&out["journals"]["exact"]), ["j1"]);
    let got = t.call_as("get", json!({"source": "journals", "id": "j1"}), peer, Some("peer:shaula")).await.unwrap();
    assert_eq!(got["document"]["origin"], json!({"node": HERE}));
    assert_eq!(got["document"]["frozen"], false);
}

#[tokio::test]
async fn sources_lists_fields_default_and_health() {
    let r = rig_with(vec![Fake::new("guide", Act::Hits(json!([])))]).await;
    r.journals.put(&[("j1", doc("Sink", "kitchen sink journal", "2026-03-01T10:00:00Z"))]).await;
    r.peers.add("altair", true, "homebase", |_| Ok(lists(&["journals", "papers"])));
    r.peers.add("vega", true, "homebase", |_| Err(awm_kb_search::CallError::new("ConnectionError", "unreachable")));
    let out = r.search.sources(None).await;
    let rows: std::collections::BTreeMap<String, Value> = out["sources"].as_array().unwrap().iter().map(|s| (s["name"].as_str().unwrap().to_string(), s.clone())).collect();
    assert_eq!(rows.keys().collect::<Vec<_>>(), ["guide", "journals", "journals@altair", "papers@altair"]);
    let j = &rows["journals"];
    assert_eq!((j["kind"].clone(), j["node"].clone(), j["count"].clone(), j["default"].clone()), (json!("journals"), json!(HERE), json!(1), json!(true)));
    assert_eq!((j["fields"]["tag"].clone(), j["fields"]["text"].clone()), (json!("categorical"), json!("content")));
    assert_eq!((j["fields"]["scope"].clone(), j["hit"].clone()), (json!({"class": "categorical", "nested": "/"}), json!(["title", "ts"])));
    for gone in ["last_sync", "frozen", "cursor", "epoch", "layout", "rebuilt"] {
        assert!(j.get(gone).is_none(), "{gone}");
    }
    assert!(j.get("last_answer").is_some() && j.get("last_error").is_some());
    assert_eq!(rows["guide"]["default"], false);
    let p = &rows["papers@altair"];
    assert_eq!((p["kind"].clone(), p["node"].clone(), p["mode"].clone(), p["default"].clone()), (json!("papers"), json!("altair"), json!("peer"), json!(false)));
    assert_eq!(p["fields"]["author"], "categorical");
    assert_eq!(out["peers"], json!({"altair": {"kinds": ["journals", "papers"]}, "vega": {"error": "ConnectionError: unreachable"}}));
    for key in ["semantic", "documents"] {
        assert!(out.get(key).is_some(), "{key}");
    }
    let relayed = r.search.sources(Some("peer:vega")).await;
    assert_eq!(relayed["sources"].as_array().unwrap().len(), 2, "a peer caller sees this node's sources alone");
    assert!(relayed.get("peers").is_none());
    let status = r.search.status(None).await;
    assert_eq!(status["sources"], json!({"journals": 1, "guide": null}));
    assert_eq!(status["semantic"], true);
}

#[tokio::test]
async fn judgements_are_recorded_replaced_and_cleared() {
    let r = rig().await;
    let s = &r.search;
    let mark = json!({"query": "kitchen sink", "source": "journals", "id": "j1", "verdict": "relevant", "section": "exact", "set": "gold"});
    s.judge(&mark, Some("scope:awm/svc-kb")).await.unwrap();
    let mut m2 = mark.clone();
    m2["verdict"] = json!("irrelevant");
    m2["section"] = json!("");
    let got = s.judge(&m2, Some("tony")).await.unwrap();
    assert_eq!(got["judged"]["judge"], "tony");
    let mut m3 = mark.clone();
    m3["id"] = json!("j2");
    s.judge(&m3, None).await.unwrap();
    let rows = s.judgements(&json!({"set": "gold"})).await.unwrap()["judgements"].clone();
    let got: Vec<(String, String, String, String)> = rows
        .as_array()
        .unwrap()
        .iter()
        .map(|r| [r["doc_id"].clone(), r["verdict"].clone(), r["section"].clone(), r["judge"].clone()].map(|v| v.as_str().unwrap().to_string()).into())
        .collect();
    assert_eq!(got, [("j1".into(), "irrelevant".into(), "".into(), "tony".into()), ("j2".into(), "relevant".into(), "exact".into(), "".into())]);
    let mut clear = mark.clone();
    clear["verdict"] = json!("clear");
    assert_eq!(s.judge(&clear, None).await.unwrap()["cleared"], 1);
    assert_eq!(ids(&json!(s.judgements(&json!({})).await.unwrap()["judgements"].as_array().unwrap().iter().map(|r| json!({"id": r["doc_id"]})).collect::<Vec<_>>())), ["j2"]);
    assert_eq!(s.judgements(&json!({"set": "t5"})).await.unwrap()["judgements"], json!([]));

    let base = json!({"query": "q", "source": "journals", "id": "j1", "verdict": "relevant"});
    let with_ = |k: &str, v: &str| {
        let mut b = base.clone();
        b[k] = json!(v);
        b
    };
    let e = s.judge(&with_("verdict", "maybe"), None).await.unwrap_err();
    assert_eq!(e.message, "verdict: 'maybe' is not relevant, irrelevant or clear");
    let e = s.judge(&with_("section", "abstract"), None).await.unwrap_err();
    assert_eq!(e.message, "section: 'abstract' is not exact or related");
    let e = s.judge(&with_("id", ""), None).await.unwrap_err();
    assert_eq!((e.class.as_str(), e.message.as_str()), ("ValueError", "search judge needs a query, a source and an id"));
}

#[tokio::test]
async fn a_get_is_logged_with_the_callers_project_and_scope() {
    let r = rig().await;
    r.journals.put(&[("j1", doc("Sink", "kitchen sink journal", "2026-03-01T10:00:00Z"))]).await;
    r.search.get(&json!({"source": "journals", "id": "j1", "query": " sink ", "_caller_scope": "awm/redux/search"}), None, None).await.unwrap();
    r.search.get(&json!({"source": "journals", "id": "j1"}), None, None).await.unwrap();
    let rows = r.search.store().read(store::usage).await.unwrap();
    assert_eq!(Value::Object(rows[0].clone()), json!({"project": "awm", "scope": "redux/search", "query": "sink", "source": "journals", "doc_id": "j1", "node": "local"}));
    assert_eq!(Value::Object(rows[1].clone()), json!({"project": "", "scope": "", "query": "", "source": "journals", "doc_id": "j1", "node": "local"}));
    // A get that finds nothing is still feedback, as the Python logged it first.
    let e = r.search.get(&json!({"source": "nonesuch", "id": "x"}), None, None).await.unwrap_err();
    assert_eq!(e.message, "no source 'nonesuch'");
    assert_eq!(r.search.store().read(store::usage).await.unwrap().len(), 3);
}

#[test]
fn the_database_keeps_the_python_schema_at_v9() {
    let dir = tempfile::tempdir().unwrap();
    let st = store::open(dir.path()).unwrap();
    let tables: Vec<String> = st
        .blocking_read(|c| {
            let mut q = c.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")?;
            let rows = q.query_map([], |r| r.get(0))?.collect::<Result<Vec<String>, _>>();
            rows.map_err(awm_store::StoreError::from)
        })
        .unwrap();
    assert_eq!(tables, ["judgements", "schema_version", "sources", "usage", "usage_score"]);
    let v: i64 = st.blocking_read(|c| c.query_row("SELECT version FROM schema_version", [], |r| r.get(0)).map_err(awm_store::StoreError::from)).unwrap();
    assert_eq!(v, 9);
    drop(st);
    store::open(dir.path()).unwrap();
}
