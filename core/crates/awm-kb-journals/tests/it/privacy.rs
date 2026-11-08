//! The private marks and the peer rules: what a peer cannot read, write or learn.

use awm_kb_journals::Who;
use serde_json::json;

use crate::common::*;

const SEEDED: [(&str, &str); 5] =
    [("avatar", "Avatar"), ("avatar", "Avatar/dev"), ("awm", "dev"), ("awm", "secret"), ("awm", "secret/sub")];

async fn seeded(r: &Rig) -> Vec<String> {
    let mut ids = vec![];
    for (project, scope) in SEEDED {
        let p = r.post(project, scope, &format!("agent:{project}/{scope}"), &format!("entry {project} {scope}"), json!(["journal"])).await;
        ids.push(p.id);
    }
    ids
}

fn open_channel() -> Vec<(String, String)> {
    vec![("awm".into(), "dev".into())]
}

#[tokio::test]
async fn a_peer_reads_no_marked_channel_and_a_local_caller_reads_all() {
    let _m = marks(&[("avatar", None), ("awm", Some("secret"))]).await;
    let r = rig().await;
    let ids = seeded(&r).await;

    for as_ in ["peer", "peer:altair"] {
        let who = Who::trusted().with_as(Some(as_));
        assert_eq!(r.owners(json!({"tags": "journal"}), &who).await, open_channel(), "{as_} all channels");
        assert_eq!(r.owners(json!({"tags": "journal", "query": "entry"}), &who).await, open_channel(), "{as_} search");
        for (project, scope) in [("avatar", "Avatar"), ("awm", "secret/sub")] {
            assert!(r.owners(json!({"project": project, "scope": scope}), &who).await.is_empty(), "{as_} {project}/{scope}");
        }
        assert!(r.owners(json!({"post_id": ids[0]}), &who).await.is_empty(), "{as_} by id");
        assert_eq!(r.owners(json!({"post_id": ids[2]}), &who).await, open_channel(), "{as_} open post by id");
    }
    for as_ in [None, Some("user:tony"), Some("agent:awm/dev")] {
        let who = Who::trusted().with_as(as_);
        assert_eq!(r.owners(json!({"tags": "journal"}), &who).await.len(), 5, "{as_:?}");
        assert_eq!(r.owners(json!({"post_id": ids[0]}), &who).await.len(), 1);
    }
}

#[tokio::test]
async fn a_peer_cannot_write_or_inbox_a_marked_scope_and_its_posts_carry_the_edge_identity() {
    let _m = marks(&[("avatar", None)]).await;
    let r = rig().await;
    let post = |project: &str| json!({"project": project, "scope": "main", "author": "agent:awm/dev", "body": "b"});

    let refused = r.call_as("post", post("avatar"), &peer("altair")).await.expect_err("marked");
    assert_eq!((refused.class, refused.message.as_str()), ("ValueError", "no scope avatar/main"));
    r.call_as("inbox", json!({"project": "avatar", "scope": "main"}), &peer("altair")).await.expect_err("peer inbox");
    r.call("post", post("avatar")).await.expect("a local caller posts to a marked scope");

    let made = r.call_as("post", post("awm"), &peer("altair")).await.unwrap();
    assert_eq!(made["post"]["author"], json!("peer:altair"));
    assert_eq!(made["post"]["meta"]["claimed_author"], json!("agent:awm/dev"));
}

#[tokio::test]
async fn a_peer_render_omits_marked_channels() {
    let _m = marks(&[("avatar", None), ("awm", Some("secret"))]).await;
    let r = rig().await;
    seeded(&r).await;
    let render = |project: &str, scope: &str, who: Who| {
        let (project, scope) = (project.to_string(), scope.to_string());
        let r = &r;
        async move { str_of(&r.call_as("render", json!({"project": project, "scope": scope}), &who).await.unwrap()["content"]) }
    };
    let peer_view = render("awm", "dev", peer("altair")).await;
    assert!(peer_view.contains("entry awm dev") && !peer_view.contains("entry awm secret"), "{peer_view}");
    assert!(render("awm", "dev", Who::trusted()).await.contains("entry awm secret"));
    let marked = render("avatar", "Avatar", peer("altair")).await;
    assert!(marked.contains("No journal entries yet") && !marked.contains("entry avatar"), "{marked}");
}

