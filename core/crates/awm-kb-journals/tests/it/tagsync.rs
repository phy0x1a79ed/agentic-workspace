//! The fleet's one tag vocabulary: merge, clash, the dispatched agent, the block, the send at start.

use awm_kb_journals::tagsync::provisional_name;
use serde_json::{json, Value};

use crate::common::*;

const R: &str = "abcdef0123456789abcdef0123456789";

fn vocabulary(r: &Rig) -> Vec<Value> {
    r.j.vocabulary().unwrap().iter().map(|t| t.to_dict()).collect()
}

fn name_of(r: &Rig, id: &str) -> String {
    r.j.tag(id).unwrap().unwrap().name
}

#[tokio::test]
async fn the_same_tag_id_takes_the_higher_version() {
    let r = rig().await;
    // (incoming version, incoming name, name held afterwards, counts)
    let steps = [
        (1, "complaint", "complaint", json!({"added": 1, "updated": 0, "clashes": 0})),
        (2, "gripe", "gripe", json!({"added": 0, "updated": 1, "clashes": 0})),
        (1, "stale", "gripe", json!({"added": 0, "updated": 0, "clashes": 0})),
        (2, "same-version", "gripe", json!({"added": 0, "updated": 0, "clashes": 0})),
    ];
    for (version, name, held, counts) in steps {
        let got = r.j.merge_tags(&[remote(R, name, 10, version, "altair")], "altair").await.unwrap();
        assert_eq!((got, name_of(&r, R)), (counts, held.to_string()), "v{version} {name}");
    }
}

#[tokio::test]
async fn two_nodes_adding_one_name_become_a_clash_with_the_deterministic_provisional_name() {
    // (created_ms of the incoming tag, whether the local tag is the newer one)
    for (created, local_newer) in [(i64::MAX / 2, false), (1, true)] {
        let r = rig().await;
        let local = r.j.tag_add(&json!("complaint"), "a bug report", "tony", None).await.unwrap();
        let got = r.j.merge_tags(&[remote(R, "complaint", created, 1, "altair")], "altair").await.unwrap();
        assert_eq!(got["clashes"], json!(1));

        let (older, newer) = if local_newer { (R, local.id.as_str()) } else { (local.id.as_str(), R) };
        assert_eq!(name_of(&r, older), "complaint");
        assert_eq!(name_of(&r, newer), provisional_name("complaint", newer));
        assert!(name_of(&r, newer).starts_with("complaint.clash-"));
        let open = r.j.clashes(true).unwrap();
        assert_eq!(open.len(), 1);
        assert_eq!((open[0]["older_id"].as_str(), open[0]["newer_id"].as_str()), (Some(older), Some(newer)));
        assert_eq!(provisional_name("complaint", R), "complaint.clash-abcdef");
    }
}

#[tokio::test]
async fn both_nodes_settle_on_the_same_names() {
    let (a, b) = (rig_on("home").await, rig_on("altair").await);
    a.j.tag_add(&json!("complaint"), "a bug report", "tony", None).await.unwrap();
    b.j.tag_add(&json!("complaint"), "a gripe", "tony", None).await.unwrap();
    let (from_a, from_b) = (vocabulary(&a), vocabulary(&b));
    a.j.merge_tags(&from_b, "altair").await.unwrap();
    b.j.merge_tags(&from_a, "home").await.unwrap();

    let names = |r: &Rig| -> Vec<(String, String)> {
        let mut v: Vec<_> = r.j.vocabulary().unwrap().into_iter().map(|t| (t.id, t.name)).collect();
        v.sort();
        v
    };
    assert_eq!(names(&a), names(&b));
    let held: Vec<_> = names(&a).into_iter().filter(|(_, n)| n.starts_with("complaint")).collect();
    assert_eq!(held.len(), 2);
    assert_eq!(held.iter().filter(|(_, n)| n.contains(".clash-")).count(), 1);
}

