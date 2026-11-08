//! A crash between the two writes of an edit loses nothing and shows no half-written post.

use serde_json::json;

use crate::common::*;

#[tokio::test]
async fn a_crash_between_the_version_and_the_post_loses_nothing_and_the_start_finishes_it() {
    // (whole body, old, new) and the text the edit produces from "alpha beta"
    let edits = [(Some("alpha gamma"), None, None), (None, Some("beta"), Some("gamma"))];
    for (body, old, new) in edits {
        let r = rig().await;
        let p = r.post("awm", "dev", "user:tony", "alpha beta", json!(["message"])).await;

        let crashed = r.j.edit_locked(&p.id, "agent:awm/dev", body, old, new, "typo", true).await;
        assert_eq!(crashed.err().map(|e| e.class), Some("RuntimeError"));

        // the version is stored; the post still reads whole at its old version
        let history = r.call("history", json!({"post_id": p.id})).await.unwrap();
        let versions: Vec<_> = history["versions"].as_array().unwrap().iter().map(|v| (v["version"].clone(), v["body"].clone())).collect();
        assert_eq!(versions, [(json!(1), json!("alpha beta")), (json!(2), json!("alpha gamma"))]);
        let seen = r.call("fetch", json!({"post_id": p.id})).await.unwrap();
        assert_eq!((seen["posts"][0]["body"].clone(), seen["posts"][0].get("version").cloned()), (json!("alpha beta"), None));

        // the next start applies the version once; a further start has nothing left to do
        let r = r.reopen().await;
        let seen = r.call("fetch", json!({"project": "awm", "scope": "dev"})).await.unwrap();
        assert_eq!(seen["total"], json!(1));
        assert_eq!((seen["posts"][0]["body"].clone(), seen["posts"][0]["version"].clone()), (json!("alpha gamma"), json!(2)));
        assert_eq!(r.j.reconcile().await.unwrap(), json!({"edits": 0, "threads": 0}));
    }
}

#[tokio::test]
async fn a_completed_edit_keeps_every_version_and_a_replay_adds_no_post() {
    let r = rig().await;
    let p = r.post("awm", "dev", "user:tony", "one", json!(["message"])).await;
    r.call("edit", json!({"post_id": p.id, "body": "two"})).await.unwrap();
    r.call("edit", json!({"post_id": p.id, "body": "three"})).await.unwrap();
    let r = r.reopen().await;
    let history = r.call("history", json!({"post_id": p.id})).await.unwrap();
    let bodies: Vec<_> = history["versions"].as_array().unwrap().iter().map(|v| v["body"].clone()).collect();
    assert_eq!(bodies, [json!("one"), json!("two"), json!("three")]);
    assert_eq!(r.call("fetch", json!({"project": "awm", "scope": "dev"})).await.unwrap()["total"], json!(1));
}
