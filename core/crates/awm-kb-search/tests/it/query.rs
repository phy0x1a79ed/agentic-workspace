//! The request parser: short forms, text normalisation, node split, relayed calls and every
//! rejection. Ported from `test_query.py`.

use awm_index::grammar::Node;
use awm_index::Veil;
use awm_kb_search::request::Request;
use crate::common::*;
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
async fn sources_and_nodes() {
    let r = rig().await;
    let req = parse(&r, json!({"query": {"journals": {"project": "awm"}, "papers@altair": {"author": "Smith"}, "transcripts@vega": {}}})).await.unwrap();
    assert_eq!(req.sources.iter().map(|s| s.name.as_str()).collect::<Vec<_>>(), ["journals"]);
    assert_eq!(req.remote.keys().collect::<Vec<_>>(), ["altair", "vega"]);
    assert_eq!((req.remote["altair"][0].kind.as_str(), req.remote["altair"][0].node.as_str()), ("papers", "altair"));

    let req = parse(&r, json!({"query": {format!("journals@{HERE}"): {"scope": "x"}}})).await.unwrap();
    assert_eq!((req.sources[0].name.clone(), req.sources[0].node.clone(), req.remote.len()), (format!("journals@{HERE}"), String::new(), 0));

    bad(&r, json!({"journals": {}, format!("journals@{HERE}"): {}}), "names the same source as").await;
    bad(&r, json!({"papers@altair": {}, "papers@Altair": {}}), "names the same source as").await;

    let req = parse(&r, json!({"query": {"journals": {}, "journals@altair": {}}})).await.unwrap();
    assert_eq!((req.sources.len(), req.remote["altair"].len()), (1, 1));

    let req = parse(&r, json!({"query": {"text": {"$contains": "x"}, "journals": {"project": "awm"},
        "journals@altair": {"ts": {"$gte": "2026-10-01"}}, "papers@altair": {"author": "A"}}, "max": 20})).await.unwrap();
    assert_eq!(req.forwarded("altair"), json!({
        "query": {"text": {"$contains": "x"}, "journals": {"ts": {"$gte": "2026-10-01T00:00:00Z"}}, "papers": {"author": "A"}},
        "max": 20}));
    let req = parse(&r, json!({"query": {"papers@altair": {}}})).await.unwrap();
    assert_eq!(req.forwarded("altair"), json!({"query": {"papers": {}}, "max": 10}));
    let req = parse(&r, json!({"query": {"journals@altair": {"$sort": "-ts"}}})).await.unwrap();
    assert_eq!(req.forwarded("altair")["query"]["journals"], json!({"$sort": "-ts"}));
}

#[tokio::test]
async fn relayed_calls_and_the_veil() {
    let r = rig().await;
    let req = r.search.parse(&json!({"query": {"journals": {}, "papers@altair": {}, "journals@vega": {}}}), Some("peer:altair")).await.unwrap();
    assert!(req.relayed && req.remote.is_empty());
    assert_eq!(req.elsewhere, ["papers@altair", "journals@vega"]);
    assert_eq!(req.sources.iter().map(|s| s.name.as_str()).collect::<Vec<_>>(), ["journals"]);
    assert!(matches!(req.veil, Veil::Marks { .. } | Veil::Unreadable));
    let req = r.search.parse(&json!({"query": "x"}), Some("session")).await.unwrap();
    assert!(!req.relayed && req.veil == Veil::None);
}

#[tokio::test]
async fn rejections() {
    let r = rig().await;
    bad(&r, json!({"journals": {"tag": ["a", "b"]}}), "query.journals.tag: a bare list").await;
    bad(&r, json!({"journals": {"tag": {"$regex": "x"}}}), "query.journals.tag.$regex: unknown operator; allowed: $eq").await;
    bad(&r, json!({"journals": {"$gt": 3}}), "query.journals.$gt: operator not allowed at a field position; allowed: $and").await;
    bad(&r, json!({"journals": {"$or": [{"$in": [1]}]}}), "$or[0].$in: operator not allowed").await;
    bad(&r, json!({"transcripts": {"tag": "x"}}), "query.transcripts.tag: transcripts has no field 'tag'; allowed: ts, project, scope").await;
    bad(&r, json!({"papers": {"project": "x"}}), "allowed: ts, author, cites, cited_by").await;
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
    // The kinds a query may name: kb's own, served here or not.
    bad(&r, json!({"text@altair": {}}), "query.text@altair: not a source; allowed: journals, papers, transcripts").await;
    bad(&r, json!({"notes": {}}), "query.notes: not a source; allowed: journals, papers, transcripts").await;
    for key in ["journals@", "journals@a b", "journals@@x", "papers@x/y", "papers@altair,capella"] {
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

