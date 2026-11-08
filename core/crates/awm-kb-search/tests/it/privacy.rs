//! Private marks: what a peer caller never sees through query, get, sources and status, and the
//! tag-clash refusal. Ported from `test_privacy.py`. The marks live in one AWM_DIR per process,
//! so the cases run in order in one test.

use awm_index::Veil;
use awm_kb_search::live;
use awm_plugin_sdk::privacy;
use crate::common::*;
use serde_json::{json, Value};
use std::collections::BTreeSet;

const PEER: Option<&str> = Some("peer:altair");
const USER: Option<&str> = Some("user:tony");

fn s(v: &[&str]) -> BTreeSet<String> {
    v.iter().map(|x| x.to_string()).collect()
}

async fn ask(r: &Rig, text: &str, as_: Option<&str>, filter: Value) -> Value {
    r.ask_as(json!({"query": {"text": text, "journals": filter}, "max": 50}), as_).await.unwrap()
}

async fn found(r: &Rig, as_: Option<&str>) -> BTreeSet<String> {
    every_ids(&ask(r, "kitchen sink", as_, json!({})).await)
}

fn clash(r: &Rig, older: &'static str, newer: &'static str) {
    r.gateway.on("journal", "tag_list", move |_| {
        Ok(json!({"tags": [], "clashes": [{"name": "ui", "older_id": "a", "newer_id": "b", "older_node": older, "newer_node": newer}]}))
    });
}