#[tokio::test]
async fn a_tag_named_for_a_marked_project_hides_its_posts_and_itself_from_a_peer() {
    let _m = marks(&[("avatar", None)]).await;
    let r = rig().await;
    r.j.tag_add(&json!("avatar"), "the avatar project", "tony", None).await.unwrap();
    r.post("awm", "dev", "user:tony", "tagged", json!(["avatar"])).await;
    r.post("awm", "dev", "user:tony", "plain", json!(["message"])).await;

    let bodies = |got: serde_json::Value| -> Vec<String> {
        let mut b: Vec<String> = got["posts"].as_array().unwrap().iter().map(|p| str_of(&p["body"])).collect();
        b.sort();
        b
    };
    let args = json!({"project": "awm", "scope": "dev"});
    assert_eq!(bodies(r.call_as("fetch", args.clone(), &peer("altair")).await.unwrap()), ["plain"]);
    assert_eq!(bodies(r.call("fetch", args.clone()).await.unwrap()), ["plain", "tagged"]);
    assert!(!names_of(&r.call_as("tag_list", json!({}), &peer("altair")).await.unwrap()).contains(&"avatar".to_string()));
    assert!(names_of(&r.call("tag_list", json!({})).await.unwrap()).contains(&"avatar".to_string()));

    // marks that cannot be read hide everything from a peer, and nothing from this node
    break_marks();
    assert!(bodies(r.call_as("fetch", args.clone(), &peer("altair")).await.unwrap()).is_empty());
    assert!(names_of(&r.call_as("tag_list", json!({}), &peer("altair")).await.unwrap()).is_empty());
    assert_eq!(bodies(r.call("fetch", args).await.unwrap()).len(), 2);
}

#[tokio::test]
async fn the_search_source_veils_marked_posts_from_a_peer() {
    let _m = marks(&[("avatar", None)]).await;
    let r = rig().await;
    let ids = seeded(&r).await;
    let get = |id: &str, who: Who| {
        let (r, id) = (&r, id.to_string());
        async move { r.call_as("journal_get", json!({"id": id}), &who).await }
    };
    assert_eq!(get(&ids[0], peer("altair")).await.expect_err("veiled").class, "ValueError");
    assert!(get(&ids[0], Who::trusted()).await.is_ok());
    assert!(get(&ids[2], peer("altair")).await.is_ok());
    let found = r.call_as("journal_query", json!({"query": "entry"}), &peer("altair")).await.unwrap();
    let seen = found.to_string();
    assert!(seen.contains(&ids[2]) && !seen.contains(&ids[0]), "{seen}");
}

#[tokio::test]
async fn tag_receive_takes_tags_only_from_an_edge_stamped_peer() {
    let r = rig().await;
    let tags = json!([remote(&"b".repeat(32), "from-altair", 10, 1, "altair")]);
    for as_ in [None, Some("peer"), Some("peer:"), Some("user:tony"), Some("agent:awm/dev")] {
        let got = r.call_as("tag_receive", json!({"tags": tags}), &Who::trusted().with_as(as_)).await;
        assert_eq!(got.err().map(|e| e.class), Some("PermissionError"), "{as_:?}");
    }
    assert!(r.j.tag_by_name("from-altair").unwrap().is_none());
    let got = r.call_as("tag_receive", json!({"tags": tags}), &peer("altair")).await.unwrap();
    assert_eq!(got["added"], json!(1));
    assert!(r.j.tag_by_name("from-altair").unwrap().is_some());
}
