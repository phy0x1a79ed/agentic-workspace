use awm_store::settings::{self, Contract};
use awm_store::{InitOutcome, Migration, Store, StoreError};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

const SCHEMA: &str = "CREATE TABLE posts (id TEXT PRIMARY KEY, body TEXT NOT NULL, n INTEGER);";

fn version(store: &Store) -> i64 {
    store
        .blocking_read(|c| c.query_row("SELECT version FROM schema_version", [], |r| r.get(0)).map_err(StoreError::from))
        .unwrap()
}

fn columns(store: &Store, table: &str) -> Vec<String> {
    store
        .blocking_read(|c| {
            let mut stmt = c.prepare(&format!("PRAGMA table_info({table})"))?;
            let names = stmt.query_map([], |r| r.get::<_, String>(1))?.collect::<Result<Vec<_>, _>>()?;
            Ok::<_, StoreError>(names)
        })
        .unwrap()
}

#[test]
fn a_fresh_database_gets_the_schema_and_its_version() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path().join("nested/svc.db")).unwrap();
    assert_eq!(store.init(SCHEMA, 3, &[]).unwrap(), InitOutcome::Created { version: 3 });
    assert_eq!(version(&store), 3);
    assert_eq!(store.init(SCHEMA, 3, &[]).unwrap(), InitOutcome::Current { version: 3 });
    let mode: String = store.blocking_read(|c| c.pragma_query_value(None, "journal_mode", |r| r.get(0)).map_err(StoreError::from)).unwrap();
    assert_eq!(mode, "wal");
}

#[test]
fn a_migration_backs_up_then_runs_each_step() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("svc.db");
    let store = Store::open(&path).unwrap();
    store.init(SCHEMA, 1, &[]).unwrap();
    store.blocking_write(|tx| tx.execute("INSERT INTO posts VALUES ('a', 'x', 1)", []).map_err(StoreError::from)).unwrap();
    let steps = [
        Migration { from: 1, sql: "ALTER TABLE posts ADD COLUMN tag TEXT;" },
        Migration { from: 2, sql: "CREATE TABLE extra (k TEXT);" },
    ];
    let out = store.init(SCHEMA, 3, &steps).unwrap();
    let InitOutcome::Migrated { from: 1, to: 3, backup } = out else { panic!("{out:?}") };
    assert_eq!(backup, dir.path().join("svc.db.v1.bak"));
    let old = Store::open_ro(&backup).unwrap();
    assert_eq!(version(&old), 1);
    assert_eq!(columns(&old, "posts"), ["id", "body", "n"]);
    assert_eq!(version(&store), 3);
    assert_eq!(columns(&store, "posts"), ["id", "body", "n", "tag"]);
    assert_eq!(columns(&store, "extra"), ["k"]);
}

#[test]
fn a_second_backup_of_one_version_never_overwrites_the_first() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("svc.db");
    let store = Store::open(&path).unwrap();
    store.init(SCHEMA, 1, &[]).unwrap();
    std::fs::write(dir.path().join("svc.db.v1.bak"), b"earlier").unwrap();
    let out = store.init(SCHEMA, 2, &[Migration { from: 1, sql: "CREATE TABLE t2 (a);" }]).unwrap();
    let InitOutcome::Migrated { backup, .. } = out else { panic!() };
    let name = backup.file_name().unwrap().to_string_lossy().into_owned();
    assert!(name.starts_with("svc.db.v1.") && name.ends_with(".bak") && name != "svc.db.v1.bak", "{name}");
    assert_eq!(std::fs::read(dir.path().join("svc.db.v1.bak")).unwrap(), b"earlier");
}

#[test]
fn a_step_a_crash_half_applied_resumes_as_done() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path().join("svc.db")).unwrap();
    store.init("CREATE TABLE posts (id TEXT, tag TEXT);", 1, &[]).unwrap();
    let steps = [
        Migration { from: 1, sql: "ALTER TABLE posts ADD COLUMN tag TEXT;" },
        Migration { from: 2, sql: "ALTER TABLE ghost ADD COLUMN x TEXT;" },
        Migration { from: 3, sql: "CREATE TABLE after (a);" },
    ];
    store.init("", 4, &steps).unwrap();
    assert_eq!(version(&store), 4);
    assert_eq!(columns(&store, "after"), ["a"]);
}

#[test]
fn a_failing_step_stops_at_the_last_good_version_and_keeps_its_partial_work() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path().join("svc.db")).unwrap();
    store.init(SCHEMA, 1, &[]).unwrap();
    let steps = [
        Migration { from: 1, sql: "CREATE TABLE ok (a);" },
        Migration { from: 2, sql: "CREATE TABLE partial (a); THIS IS NOT SQL;" },
    ];
    assert!(store.init(SCHEMA, 3, &steps).is_err());
    assert_eq!(version(&store), 2);
    assert_eq!(columns(&store, "partial"), ["a"], "autocommit keeps the statements before the failure");
}

