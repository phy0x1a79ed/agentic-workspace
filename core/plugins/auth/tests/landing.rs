//! The landing store: Python's schema and answers (`vectors/landing.json`), the one-time copy
//! from `httpsfront.db`, and httpsfront's `test_landing_tags` and `test_landing_rename` DAO tests.

use std::path::Path;

use awm_plugin_auth::landing::{copy_once, Landing, SCHEMA_SQL, SCHEMA_VERSION};
use rusqlite::Connection;
use serde_json::{json, Value};

fn vectors() -> Value {
    serde_json::from_str(include_str!("vectors/landing.json")).unwrap()
}

fn fresh() -> (tempfile::TempDir, Landing) {
    let dir = tempfile::tempdir().unwrap();
    let store = Landing::open(&dir.path().join("landing.db")).unwrap();
    (dir, store)
}

fn strs(v: &Value) -> Vec<String> {
    v.as_array().unwrap().iter().map(|s| s.as_str().unwrap().to_string()).collect()
}

fn replay(store: &Landing, call: &[Value]) -> Value {
    let s = |i: usize| call[i].as_str().unwrap();
    match s(0) {
        "tags_for_page" => json!(store.tags_for_page(s(1)).unwrap()),
        "tags_by_page" => json!(store.tags_by_page(&strs(&call[1])).unwrap()),
        "all_tag_counts" => Value::Object(store.all_tag_counts().unwrap().into_iter().map(|(k, n)| (k, json!(n))).collect()),
        "add_tag" => json!(store.add_tag(s(1), s(2)).unwrap()),
        "remove_tag" => json!(store.remove_tag(s(1), s(2)).unwrap()),
        "selected_tags" => json!(store.selected_tags().unwrap()),
        "select_tag" => json!(store.select_tag(s(1)).unwrap()),
        "deselect_tag" => json!(store.deselect_tag(s(1)).unwrap()),
        "display_name" => json!(store.display_name(s(1)).unwrap()),
        "display_names" => Value::Object(store.display_names(&strs(&call[1])).unwrap().into_iter().map(|(k, v)| (k, json!(v))).collect()),
        "set_display_name" => json!(store.set_display_name(s(1), s(2)).unwrap()),
        "clear_display_name" => json!(store.clear_display_name(s(1)).unwrap()),
        other => panic!("no call {other}"),
    }
}

fn master(path: &Path) -> Value {
    let db = Connection::open(path).unwrap();
    let mut st = db.prepare("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name").unwrap();
    let rows: Vec<Value> = st
        .query_map([], |r| Ok(json!([r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, Option<String>>(3)?])))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    Value::Array(rows)
}

fn tables(path: &Path) -> Value {
    let db = Connection::open(path).unwrap();
    let mut out = serde_json::Map::new();
    for (t, sql) in [
        ("page_tags", "SELECT * FROM page_tags ORDER BY 1, 2"),
        ("selected_tags", "SELECT * FROM selected_tags ORDER BY 1"),
        ("page_names", "SELECT * FROM page_names ORDER BY 1, 2"),
    ] {
        let mut st = db.prepare(sql).unwrap();
        let n = st.column_count();
        let rows: Vec<Value> = st
            .query_map([], |r| Ok(Value::Array((0..n).map(|i| json!(r.get::<_, String>(i).unwrap())).collect())))
            .unwrap()
            .map(Result::unwrap)
            .collect();
        out.insert(t.into(), Value::Array(rows));
    }
    Value::Object(out)
}

#[test]
fn the_store_answers_pythons_script_as_python_did() {
    let v = vectors();
    let (dir, store) = fresh();
    for step in v["script"].as_array().unwrap() {
        let call = step["call"].as_array().unwrap();
        let got = replay(&store, call);
        let want = &step["result"];
        if call[0] == "tags_by_page" {
            assert_eq!(got, *want, "{call:?}");
        } else {
            // Ordered: a dict's order is the query's.
            assert_eq!(serde_json::to_string(&got).unwrap(), serde_json::to_string(want).unwrap(), "{call:?}");
        }
    }
    drop(store);
    assert_eq!(tables(&dir.path().join("landing.db")), v["tables"]);
}