#[tokio::test]
async fn only_the_node_that_added_the_newer_tag_dispatches_an_agent_and_a_rename_ends_the_clash() {
    // (incoming created_ms, agents started here)
    for (created, started) in [(1, 1), (i64::MAX / 2, 0)] {
        let r = rig().await;
        let local = r.j.tag_add(&json!("complaint"), "a bug report", "tony", None).await.unwrap();
        r.j.merge_tags(&[remote(R, "complaint", created, 1, "altair")], "altair").await.unwrap();
        r.j.dispatch_open().await.unwrap();
        let sent = r.out.started.lock().unwrap().clone();
        assert_eq!(sent.len(), started);
        if let Some(args) = sent.first() {
            assert!(args["prompt"].as_str().unwrap().contains("tag_edit"));
        }
        assert!(r.j.blocked(Some("altair")).unwrap() && !r.j.blocked(Some("capella")).unwrap());

        let newer = if started == 1 { local.id.as_str() } else { R };
        r.call("tag_edit", json!({"tag": newer, "name": "renamed"})).await.unwrap();
        assert!(r.j.clashes(true).unwrap().is_empty());
        assert!(!r.j.blocked(Some("altair")).unwrap());
    }
}

#[tokio::test]
async fn the_two_nodes_refuse_each_others_journal_reads_while_a_clash_stands() {
    let _m = marks(&[]).await;
    let r = rig().await;
    let local = r.j.tag_add(&json!("complaint"), "a bug report", "tony", None).await.unwrap();
    r.j.merge_tags(&[remote(R, "complaint", i64::MAX / 2, 1, "altair")], "altair").await.unwrap();
    let _ = local;

    for verb in ["fetch", "journal_query", "journal_get"] {
        let got = r.call_as(verb, json!({"id": "x"}), &peer("altair")).await;
        let e = got.expect_err(verb);
        assert_eq!(e.class, "PermissionError", "{verb}");
        assert!(e.message.contains("tag clash"), "{}", e.message);
    }
    r.call_as("fetch", json!({}), &peer("capella")).await.expect("another peer reads");
    r.call("fetch", json!({})).await.expect("this node reads");

    r.call("tag_edit", json!({"tag": R, "name": "altair-gripe"})).await.unwrap();
    r.call_as("fetch", json!({}), &peer("altair")).await.expect("the rename lifts the block");
}

#[tokio::test]
async fn the_start_sends_the_vocabulary_but_no_tag_the_marks_hide() {
    let _m = marks(&[("avatar", None)]).await;
    let r = rig().await;
    for n in ["avatar", "t-secret", "t-both", "t-unused"] {
        r.j.tag_add(&json!(n), "a tag", "tony", None).await.unwrap();
    }
    r.post("avatar", "main", "user:tony", "hidden channel", json!(["t-secret", "t-both"])).await;
    r.post("awm", "dev", "user:tony", "open channel", json!(["t-both"])).await;
    *r.out.peers.lock().unwrap() = vec!["altair".into()];
    r.out.lists.lock().unwrap().insert("altair".into(), json!({"tags": [remote(R, "from-altair", 10, 1, "altair")]}));
    r.out.sent.lock().unwrap().clear();

    r.j.sync_at_start().await;

    let sent = r.out.sent.lock().unwrap().clone();
    assert_eq!(sent.len(), 1);
    assert_eq!(sent[0].0, "altair");
    let mut names: Vec<String> = sent[0].1.iter().map(|t| str_of(&t["name"])).collect();
    names.sort();
    let mut want = vec!["brief", "closed", "folded", "from-altair", "journal", "message", "precedent", "superseded", "t-both", "t-unused"];
    want.sort();
    assert_eq!(names, want);
}

#[tokio::test]
async fn the_start_sends_nothing_when_the_marks_cannot_be_read() {
    let _m = marks(&[]).await;
    let r = rig().await;
    *r.out.peers.lock().unwrap() = vec!["altair".into()];
    r.out.lists.lock().unwrap().insert("altair".into(), json!({"tags": []}));
    break_marks();
    r.j.sync_at_start().await;
    assert!(r.out.sent.lock().unwrap().is_empty());
}
