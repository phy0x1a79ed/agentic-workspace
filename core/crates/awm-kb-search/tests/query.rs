//! The request parser: short forms, text normalisation, node split, relayed calls and every
//! rejection. Ported from `test_query.py`.

mod common;

use awm_index::grammar::Node;
use awm_index::Veil;
use awm_kb_search::request::Request;
use common::*;
use serde_json::{json, Value};

async fn parse(r: &Rig, args: Value) -> Result<Request, String> {
    r.search.parse(&args, None).await.map_err(|e| {
        assert_eq!(e.class, "QueryError");
        e.message
    })
}

async fn bad(r: &Rig, query: Value, fragment: &str) {
    let e = parse(r, json!({"query": query})).await.expect_err(&format!("{query} parsed"));
    assert!(e.contains(fragment), "{e:?} lacks {fragment:?}");
}

fn lit(s: &str) -> Node {
    Node::Lit(s.into())
}

fn op(o: &str, items: Vec<Node>) -> Node {
    Node::Op(o.into(), items)
}

#[tokio::test]
async fn short_forms() {
    let r = rig().await;
    let req = parse(&r, json!({"query": "trigram budget"})).await.unwrap();
    assert_eq!(req.sources.iter().map(|s| (s.kind.as_str(), s.node.as_str())).collect::<Vec<_>>(), [("journals", "")]);
    assert!(req.sources[0].filter.filter.is_empty());
    let t = req.text.unwrap();
    assert_eq!((t.about.as_deref(), t.under_or, t.contains, t.raw), (Some("trigram budget"), true, Some(lit("trigram budget")), json!("trigram budget")));
    assert_eq!((req.max, req.relayed, req.remote.len(), req.elsewhere.len()), (10, false, 0, 0));

    let req = parse(&r, json!({"query": {"text": {"$contains": "HNSW"}}})).await.unwrap();
    assert_eq!(req.sources[0].name, "journals");
    let t = req.text.unwrap();
    assert_eq!((t.about, t.contains), (None, Some(lit("HNSW"))));

    let req = parse(&r, json!({"query": {"journals": {"project": "awm", "tag": "debrief"}}})).await.unwrap();
    assert!(req.text.is_none());
    assert_eq!(Value::Object(req.sources[0].filter.filter.clone()), json!({"project": "awm", "tag": "debrief"}));

    let req = parse(&r, json!({"query": "{\"text\": \"x\", \"journals\": {\"scope\": \"svc-*\"}}"})).await.unwrap();
    assert_eq!(Value::Object(req.sources[0].filter.filter.clone()), json!({"scope": "svc-*"}));
    assert_eq!(req.text.unwrap().about.as_deref(), Some("x"));
}

#[tokio::test]
async fn text_normalisation() {
    let r = rig().await;
    let text = |v: Value| {
        let r = &r;
        async move { parse(r, json!({"query": {"text": v}})).await.unwrap().text.unwrap() }
    };
    let t = text(json!({"$about": "latency"})).await;
    assert_eq!((t.about.as_deref(), t.under_or, t.contains), (Some("latency"), false, None));
    let t = text(json!({"$or": [{"$about": "latency"}]})).await;
    assert_eq!((t.about.as_deref(), t.under_or, t.contains), (Some("latency"), false, None));
    let t = text(json!({"$or": [{"$about": "a"}, {"$contains": "p95"}]})).await;
    assert_eq!((t.about.as_deref(), t.under_or, t.contains), (Some("a"), true, Some(lit("p95"))));
    let t = text(json!({"$and": [{"$about": "a"}, {"$contains": "p95"}, {"$or": [{"$contains": "x"}, {"$contains": "y"}]}]})).await;
    assert!(t.about.as_deref() == Some("a") && !t.under_or);
    assert_eq!(t.contains, Some(op("and", vec![lit("p95"), op("or", vec![lit("x"), lit("y")])])));
    let t = text(json!({"$and": [{"$contains": "p95"}, {"$about": "a"}]})).await;
    assert_eq!((t.contains, t.under_or), (Some(lit("p95")), false));
    let t = text(json!({"$and": [{"$contains": "a"}, {"$contains": "b"}]})).await;
    assert_eq!((t.about, t.under_or, t.contains), (None, false, Some(op("and", vec![lit("a"), lit("b")]))));
    let t = text(json!({"$nor": [{"$contains": "a"}, {"$contains": "b"}]})).await;
    assert_eq!(t.contains, Some(op("nor", vec![lit("a"), lit("b")])));
    let t = text(json!({"$and": [{"$about": "a"}, {"$nor": [{"$contains": "z"}]}]})).await;
    assert_eq!(t.contains, Some(op("nor", vec![lit("z")])));
    let raw = json!({"$or": [{"$about": "a"}, {"$contains": "b"}]});
    assert_eq!(text(raw.clone()).await.raw, raw);
}