#[test]
fn the_schema_is_httpsfronts() {
    let v = vectors();
    assert_eq!(SCHEMA_SQL, v["schema_sql"]);
    assert_eq!(SCHEMA_VERSION, v["schema_version"].as_i64().unwrap());
    let (dir, store) = fresh();
    drop(store);
    let path = dir.path().join("landing.db");
    assert_eq!(master(&path), v["sqlite_master"]);
    let db = Connection::open(&path).unwrap();
    let version: i64 = db.query_row("SELECT version FROM schema_version", [], |r| r.get(0)).unwrap();
    assert_eq!(json!([[version]]), v["version_rows"]);
    let mode: String = db.query_row("PRAGMA journal_mode", [], |r| r.get(0)).unwrap();
    assert_eq!(mode, "wal");
}

/// An `httpsfront.db` as the Python left it: its schema, its rows, in WAL mode.
fn python_db(path: &Path) {
    let v = vectors();
    let db = Connection::open(path).unwrap();
    db.pragma_update(None, "journal_mode", "WAL").unwrap();
    for row in v["sqlite_master"].as_array().unwrap() {
        if let Some(sql) = row[3].as_str() {
            db.execute_batch(sql).unwrap();
        }
    }
    db.execute("INSERT INTO schema_version (version) VALUES (2)", []).unwrap();
    for (t, rows) in v["tables"].as_object().unwrap() {
        for r in rows.as_array().unwrap() {
            let vals: Vec<String> = strs(r);
            let marks = vec!["?"; vals.len()].join(",");
            db.execute(&format!("INSERT INTO {t} VALUES ({marks})"), rusqlite::params_from_iter(vals.iter())).unwrap();
        }
    }
}

#[test]
fn copy_once_carries_httpsfront_db_over_and_never_again() {
    let dir = tempfile::tempdir().unwrap();
    let old = dir.path().join("services/httpsfront/httpsfront.db");
    let new = dir.path().join("state/landing.db");
    std::fs::create_dir_all(old.parent().unwrap()).unwrap();
    python_db(&old);
    let before = std::fs::read(&old).unwrap();

    assert!(copy_once(&old, &new).unwrap());
    assert_eq!(std::fs::read(&old).unwrap(), before, "the old file is never written");
    let store = Landing::open(&new).unwrap();
    assert_eq!(store.tags_for_page("science").unwrap(), ["lab work"]);
    assert_eq!(store.selected_tags().unwrap(), ["notes"]);
    assert_eq!(store.display_name("trilium").unwrap().as_deref(), Some("Notes"));
    store.add_tag("science", "new").unwrap();
    drop(store);
    assert_eq!(master(&new), vectors()["sqlite_master"]);

    // A second start finds landing.db and copies nothing over it.
    assert!(!copy_once(&old, &new).unwrap());
    let store = Landing::open(&new).unwrap();
    assert_eq!(store.tags_for_page("science").unwrap(), ["lab work", "new"]);
    assert!(!new.with_extension("db.copying").exists());
}

#[test]
fn copy_once_with_no_old_store_copies_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let new = dir.path().join("landing.db");
    assert!(!copy_once(&dir.path().join("absent.db"), &new).unwrap());
    assert!(!new.exists());
}

#[test]
fn copy_once_reads_a_read_only_old_store() {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let old = dir.path().join("httpsfront.db");
    python_db(&old);
    // Checkpoint the WAL into the file so the read-only open needs no -wal of its own.
    Connection::open(&old).unwrap().pragma_update(None, "journal_mode", "DELETE").unwrap();
    std::fs::set_permissions(&old, std::fs::Permissions::from_mode(0o444)).unwrap();
    let new = dir.path().join("landing.db");
    assert!(copy_once(&old, &new).unwrap());
    assert_eq!(Landing::open(&new).unwrap().all_tag_counts().unwrap().len(), 3);
}

