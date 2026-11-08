//! journal.db: its schema, its migrations, and a database the Python wrote opened in place.

use std::collections::BTreeSet;

use awm_kb_journals::schema::{BUILTINS, SCHEMA_VERSION};
use awm_kb_journals::{Journals, Opts};
use serde_json::json;

use crate::common::*;

fn tables(r: &Rig) -> BTreeSet<String> {
    let c = r.sql();
    let mut stmt = c.prepare("SELECT name FROM sqlite_master WHERE type='table'").unwrap();
    let names = stmt.query_map([], |row| row.get::<_, String>(0)).unwrap().map(|n| n.unwrap()).collect();
    names
}

#[tokio::test]
async fn a_fresh_journal_is_schema_v6_with_the_fixed_tags() {
    let r = rig().await;
    let c = r.sql();
    let version: i64 = c.query_row("SELECT version FROM schema_version", [], |row| row.get(0)).unwrap();
    assert_eq!(version, SCHEMA_VERSION);
    assert_eq!(SCHEMA_VERSION, 6);
    let want = [
        "post_cites", "post_tags", "post_versions", "schema_version", "scope_cursors", "scope_posts", "search_feed",
        "session_parents", "session_reads", "tag_clashes", "tag_versions", "tags",
    ];
    assert!(want.iter().all(|t| tables(&r).contains(*t)), "{:?}", tables(&r));
    for (id, name, _) in BUILTINS {
        let got: String = c.query_row("SELECT name FROM tags WHERE id=?", [id], |row| row.get(0)).unwrap();
        assert_eq!(got, name);
    }
}

#[tokio::test]
async fn a_python_v6_database_is_read_in_place() {
    let dir = python_dir(Python::V6);
    let r = from_dir(dir, "home").await;

    let all = r.call("fetch", json!({"project": "awm", "scope": "dev", "order": "asc"})).await.unwrap();
    let rows: Vec<_> = all["posts"].as_array().unwrap().iter().map(|p| (p["id"].clone(), p["tags"].clone())).collect();
    assert_eq!(
        rows,
        [(json!(P1), json!(["alpha", "journal"])), (json!(P2), json!(["message"])), (json!(P3), json!(["message"])), (json!(P4), json!(["journal"]))]
    );
    assert_eq!(all["posts"][3]["reply_to"], json!([P1]));
    assert_eq!((all["posts"][2]["reply_to"].clone(), all["posts"][2]["thread"].clone()), (json!([P2]), json!(P2)));

    // the cursor the Python stored still marks the ping read
    let inbox = r.call("inbox", json!({"project": "awm", "scope": "dev"})).await.unwrap();
    assert_eq!(inbox["unread"], json!(0));
    assert_eq!(inbox["cursor"]["through_id"], json!(P2));

    // opened in place: no second journal, no migration, the frozen rows stay
    let c = r.sql();
    let frozen: i64 = c.query_row("SELECT COUNT(*) FROM scope_posts", [], |row| row.get(0)).unwrap();
    assert_eq!(frozen, 4);
}

#[tokio::test]
async fn a_python_v4_database_migrates_its_kinds_into_tags_and_keeps_a_backup() {
    let dir = python_dir(Python::V4);
    let r = from_dir(dir, "home").await;

    let all = r.call("fetch", json!({"project": "awm", "scope": "dev", "order": "asc"})).await.unwrap();
    let got: Vec<_> = all["posts"].as_array().unwrap().iter().map(|p| (str_of(&p["id"]), p["tags"].clone())).collect();
    let want = [("k1", json!(["journal"])), ("k2", json!(["journal"])), ("k3", json!(["message"])), ("k4", json!(["weird"])), ("k5", json!(["message"]))];
    assert_eq!(got, want.map(|(i, t)| (i.to_string(), t)));

    let c = r.sql();
    let version: i64 = c.query_row("SELECT version FROM schema_version", [], |row| row.get(0)).unwrap();
    assert_eq!(version, 6);
    let kind_column: i64 = c.query_row("SELECT COUNT(*) FROM pragma_table_info('scope_posts') WHERE name='kind'", [], |row| row.get(0)).unwrap();
    assert_eq!(kind_column, 0);
    let backup = Journals::db_path(r.dir.path()).with_file_name("journal.db.v4.bak");
    assert!(backup.exists(), "the pre-migration copy waits next to journal.db");
}

#[tokio::test]
async fn a_node_with_a_journal_elsewhere_refuses_to_grow_a_second_one() {
    let elsewhere = python_dir(Python::V6);
    let fresh = tempfile::tempdir().unwrap();
    state_open(fresh.path(), vec![Journals::db_path(elsewhere.path())]).await.expect_err("a second journal");
    assert!(!Journals::db_path(fresh.path()).exists());
}

async fn state_open(dir: &std::path::Path, other_dbs: Vec<std::path::PathBuf>) -> awm_kb_journals::Result<()> {
    Journals::open(dir, None, Opts { node: "home".into(), other_dbs }).await.map(|_| ())
}