#[tokio::test]
async fn sources_and_nodes() {
    let r = rig().await;
    let req = parse(&r, json!({"query": {"journals": {"project": "awm"}, "kb@altair": {"author": "Smith"}, "transcripts@vega": {}}})).await.unwrap();
    assert_eq!(req.sources.iter().map(|s| s.name.as_str()).collect::<Vec<_>>(), ["journals"]);
    assert_eq!(req.remote.keys().collect::<Vec<_>>(), ["altair", "vega"]);
    assert_eq!((req.remote["altair"][0].kind.as_str(), req.remote["altair"][0].node.as_str()), ("kb", "altair"));

    let req = parse(&r, json!({"query": {format!("journals@{HERE}"): {"scope": "x"}}})).await.unwrap();
    assert_eq!((req.sources[0].name.clone(), req.sources[0].node.clone(), req.remote.len()), (format!("journals@{HERE}"), String::new(), 0));

    bad(&r, json!({"journals": {}, format!("journals@{HERE}"): {}}), "named twice").await;
    bad(&r, json!({"kb@altair": {}, "kb@Altair": {}}), "named twice").await;

    let req = parse(&r, json!({"query": {"journals": {}, "journals@altair": {}}})).await.unwrap();
    assert_eq!((req.sources.len(), req.remote["altair"].len()), (1, 1));

    let req = parse(&r, json!({"query": {"text": {"$contains": "x"}, "journals": {"project": "awm"},
        "journals@altair": {"ts": {"$gte": "2026-10-01"}}, "kb@altair": {"author": "A"}}, "max": 20})).await.unwrap();
    assert_eq!(req.forwarded("altair"), json!({
        "query": {"text": {"$contains": "x"}, "journals": {"ts": {"$gte": "2026-10-01T00:00:00Z"}}, "kb": {"author": "A"}},
        "max": 20}));
    let req = parse(&r, json!({"query": {"kb@altair": {}}})).await.unwrap();
    assert_eq!(req.forwarded("altair"), json!({"query": {"kb": {}}, "max": 10}));
    let req = parse(&r, json!({"query": {"journals@altair": {"$sort": "-ts"}}})).await.unwrap();
    assert_eq!(req.forwarded("altair")["query"]["journals"], json!({"$sort": "-ts"}));
}

#[tokio::test]
async fn relayed_calls_and_the_veil() {
    let r = rig().await;
    let req = r.search.parse(&json!({"query": {"journals": {}, "kb@altair": {}, "journals@vega": {}}}), Some("peer:altair")).await.unwrap();
    assert!(req.relayed && req.remote.is_empty());
    assert_eq!(req.elsewhere, ["kb@altair", "journals@vega"]);
    assert_eq!(req.sources.iter().map(|s| s.name.as_str()).collect::<Vec<_>>(), ["journals"]);
    assert!(matches!(req.veil, Veil::Marks { .. } | Veil::Unreadable));
    let req = r.search.parse(&json!({"query": "x"}), Some("session")).await.unwrap();
    assert!(!req.relayed && req.veil == Veil::None);
}

#[tokio::test]
async fn max_and_arguments() {
    let r = rig().await;
    for (value, want) in [(json!(1), 1), (json!(100), 100), (json!("20"), 20), (Value::Null, 10)] {
        assert_eq!(parse(&r, json!({"query": "x", "max": value})).await.unwrap().max, want);
    }
    for value in [json!(0), json!(101), json!(-1), json!(true), json!(2.5), json!("ten"), json!(""), json!([5])] {
        let e = parse(&r, json!({"query": "x", "max": value})).await.unwrap_err();
        assert!(e.starts_with("max: ") && e.ends_with("allowed: 1-100"), "{e}");
    }
    let e = parse(&r, json!({"query": "x", "sources": ["journals"]})).await.unwrap_err();
    assert!(e.starts_with("sources: unknown argument; allowed: query, max"), "{e}");
    assert_eq!(parse(&r, json!({"query": "x", "_caller_scope": "awm/redux"})).await.unwrap().max, 10);
    assert!(parse(&r, json!({})).await.unwrap_err().starts_with("query: missing"));
}