#[test]
fn a_version_1_store_gains_page_names() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("landing.db");
    {
        let db = Connection::open(&path).unwrap();
        db.execute_batch(
            "CREATE TABLE page_tags (page TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (page, tag));
             CREATE TABLE selected_tags (tag TEXT PRIMARY KEY);
             CREATE TABLE schema_version (version INTEGER NOT NULL);
             INSERT INTO schema_version VALUES (1);
             INSERT INTO page_tags VALUES ('science', 'physics');",
        )
        .unwrap();
    }
    let store = Landing::open(&path).unwrap();
    store.set_display_name("science", "Lab").unwrap();
    assert_eq!(store.display_name("science").unwrap().as_deref(), Some("Lab"));
    assert_eq!(store.tags_for_page("science").unwrap(), ["physics"]);
    drop(store);
    let version: i64 = Connection::open(&path).unwrap().query_row("SELECT version FROM schema_version", [], |r| r.get(0)).unwrap();
    assert_eq!(version, 2);
}

// --- test_landing_tags: TestSelectedTagsPruning --------------------------------------------

#[test]
fn test_selected_tag_survives_while_in_use() {
    let (_d, dao) = fresh();
    dao.add_tag("science", "physics").unwrap();
    dao.select_tag("physics").unwrap();
    assert_eq!(dao.selected_tags().unwrap(), ["physics"]);
}

#[test]
fn test_removing_last_use_prunes_selection() {
    let (_d, dao) = fresh();
    dao.add_tag("science", "physics").unwrap();
    dao.select_tag("physics").unwrap();
    dao.remove_tag("science", "physics").unwrap();
    assert!(dao.selected_tags().unwrap().is_empty());
}

#[test]
fn test_removing_one_of_several_pages_keeps_selection() {
    let (_d, dao) = fresh();
    dao.add_tag("science", "physics").unwrap();
    dao.add_tag("math", "physics").unwrap();
    dao.select_tag("physics").unwrap();
    dao.remove_tag("science", "physics").unwrap();
    assert_eq!(dao.selected_tags().unwrap(), ["physics"]);
    dao.remove_tag("math", "physics").unwrap();
    assert!(dao.selected_tags().unwrap().is_empty());
}

#[test]
fn test_stale_selection_from_before_the_fix_is_filtered_on_read() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("landing.db");
    let dao = Landing::open(&path).unwrap();
    Connection::open(&path).unwrap().execute("INSERT OR IGNORE INTO selected_tags (tag) VALUES (?1)", ["ghost"]).unwrap();
    assert!(dao.selected_tags().unwrap().is_empty());
}

// --- test_landing_rename: TestLandingDAODisplayNames ---------------------------------------

#[test]
fn test_no_override_by_default() {
    let (_d, dao) = fresh();
    assert_eq!(dao.display_name("science").unwrap(), None);
    assert!(dao.display_names(&["science".into()]).unwrap().is_empty());
}

#[test]
fn test_set_and_read_back() {
    let (_d, dao) = fresh();
    dao.set_display_name("science", "Science Lab").unwrap();
    assert_eq!(dao.display_name("science").unwrap().as_deref(), Some("Science Lab"));
    assert_eq!(dao.display_names(&["science".into(), "other".into()]).unwrap(), [("science".to_string(), "Science Lab".to_string())]);
}

#[test]
fn test_set_overwrites_existing() {
    let (_d, dao) = fresh();
    dao.set_display_name("science", "First").unwrap();
    dao.set_display_name("science", "Second").unwrap();
    assert_eq!(dao.display_name("science").unwrap().as_deref(), Some("Second"));
}

#[test]
fn test_clear_removes_override() {
    let (_d, dao) = fresh();
    dao.set_display_name("science", "Science Lab").unwrap();
    dao.clear_display_name("science").unwrap();
    assert_eq!(dao.display_name("science").unwrap(), None);
}

#[test]
fn test_set_blank_name_is_a_noop() {
    let (_d, dao) = fresh();
    dao.set_display_name("science", "   ").unwrap();
    assert_eq!(dao.display_name("science").unwrap(), None);
}

#[test]
fn test_display_names_bulk_empty_pages() {
    let (_d, dao) = fresh();
    assert!(dao.display_names(&[]).unwrap().is_empty());
}

#[test]
fn test_rename_does_not_touch_tags() {
    let (_d, dao) = fresh();
    dao.add_tag("science", "physics").unwrap();
    dao.set_display_name("science", "Science Lab").unwrap();
    assert_eq!(dao.tags_for_page("science").unwrap(), ["physics"]);
}