#[test]
fn a_foreign_key_check_that_finds_rows_does_not_fail_the_step() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path().join("svc.db")).unwrap();
    store
        .init("CREATE TABLE p (id INTEGER PRIMARY KEY); CREATE TABLE c (pid INTEGER REFERENCES p(id));", 1, &[])
        .unwrap();
    let steps = [Migration { from: 1, sql: "INSERT INTO c VALUES (42);" }];
    store.init("", 2, &steps).unwrap();
    assert_eq!(version(&store), 2);
}

#[test]
fn a_newer_database_is_refused_and_a_gap_in_the_steps_is_an_error() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path().join("svc.db")).unwrap();
    store.init(SCHEMA, 5, &[]).unwrap();
    assert!(matches!(store.init(SCHEMA, 4, &[]), Err(StoreError::VersionAhead { found: 5, ours: 4, .. })));
    assert!(matches!(store.init(SCHEMA, 7, &[]), Err(StoreError::NoMigration { from: 5, to: 6 })));
}

#[test]
fn a_database_replaced_on_disk_is_read_fresh() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("svc.db");
    let store = Store::open(&path).unwrap();
    store.init(SCHEMA, 1, &[]).unwrap();
    store.blocking_write(|tx| tx.execute("INSERT INTO posts VALUES ('old', 'x', 1)", []).map_err(StoreError::from)).unwrap();

    let other = dir.path().join("other.db");
    let restored = Store::open(&other).unwrap();
    restored.init(SCHEMA, 1, &[]).unwrap();
    restored.blocking_write(|tx| tx.execute("INSERT INTO posts VALUES ('new', 'y', 2)", []).map_err(StoreError::from)).unwrap();
    restored.blocking_read(|c| c.execute_batch("PRAGMA wal_checkpoint(TRUNCATE)").map_err(StoreError::from)).unwrap();
    drop(restored);
    for ext in ["", "-wal", "-shm"] {
        let _ = std::fs::remove_file(format!("{}{ext}", path.display()));
    }
    std::fs::rename(&other, &path).unwrap();

    let id: String = store.blocking_read(|c| c.query_row("SELECT id FROM posts", [], |r| r.get(0)).map_err(StoreError::from)).unwrap();
    assert_eq!(id, "new");
}

#[test]
fn rows_come_back_as_json_in_column_order_and_a_blob_is_an_error() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path().join("svc.db")).unwrap();
    let rows = store
        .blocking_read(|c| {
            let mut stmt = c.prepare("SELECT 'a' AS z, 1 AS a, 2.5 AS m, NULL AS n")?;
            awm_store::rows_json(&mut stmt, [])
        })
        .unwrap();
    assert_eq!(serde_json::to_string(&rows).unwrap(), r#"[{"z":"a","a":1,"m":2.5,"n":null}]"#);
    let blob = store.blocking_read(|c| {
        let mut stmt = c.prepare("SELECT x'00' AS b")?;
        awm_store::rows_json(&mut stmt, [])
    });
    assert!(matches!(blob, Err(StoreError::Blob { .. })));
}

#[derive(Serialize, Deserialize, Debug, PartialEq)]
#[serde(default)]
struct Shape {
    enabled: bool,
    limit: u32,
}

impl Default for Shape {
    fn default() -> Self {
        Shape { enabled: true, limit: 10 }
    }
}

#[test]
fn a_contract_merges_defaults_validates_and_stores_python_bytes() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(dir.path().join("svc.db")).unwrap();
    let contract: Contract<Shape> = Contract::new(store.clone(), "svc", json!({"type": "object"}));
    assert_eq!(contract.load().unwrap(), Shape { enabled: true, limit: 10 });
    let saved = contract.set(&json!({"values": {"limit": 3, "stale": 1}})).unwrap();
    assert_eq!(saved["values"], json!({"enabled": true, "limit": 3}));
    assert!(contract.save(json!({"limit": "x"}).as_object().unwrap()).is_err());
    let raw: String = store
        .blocking_read(|c| c.query_row("SELECT value FROM awm_settings WHERE key = 'service_config'", [], |r| r.get(0)).map_err(StoreError::from))
        .unwrap();
    assert_eq!(raw, r#"{"enabled": true, "limit": 3}"#);
    assert_eq!(settings::read(&store, "absent").unwrap(), serde_json::Map::new());
    let _: Value = contract.get().unwrap();
}