#[tokio::test]
async fn ts_is_normalised_everywhere() {
    let r = rig().await;
    let f = json!({"ts": {"$gte": "2026-03-01", "$lt": "2026-03-01T12:00:00+02:00", "$in": ["2026-03-02 05:00"]}});
    let req = parse(&r, json!({"query": {"journals": {"$or": [f, {"$and": [{"ts": "2026-04-01"}]}]}}})).await.unwrap();
    let or = &req.sources[0].filter.filter["$or"];
    assert_eq!(or[0]["ts"], json!({"$gte": "2026-03-01T00:00:00Z", "$lt": "2026-03-01T10:00:00Z", "$in": ["2026-03-02T05:00:00Z"]}));
    assert_eq!(or[1]["$and"][0]["ts"], json!("2026-04-01T00:00:00Z"));
    for filt in [json!({"ts": "yesterday"}), json!({"ts": {"$gt": "soon"}}), json!({"ts": {"$in": ["2026-01-01", "nope"]}}),
                 json!({"$or": [{"ts": "x"}]}), json!({"$nor": [{"$and": [{"ts": {"$lte": "13/01/2026"}}]}]}), json!({"ts": 5})] {
        let e = parse(&r, json!({"query": {"journals": filt}})).await.unwrap_err();
        assert!(e.contains("ts") || e.contains("ISO 8601") || e.contains("scalar"), "{e}");
    }
    bad(&r, json!({"journals": {"$or": [{"ts": {"$in": ["2026-01-01", "nope"]}}]}}), "query.journals.$or[0].ts.$in[1]: 'nope' is not an ISO 8601").await;
}

#[tokio::test]
async fn rejections() {
    let r = rig().await;
    bad(&r, json!({"journals": {"tag": ["a", "b"]}}), "query.journals.tag: a bare list").await;
    bad(&r, json!({"journals": {"tag": {"$regex": "x"}}}), "query.journals.tag.$regex: unknown operator; allowed: $eq").await;
    bad(&r, json!({"journals": {"$gt": 3}}), "query.journals.$gt: operator not allowed at a field position; allowed: $and").await;
    bad(&r, json!({"journals": {"$or": [{"$in": [1]}]}}), "$or[0].$in: operator not allowed").await;
    bad(&r, json!({"transcripts": {"tag": "x"}}), "query.transcripts.tag: transcripts has no field 'tag'; allowed: ts, project, scope").await;
    bad(&r, json!({"kb": {"project": "x"}}), "allowed: ts, author, tag, cites, cited_by").await;
    bad(&r, json!({"journals": {"$and": [{"author": "x"}]}}), "$and[0].author").await;
    for key in ["$about", "$contains"] {
        bad(&r, json!({"journals": {key: "x"}}), &format!("query.journals.{key}: {key} belongs in text")).await;
        bad(&r, json!({"journals": {"$or": [{key: "x"}]}}), &format!("$or[0].{key}")).await;
    }
    bad(&r, json!({"text": {"$or": [{"$about": "a"}, {"$about": "b"}]}}), "query.text.$or: more than one $about").await;
    bad(&r, json!({"text": {"$and": [{"$about": "a"}, {"$about": "b"}, {"$contains": "c"}]}}), "more than one").await;
    bad(&r, json!({"text": {"$and": [{"$or": [{"$about": "a"}, {"$contains": "b"}]}]}}), "query.text.$and[0].$or[0]: $about sits only").await;
    bad(&r, json!({"text": {"$nor": [{"$about": "a"}]}}), "query.text.$nor[0]: $about sits only").await;
    bad(&r, json!({"text": {"$about": "a", "$contains": "b"}}), "exactly one key").await;
    bad(&r, json!({"text": {"$and": [{"$contains": "a", "$about": "b"}]}}), "query.text.$and[0]: a text term is an object with exactly one key").await;
    bad(&r, json!({"text": {}}), "exactly one key").await;
    bad(&r, json!({"text": {"$and": [{"$regex": "a"}]}}), "$and[0].$regex: not allowed in text").await;
    for (t, frag) in [(json!({"$contains": ""}), "a letter or digit"), (json!({"$contains": 5}), "a letter or digit"),
                      (json!({"$contains": "--"}), "a letter or digit"), (json!({"$about": ""}), "non-empty string"),
                      (json!({"$and": [{"$about": 5}]}), "non-empty string"), (json!(""), "text is empty")] {
        bad(&r, json!({"text": t}), frag).await;
    }
    bad(&r, json!({"journals": {"$and": []}}), "query.journals.$and: needs a non-empty list").await;
    bad(&r, json!({"journals": {"$or": {"project": "x"}}}), "non-empty list").await;
    bad(&r, json!({"text": {"$or": []}}), "query.text.$or: needs a non-empty list").await;
    bad(&r, json!({"journals": {"project": {}}}), "query.journals.project: empty operator object").await;
    bad(&r, json!({"journals": {"tag": {"$in": []}}}), "$in: needs a non-empty list").await;
    bad(&r, json!({"journals": {"tag": {"$all": "a"}}}), "$all: needs a non-empty list").await;
    bad(&r, json!({"journals": {"project": {"$eq": {"a": 1}}}}), "project.$eq: dict is not a scalar").await;
    for v in [json!(true), Value::Null] {
        bad(&r, json!({"journals": {"project": v}}), "not a scalar").await;
        bad(&r, json!({"journals": {"project": {"$in": [v]}}}), "$in[0]").await;
    }
    // The kinds a query may name: kb's own sources and the declared ones; library declares no fields.
    bad(&r, json!({"text@altair": {}}), "query.text@altair: not a source; allowed: journals, kb, transcripts").await;
    bad(&r, json!({"notes": {}}), "query.notes: not a source; allowed: journals, kb, transcripts").await;
    bad(&r, json!({"library": {}}), "not a source").await;
    for key in ["journals@", "journals@a b", "journals@@x", "kb@x/y"] {
        bad(&r, json!({key: {}}), "malformed node after @").await;
    }
    bad(&r, json!({"journals": "awm"}), "query.journals: a source filter is an object").await;
    bad(&r, json!({"journals": {"$and": ["x"]}}), "$and[0]: a source filter is an object").await;
    for q in [json!({}), json!([]), json!(5), Value::Null, json!(""), json!("   ")] {
        let e = parse(&r, json!({"query": q})).await.unwrap_err();
        assert!(e.starts_with("query: "), "{e}");
    }
    bad(&r, json!("{\"text\": "), "query: not valid JSON").await;
}

