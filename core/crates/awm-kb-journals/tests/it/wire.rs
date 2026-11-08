//! What the gateway, the hooks and the CLI rely on: the describe surface and the answers.

use std::collections::BTreeMap;

use awm_kb_journals::verbs::specs;
use awm_kb_journals::{Fail, Who};
use serde_json::{json, Value};

use crate::common::*;

#[test]
fn describe_lists_every_verb_with_its_required_params() {
    let want: &[(&str, &[&str])] = &[
        ("edit", &["post_id"]),
        ("history", &["post_id"]),
        ("addenda", &[]),
        ("post", &["author", "body"]),
        ("fetch", &[]),
        ("reads", &[]),
        ("note_read", &["post_id"]),
        ("inbox", &["project", "scope"]),
        ("ack", &["project", "scope"]),
        ("tag_add", &["description", "name"]),
        ("tag_edit", &["tag"]),
        ("tag_list", &[]),
        ("retag", &["post_id"]),
        ("tag_receive", &["tags"]),
        ("brief_read", &["project", "scope"]),
        ("brief_history", &["project", "scope"]),
        ("brief_set", &["project", "scope", "text"]),
        ("brief_edit", &["new", "old", "project", "scope"]),
        ("render", &["project", "scope"]),
        ("journal_query", &[]),
        ("journal_get", &["id"]),
        ("precedent_import", &[]),
        ("lineage", &[]),
        ("session_parent", &["parent", "session_id"]),
        ("lineage_import", &["path"]),
    ];
    let want: BTreeMap<String, Vec<String>> =
        want.iter().map(|(v, req)| (v.to_string(), req.iter().map(|r| r.to_string()).collect())).collect();
    let got: BTreeMap<String, Vec<String>> = specs()
        .iter()
        .map(|(verb, _, params)| {
            let mut req: Vec<String> = params
                .as_array()
                .unwrap()
                .iter()
                .filter(|p| p["required"] == json!(true))
                .map(|p| str_of(&p["name"]))
                .collect();
            req.sort();
            (verb.to_string(), req)
        })
        .collect();
    assert_eq!(got, want);
}

#[tokio::test]
async fn post_answers_with_the_post_and_a_reply_joins_its_threads_channel() {
    let r = rig().await;
    let first = r.call("post", json!({"project": "awm", "scope": "dev", "author": "tony", "body": "first"})).await.unwrap();
    assert_eq!(keys(&first), ["post"]);
    let post = &first["post"];
    assert_eq!(keys(post), ["author", "body", "id", "meta", "project", "scope", "tags", "ts"]);
    assert_eq!((post["author"].as_str(), post["tags"].clone()), (Some("user:tony"), json!(["message"])));

    let reply = r.call("post", json!({"reply_to": [post["id"]], "author": "agent:awm/dev", "body": "second"})).await.unwrap();
    let c = &reply["post"];
    assert_eq!((c["project"].as_str(), c["scope"].as_str()), (Some("awm"), Some("dev")));
    assert_eq!((c["reply_to"].clone(), c["thread"].clone()), (json!([post["id"]]), post["id"].clone()));
}

#[tokio::test]
async fn fetch_answers_a_channel_a_search_and_a_post_with_its_thread() {
    let r = rig().await;
    let root = r.post("awm", "dev", "user:tony", "root post", json!(["message"])).await;
    let comment = r.call("post", json!({"reply_to": [root.id], "author": "user:tony", "body": "a comment"})).await.unwrap();

    let channel = r.call("fetch", json!({"project": "awm", "scope": "dev"})).await.unwrap();
    assert_eq!(keys(&channel), ["posts", "total"]);
    assert_eq!(channel["total"], json!(2));

    let one = r.call("fetch", json!({"post_id": root.id})).await.unwrap();
    assert_eq!(keys(&one), ["posts", "thread", "total"]);
    assert_eq!(one["posts"][0]["comments"], json!(1));
    assert_eq!(one["thread"][0]["id"], comment["post"]["id"]);
    assert_eq!(one["thread"][0]["replies"], json!([]));

    let missing = r.call("fetch", json!({"post_id": "nope"})).await.unwrap();
    assert_eq!(missing, json!({"posts": [], "total": 0}));
}

#[tokio::test]
async fn render_answers_markdown_and_its_line_count() {
    let r = rig().await;
    r.post("awm", "dev", "agent:awm/dev", "did the thing", json!(["journal"])).await;
    let out = r.call("render", json!({"project": "awm", "scope": "dev"})).await.unwrap();
    assert_eq!(keys(&out), ["content", "history_lines"]);
    let content = out["content"].as_str().unwrap();
    assert!(content.starts_with("# Project History: awm"), "{content}");
    assert!(content.contains("## This Scope's Journal (dev)"), "{content}");
    assert_eq!(out["history_lines"].as_u64().unwrap() as usize, content.lines().count());
}

#[tokio::test]
async fn inbox_lists_unread_lines_with_the_cursor_and_the_ack_id() {
    let r = rig().await;
    let a = r.post_at("awm", "dev", "user:tony", "# Heading\nbody one", json!(["message"]), Some(1000)).await;
    let inbox = r.call("inbox", json!({"project": "awm", "scope": "dev"})).await.unwrap();
    assert_eq!(keys(&inbox), ["ack_through", "cursor", "more", "next", "posts", "project", "scope", "tags", "total", "unread"]);
    assert_eq!((inbox["unread"].clone(), inbox["cursor"].clone(), inbox["ack_through"].clone()), (json!(1), Value::Null, json!(a.id)));
    assert_eq!(keys(&inbox["posts"][0]), ["age", "asks", "author", "chars", "gist", "id"]);
    assert_eq!(inbox["posts"][0]["gist"], json!("Heading"));
}

#[tokio::test]
async fn failures_carry_the_class_the_python_raised() {
    let r = rig().await;
    let existing = r.post("awm", "dev", "user:tony", "x", json!(["message"])).await;
    let trusted = Who::trusted();
    let cases: Vec<(&str, Value, Who, &str)> = vec![
        ("post", json!({"author": "tony", "body": "no home"}), trusted.clone(), "ValueError"),
        ("post", json!({"project": "awm", "scope": "dev", "author": "peer:x", "body": "b"}), trusted.clone(), "ValueError"),
        ("tag_add", json!({"name": "fresh", "description": ""}), trusted.clone(), "TagError"),
        ("ack", json!({"project": "awm", "scope": "dev", "through": "nope"}), trusted.clone(), "ValueError"),
        ("edit", json!({"post_id": "nope", "body": "b"}), trusted.clone(), "ValueError"),
        ("edit", json!({"post_id": existing.id, "body": "x"}), trusted.clone(), "ValueError"),
        ("fetch", json!({}), Who::default(), "Refused"),
        ("no_such_verb", json!({}), trusted.clone(), "RuntimeError"),
    ];
    for (verb, args, who, class) in cases {
        let got: Result<Value, Fail> = r.call_as(verb, args.clone(), &who).await;
        assert_eq!(got.err().map(|e| e.class), Some(class), "{verb} {args}");
    }
}
