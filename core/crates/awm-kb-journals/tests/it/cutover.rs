//! The cutover from the Python's post tables, and the export-back that undoes it.

use awm_kb_journals::export_back;
use serde_json::json;

use crate::common::*;

#[tokio::test]
async fn the_first_start_copies_every_post_and_a_second_copies_nothing() {
    let r = from_dir(python_dir(Python::V6), "home").await;
    assert_eq!((r.j.cutover.posts, r.j.cutover.copied), (4, 4));
    assert_eq!(r.j.index.count(None).await.unwrap(), 4);

    // a post edited after the cutover is newer than the frozen row, and the frozen row must not win
    r.call("edit", json!({"post_id": P1, "body": "edited after the cutover"})).await.unwrap();
    let r = r.reopen().await;
    assert_eq!((r.j.cutover.posts, r.j.cutover.copied), (4, 0));
    assert_ne!(r.j.cutover.at_ms, 0);
    let got = r.call("fetch", json!({"post_id": P1})).await.unwrap();
    assert_eq!(got["posts"][0]["body"], json!("edited after the cutover"));
    assert_eq!(r.j.index.count(None).await.unwrap(), 4);
}

#[tokio::test]
async fn export_back_returns_new_and_edited_posts_to_the_python_tables() {
    let r = from_dir(python_dir(Python::V6), "home").await;
    assert_eq!(r.j.export_back().await.unwrap(), json!({"posts": 4, "exported": 0, "unchanged": 4}));

    let fresh = r.post("awm", "dev", "user:tony", "written after the cutover", json!(["message"])).await;
    r.call("edit", json!({"post_id": P2, "body": "ping, edited"})).await.unwrap();
    assert_eq!(r.j.export_back().await.unwrap(), json!({"posts": 5, "exported": 2, "unchanged": 3}));
    assert_eq!(r.j.export_back().await.unwrap()["exported"], json!(0));

    let c = r.sql();
    let row = |id: &str| -> (String, String, String, i64) {
        c.query_row("SELECT owner_project, owner_scope, body, version FROM scope_posts WHERE id=?", [id], |x| Ok((x.get(0)?, x.get(1)?, x.get(2)?, x.get(3)?)))
            .unwrap()
    };
    assert_eq!(row(&fresh.id), ("awm".into(), "dev".into(), "written after the cutover".into(), 1));
    assert_eq!(row(P2), ("awm".into(), "dev".into(), "ping, edited".into(), 2));
    let tag: String = c.query_row("SELECT tag_id FROM post_tags WHERE post_id=?", [&fresh.id], |x| x.get(0)).unwrap();
    assert_eq!(tag, "builtin:message");
    let total: i64 = c.query_row("SELECT COUNT(*) FROM scope_posts", [], |x| x.get(0)).unwrap();
    assert_eq!(total, 5);
}

#[tokio::test]
async fn the_rollback_tool_opens_the_node_and_exports_without_a_model() {
    let r = from_dir(python_dir(Python::V6), "home").await;
    r.post("awm", "dev", "user:tony", "rolled back", json!(["message"])).await;
    let Rig { dir, j, out } = r;
    drop((j, out));
    let done = export_back(dir.path(), "home").await.unwrap();
    assert_eq!(done["exported"], json!(1));
}
