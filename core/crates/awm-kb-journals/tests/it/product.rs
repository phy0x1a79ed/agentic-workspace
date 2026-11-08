//! One path each through precedents, briefs and the inbox cursor.

use awm_kb_journals::precedents::post_id;
use serde_json::json;

use crate::common::*;

fn precedence_db(dir: &std::path::Path) -> std::path::PathBuf {
    let path = dir.join("precedence.db");
    awm_store::rusqlite::Connection::open(&path)
        .unwrap()
        .execute_batch(
            "CREATE TABLE decisions (id TEXT PRIMARY KEY, context TEXT, question TEXT, decision TEXT, created TEXT, added TEXT,
               status TEXT, superseded_by TEXT, source TEXT, source_ref TEXT);
             CREATE TABLE tags (decision_id TEXT, tag TEXT);
             CREATE TABLE notes (id TEXT PRIMARY KEY, decision_id TEXT, body TEXT, kind TEXT, author TEXT, created TEXT);
             INSERT INTO decisions VALUES ('d1', 'ctx one', 'Question one?', 'Yes.', '2026-01-01', NULL, 'active', NULL, NULL, NULL),
               ('d2', 'ctx two', 'Question two?', 'Old way.', '2026-01-02', NULL, 'superseded', 'd3', NULL, NULL),
               ('d3', 'ctx three', 'Question two again?', 'New way.', '2026-01-03', NULL, 'active', NULL, NULL, NULL);
             INSERT INTO tags VALUES ('d1', 'Style Guide');
             INSERT INTO notes VALUES ('n1', 'd1', 'also applies to docs', 'amendment', 'tony', '2026-01-04');",
        )
        .unwrap();
    path
}

#[tokio::test]
async fn precedent_import_makes_one_post_per_decision_and_a_rerun_adds_nothing() {
    let r = rig().await;
    let path = precedence_db(r.dir.path());
    let args = json!({"path": path});

    let dry = r.call("precedent_import", json!({"path": path, "dry_run": true})).await.unwrap();
    assert_eq!((dry["status"].clone(), dry["decisions"].clone()), (json!("dry-run"), json!(3)));
    assert_eq!(r.call("fetch", json!({"tags": "precedent"})).await.unwrap()["total"], json!(0));

    let done = r.call("precedent_import", args.clone()).await.unwrap();
    assert_eq!((done["status"].clone(), done["decisions"].clone(), done["notes"].clone(), done["already"].clone()), (json!("imported"), json!(3), json!(1), json!(0)));
    let added = done["tags_added"].as_array().unwrap().clone();
    assert_eq!(added.len(), 1);

    let all = r.call("fetch", json!({"tags": "precedent"})).await.unwrap();
    assert_eq!(all["total"], json!(3));
    let by_id = |id: String| all["posts"].as_array().unwrap().iter().find(|p| p["id"] == json!(id)).unwrap().clone();
    let d1 = by_id(post_id("decision", "d1"));
    assert!(d1["tags"].as_array().unwrap().contains(&added[0]) && d1["body"].as_str().unwrap().starts_with("Context: ctx one"));
    assert!(by_id(post_id("decision", "d2"))["tags"].as_array().unwrap().contains(&json!("superseded")));
    assert_eq!(by_id(post_id("decision", "d3"))["reply_to"], json!([post_id("decision", "d2")]));

    let again = r.call("precedent_import", args).await.unwrap();
    assert_eq!((again["decisions"].clone(), again["already"].clone()), (json!(0), json!(3)));
    assert_eq!(r.call("fetch", json!({"tags": "precedent"})).await.unwrap()["total"], json!(3));
}