#[tokio::test]
async fn duplicate_keys_are_found_only_in_json_text() {
    let r = rig().await;
    let text_form = "{\"journals\": {\"project\": \"a\", \"project\": \"b\"}}";
    bad(&r, json!(text_form), "query.journals.project: duplicate key").await;
    bad(&r, json!("{\"journals\": {}, \"journals\": {}}"), "query.journals: duplicate key").await;
    bad(&r, json!("{\"journals\": {\"$or\": [{\"tag\": \"a\", \"tag\": \"b\"}]}}"), "$or[0].tag: duplicate key").await;
    bad(&r, json!("{\"text\": {\"$or\": [{\"$about\": \"a\", \"$about\": \"b\"}]}}"), "duplicate key").await;
    let parsed: Value = serde_json::from_str(text_form).unwrap();
    let req = parse(&r, json!({"query": parsed})).await.unwrap();
    assert_eq!(Value::Object(req.sources[0].filter.filter.clone()), json!({"project": "b"}));
}

#[tokio::test]
async fn the_plans_examples_parse() {
    let r = rig().await;
    let examples = [
        json!({"query": "trigram budget"}),
        json!({"query": {"journals": {"project": "awm", "scope": "redux-dev", "tag": "debrief"}}}),
        json!({"query": {"text": "trigram budget", "journals": {"project": "awm", "scope": "svc-*"}}}),
        json!({"query": {"text": {"$and": [{"$about": "search latency at 10x scale"}, {"$or": [{"$contains": "p95"}, {"$contains": "timeout"}]}]},
                         "journals": {"project": "awm"}, "journals@altair": {"ts": {"$gte": "2026-10-01"}}}}),
        json!({"query": {"text": {"$contains": "HNSW"}, "journals": {"replies_to": "<post id>"}}, "max": 20}),
    ];
    for args in &examples {
        let req = parse(&r, args.clone()).await.unwrap();
        assert!(!req.sources.is_empty() && [10, 20].contains(&req.max));
    }
    let req = parse(&r, examples[3].clone()).await.unwrap();
    let t = req.text.unwrap();
    assert!(t.about.as_deref() == Some("search latency at 10x scale") && !t.under_or);
    assert_eq!(t.contains, Some(op("or", vec![lit("p95"), lit("timeout")])));
    assert_eq!(Value::Object(req.remote["altair"][0].filter.filter.clone()), json!({"ts": {"$gte": "2026-10-01T00:00:00Z"}}));
}

#[tokio::test]
async fn guide_style_defaults_follow_the_sources() {
    // A node with no default source still answers a string query on journals, by name.
    let r = rig_with(vec![Fake::new("guide", Act::Hits(json!([])))]).await;
    let req = parse(&r, json!({"query": "x"})).await.unwrap();
    assert_eq!(req.sources.iter().map(|s| s.kind.as_str()).collect::<Vec<_>>(), ["journals"]);
    bad(&r, json!({"nope": {}}), "allowed: guide, journals, kb, transcripts").await;
}