#[tokio::test]
async fn a_peer_never_sees_what_a_mark_covers() {
    let r = rig().await;
    let root = r.dir.path().join("projects");
    r.journals
        .put(&[
            ("j1", with(doc("Open", "kitchen sink in the open project", "2026-03-01T10:00:00Z"), json!({"project": "awm", "scope": "dev"}))),
            ("j2", with(doc("Avatar", "kitchen sink in the private project", "2026-03-01T10:00:00Z"), json!({"project": "avatar", "scope": "dev"}))),
            ("j5", with(doc("Sibling", "kitchen sink in the sibling project", "2026-03-01T10:00:00Z"), json!({"project": "avatar2", "scope": "dev"}))),
            ("j6", with(doc("Marked scope", "kitchen sink in a marked scope", "2026-03-01T10:00:00Z"), json!({"project": "awm", "scope": "svc-a"}))),
            ("j7", with(doc("Nested scope", "kitchen sink in a nested marked scope", "2026-03-01T10:00:00Z"), json!({"project": "awm", "scope": "svc-a/x"}))),
            ("j8", with(doc("Near scope", "kitchen sink in a scope that only starts alike", "2026-03-01T10:00:00Z"), json!({"project": "awm", "scope": "svc-ab"}))),
            ("j9", with(doc("Goal", "kitchen sink goal set on the private project's channel", "2026-03-01T10:00:00Z"), json!({"project": "avatar"}))),
            ("p1", doc("Sink", "kitchen sink precedent with no project", "2026-03-01T10:00:00Z")),
            ("p2", with(doc("Tagged", "kitchen sink precedent tagged with the private project", "2026-03-01T10:00:00Z"), json!({"tag": ["UI", "Avatar"]}))),
            ("p3", with(doc("Near tag", "kitchen sink precedent tagged with a sibling", "2026-03-01T10:00:00Z"), json!({"tag": ["avatar2"]}))),
        ])
        .await;
    privacy::write_marks(&[
        json!({"project": "avatar", "scope": null, "path": root.join("avatar").to_string_lossy()}),
        json!({"project": "awm", "scope": "svc-a", "path": root.join("awm").join("svc-a").to_string_lossy()}),
    ])
    .unwrap();
    let all = s(&["j1", "j2", "j5", "j6", "j7", "j8", "j9", "p1", "p2", "p3"]);
    let hidden = s(&["j2", "j6", "j7", "j9", "p2"]);
    let visible: BTreeSet<String> = all.difference(&hidden).cloned().collect();

    // A local or user caller sees everything; a peer, nothing marked, in either section.
    for as_ in [None, USER, Some("agent:x")] {
        assert_eq!(found(&r, as_).await, all);
    }
    for as_ in [Some("peer"), PEER] {
        assert_eq!(found(&r, as_).await, visible);
    }
    let out = ask(&r, "kitchen sink", PEER, json!({})).await;
    assert_eq!(out["journals"].as_object().unwrap().keys().collect::<Vec<_>>(), ["exact", "related"]);
    let listed = r.ask_as(json!({"query": {"text": {"$contains": "kitchen"}, "journals": {}}, "max": 50}), PEER).await.unwrap();
    assert_eq!(set(&listed["journals"]["exact"]), visible);

    // Naming a marked project finds what an unknown one finds.
    let named = ask(&r, "kitchen sink", PEER, json!({"project": "avatar"})).await;
    let unknown = ask(&r, "kitchen sink", PEER, json!({"project": "nonesuch"})).await;
    assert!(every(&named).is_empty() && named == unknown);
    assert!(every(&ask(&r, "kitchen sink", PEER, json!({"project": "awm", "scope": "svc-a"})).await).is_empty());
    assert!(every(&ask(&r, "kitchen sink", PEER, json!({"tag": "Avatar"})).await).is_empty());
    assert_eq!(every_ids(&ask(&r, "kitchen sink", None, json!({"tag": "Avatar"})).await), s(&["p2"]));

    // get answers a peer as if a hidden document did not exist.
    for id in ["j2", "zz", "p2", "j9"] {
        let e = r.search.get(&json!({"source": "journals", "id": id}), PEER, None).await.unwrap_err();
        assert_eq!(e.message, format!("no document journals:{id}"));
    }
    assert_eq!(r.search.get(&json!({"source": "journals", "id": "j2"}), None, None).await.unwrap()["document"]["project"], json!(["avatar"]));
    assert_eq!(r.search.get(&json!({"source": "journals", "id": "j2"}), USER, None).await.unwrap()["document"]["project"], json!(["avatar"]));
    assert_eq!(r.search.get(&json!({"source": "journals", "id": "j1"}), PEER, None).await.unwrap()["document"]["project"], json!(["awm"]));
    assert_eq!(r.search.get(&json!({"source": "journals", "id": "p3"}), PEER, None).await.unwrap()["document"]["title"], "Near tag");

    // Counts: a source's own count is unveiled, so a peer is told none.
    let status = r.search.status(PEER).await;
    assert_eq!((status["sources"]["journals"].clone(), status["documents"].clone()), (Value::Null, Value::Null));
    assert!(status.get("states").is_none());
    assert_eq!(r.search.status(None).await["sources"]["journals"], 10);
    assert_eq!(r.search.status(USER).await["documents"], 10);
    assert!(r.search.sources(PEER).await["sources"][0]["count"].is_null());

    // A live transcripts hit from a marked path never reaches a peer.
    let marked = privacy::slug(&root.join("avatar").to_string_lossy());
    let sibling = privacy::slug(&root.join("avatar2").to_string_lossy());
    let (m, sb) = (marked.clone(), sibling.clone());
    r.gateway.live("transcripts", "transcripts", json!([{"name": "transcripts", "mode": "live", "query": "transcripts_query", "optin": true}]));
    r.gateway.on("transcripts", "query", move |_| {
        Ok(json!({"hits": [
            {"id": "s1:1", "title": m, "snippet": "kitchen sink", "project": m},
            {"id": "s2:1", "title": format!("{m}-Avatar-dev"), "snippet": "kitchen sink", "project": format!("{m}-Avatar-dev")},
            {"id": "s3:1", "title": sb, "snippet": "kitchen sink", "project": sb},
            {"id": "s4:1", "title": "avatar", "snippet": "kitchen sink", "project": "avatar", "scope": "dev"},
        ]}))
    });
    r.search.refresh().await;
    let q = json!({"query": {"text": "kitchen sink", "transcripts": {}}});
    assert_eq!(ids(&r.ask_as(q.clone(), PEER).await.unwrap()["transcripts"]["exact"]), ["s3:1"]);
    assert_eq!(every(&r.ask_as(q.clone(), None).await.unwrap()).len(), 4);

    // A peer call never asks another node.
    r.peers.add("shaula", true, "homebase", |_| Ok(json!({"journals": {"exact": [], "related": []}})));
    let out = r.ask_as(json!({"query": {"text": "kitchen sink", "journals": {}, "journals@shaula": {}}}), PEER).await.unwrap();
    assert_eq!(out["journals@shaula"], json!({"exact": [], "error": "a relayed call answers for this node alone"}));
    assert!(r.peers.calls.lock().unwrap().is_empty() && !every(&out).is_empty());

    // Usage: a local get is logged with the caller's scope and query; a peer's relayed get is not.
    r.search.get(&json!({"source": "journals", "id": "j1", "query": "kitchen sink", "_caller_scope": "awm/dev"}), None, None).await.unwrap();
    r.search.get(&json!({"source": "journals", "id": "j1", "_caller_scope": "awm/dev"}), PEER, None).await.unwrap();
    let rows = r.search.store().read(awm_kb_search::store::usage).await.unwrap();
    let rows: Vec<Value> = rows.into_iter().map(Value::Object).filter(|u| u["doc_id"] == "j1").collect();
    assert_eq!(rows, [json!({"project": "awm", "scope": "dev", "query": "kitchen sink", "source": "journals", "doc_id": "j1", "node": "local"})]);

    // Tag clashes: a peer the clash stands between is refused, others are not.
    clash(&r, "altair", "local");
    let e = r.ask_as(json!({"query": "kitchen sink"}), PEER).await.unwrap_err();
    assert!(e.class == "PermissionError" && e.message.contains("tag clash"), "{e:?}");
    let e = r.search.get(&json!({"source": "journals", "id": "j1"}), PEER, None).await.unwrap_err();
    assert!(e.message.contains("tag clash"));
    assert!(!every(&r.ask_as(json!({"query": "kitchen sink"}), Some("peer:shaula")).await.unwrap()).is_empty());
    assert!(!every(&r.ask(json!({"query": "kitchen sink"})).await).is_empty());
    privacy::write_marks(&[]).unwrap();

    // Unmarking shows everything again, with no rebuild.
    assert_eq!(found(&r, Some("peer:vega")).await, all);

    // An unreadable marks file hides every project from a peer.
    std::fs::write(privacy::marks_path(), "{not json").unwrap();
    assert_eq!(found(&r, Some("peer:vega")).await, s(&["p1"]));
    assert_eq!(found(&r, None).await, all);
    let e = r.search.get(&json!({"source": "journals", "id": "j1"}), Some("peer:vega"), None).await.unwrap_err();
    assert_eq!(e.message, "no document journals:j1");
    assert!(every(&r.ask_as(q, Some("peer:vega")).await.unwrap()).is_empty());
    privacy::write_marks(&[]).unwrap();
}