#[tokio::test]
async fn a_brief_is_set_replaced_edited_and_kept_in_a_history() {
    let r = rig().await;
    let at = |verb: &'static str, extra: serde_json::Value| {
        let mut a = json!({"project": "awm", "scope": "dev"});
        a.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        let r = &r;
        async move { r.call(verb, a).await }
    };
    assert_eq!(at("brief_read", json!({})).await.unwrap(), json!({"brief": null}));

    let first = at("brief_set", json!({"text": "For the redux work."})).await.unwrap()["brief"].clone();
    assert_eq!((first["author"].clone(), first["supersedes"].clone()), (json!("agent:awm/dev"), json!("")));
    assert_eq!(first["line"], json!("The brief of awm/dev: For the redux work."));
    let second = at("brief_set", json!({"text": "For the redux port."})).await.unwrap()["brief"].clone();
    assert_eq!(second["supersedes"], first["id"]);
    let same = at("brief_set", json!({"text": "For the redux port."})).await.unwrap()["brief"].clone();
    assert_eq!(same["id"], second["id"]);
    let third = at("brief_edit", json!({"old": "port", "new": "finish"})).await.unwrap()["brief"].clone();
    assert_eq!(third["text"], json!("For the redux finish."));

    assert_eq!(at("brief_read", json!({})).await.unwrap()["brief"]["id"], third["id"]);
    let history = at("brief_history", json!({})).await.unwrap();
    let ids: Vec<_> = history["briefs"].as_array().unwrap().iter().map(|b| b["id"].clone()).collect();
    assert_eq!((ids, history["total"].clone()), (vec![first["id"].clone(), second["id"].clone(), third["id"].clone()], json!(3)));

    let too_long = "x".repeat(401);
    for (verb, extra) in [
        ("brief_set", json!({"text": too_long})),
        ("brief_set", json!({"text": "One thing. Two things. Three things."})),
        ("brief_set", json!({"text": "  "})),
        ("brief_edit", json!({"old": "absent", "new": "y"})),
    ] {
        assert_eq!(at(verb, extra.clone()).await.err().map(|e| e.class), Some("BriefError"), "{verb} {extra}");
    }
}

#[tokio::test]
async fn the_inbox_cursor_moves_forward_only_and_survives_a_restart() {
    let r = rig().await;
    let mut ids = vec![];
    for (i, ts) in [1000, 2000, 3000].into_iter().enumerate() {
        ids.push(r.post_at("awm", "dev", "user:tony", &format!("message {i}"), json!(["message"]), Some(ts)).await.id);
    }
    let own = r.post_at("awm", "dev", "agent:awm/dev", "my own post", json!(["message"]), Some(4000)).await;
    let scope = json!({"project": "awm", "scope": "dev"});
    let ack = |through: &str| {
        let mut a = scope.clone();
        a["through"] = json!(through);
        a
    };

    let inbox = r.call("inbox", scope.clone()).await.unwrap();
    assert_eq!((inbox["unread"].clone(), inbox["ack_through"].clone()), (json!(3), json!(ids[2])));
    let listed: Vec<_> = inbox["posts"].as_array().unwrap().iter().map(|p| p["id"].clone()).collect();
    assert_eq!(listed, [json!(ids[2]), json!(ids[1]), json!(ids[0])], "newest first, never the scope's own post");

    let moved = r.call("ack", ack(&ids[1])).await.unwrap();
    assert_eq!((moved["moved"].clone(), moved["unread"].clone()), (json!(true), json!(1)));
    let back = r.call("ack", ack(&ids[0])).await.unwrap();
    assert_eq!((back["moved"].clone(), back["cursor"]["through_id"].clone()), (json!(false), json!(ids[1])));
    r.call("ack", ack("not-a-post")).await.expect_err("an id outside the channel");

    let stored: (i64, String) = r
        .sql()
        .query_row("SELECT through_ts, through_id FROM scope_cursors WHERE owner_project='awm' AND owner_scope='dev'", [], |x| Ok((x.get(0)?, x.get(1)?)))
        .unwrap();
    assert_eq!(stored, (2000, ids[1].clone()));

    let r = r.reopen().await;
    let after = r.call("inbox", scope.clone()).await.unwrap();
    assert_eq!((after["unread"].clone(), after["posts"][0]["id"].clone()), (json!(1), json!(ids[2])));
    let all = r.call("ack", scope).await.unwrap();
    assert_eq!((all["unread"].clone(), all["cursor"]["through_id"].clone()), (json!(0), json!(own.id)));
}