#[tokio::test]
async fn a_node_an_open_tag_clash_stands_between_is_not_asked() {
    let r = rig().await;
    clash(&r, "local", "altair");
    r.peers.add("altair", true, "homebase", |_| Ok(json!({"journals": {"exact": [], "related": []}})));
    r.peers.add("shaula", true, "homebase", |_| Ok(json!({"journals": {"exact": [{"id": "r1", "title": "T", "ts": ""}]}})));
    let out = r.ask(json!({"query": {"text": "kitchen sink", "journals@altair": {}, "journals@shaula": {}}})).await;
    assert!(out["journals@altair"]["error"].as_str().unwrap().contains("tag clash"));
    assert_eq!(out["journals@shaula"]["exact"], json!([{"id": "r1", "title": "T", "ts": ""}]));
    assert_eq!(r.peers.asked(), ["shaula"]);
    let e = r.search.get(&json!({"source": "journals", "id": "r1", "node": "altair"}), None, None).await.unwrap_err();
    assert_eq!(e.class, "PermissionError");
}

#[test]
fn the_index_veil_and_the_live_hit_check_agree() {
    init();
    let marks = vec![
        awm_index::Mark { project: "avatar".into(), scope: None },
        awm_index::Mark { project: "awm".into(), scope: Some("svc-a".into()) },
    ];
    let veil = Veil::Marks { marks, tags: None };
    let rt = tokio::runtime::Runtime::new().unwrap();
    rt.block_on(async {
        let dir = tempfile::tempdir().unwrap();
        let l = Lance::open(dir.path(), "t", false).await;
        let cond = awm_index::sql::veil_sql(&l.fields, &veil);
        for (i, (project, scope)) in [("avatar", ""), ("avatar", "dev"), ("avatar2", "dev"), ("awm", "svc-a"), ("awm", "svc-a/x"), ("awm", "svc-ab"), ("awm", ""), ("", "")]
            .into_iter()
            .enumerate()
        {
            let id = format!("x{i}");
            l.put(&[(id.as_str(), json!({"title": "t", "text": "body", "ts": "2026-01-01T00:00:00Z", "project": project, "scope": scope}))]).await;
            let kept = l.index.count(Some(&format!("{cond} AND id = '{id}'"))).await.unwrap();
            assert_eq!(live::covered(&veil, project, scope), kept == 0, "{project}/{scope}");
        }
    });
}
