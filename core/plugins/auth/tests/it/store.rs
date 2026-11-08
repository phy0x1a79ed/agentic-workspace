//! auth.db: the schema-7 file read in place, its migrations, the secret that fails closed, and
//! the settings rows (awm/services/auth/tests, migration and store tests).

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use awm_plugin_auth::store::{AuthSettings, AuthStore, AuthStoreError, SCHEMA_VERSION};
use awm_plugin_auth::tokens::AVATAR_STAGE;
use rusqlite::Connection;
use serde_json::{json, Value};

fn db_path(dir: &tempfile::TempDir) -> PathBuf {
    AuthStore::path_in(dir.path())
}

fn seed(path: &Path, sql: &str) {
    let conn = Connection::open(path).unwrap();
    conn.execute_batch(sql).unwrap();
}

fn tables(path: &Path) -> BTreeSet<String> {
    let conn = Connection::open(path).unwrap();
    let mut stmt = conn.prepare("SELECT name FROM sqlite_master WHERE type = 'table'").unwrap();
    let names = stmt.query_map([], |r| r.get::<_, String>(0)).unwrap();
    names.map(Result::unwrap).collect()
}

fn version(path: &Path) -> i64 {
    Connection::open(path).unwrap().query_row("SELECT version FROM schema_version", [], |r| r.get(0)).unwrap()
}

fn fresh() -> (tempfile::TempDir, AuthStore) {
    let dir = tempfile::tempdir().unwrap();
    let store = AuthStore::open(db_path(&dir), true).unwrap();
    (dir, store)
}

const USERS: &str = "CREATE TABLE awm_users (username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, pw_salt TEXT NOT NULL, \
pw_params TEXT NOT NULL, created_at REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);";
const LOGIN_FAIL: &str = "CREATE TABLE awm_login_fail (key TEXT PRIMARY KEY, fails INTEGER NOT NULL DEFAULT 0, \
locked_until REAL NOT NULL DEFAULT 0, last_at REAL NOT NULL DEFAULT 0);";
const CREDENTIALS: &str = "CREATE TABLE awm_credentials (generation INTEGER PRIMARY KEY AUTOINCREMENT, \
login_password TEXT NOT NULL, peer_credential TEXT NOT NULL, minted_at REAL NOT NULL, expires_at REAL NOT NULL);";
const SECRET: &str = "CREATE TABLE awm_secret (id INTEGER PRIMARY KEY CHECK (id = 1), secret TEXT NOT NULL);";
const DEVICES_V4: &str = "CREATE TABLE awm_device_tokens (id TEXT PRIMARY KEY, username TEXT NOT NULL, label TEXT NOT NULL, \
secret_sha256 TEXT NOT NULL, created_at REAL NOT NULL, last_used_at REAL, revoked_at REAL);";
const DEVICES_V5: &str = "CREATE TABLE awm_device_tokens (id TEXT PRIMARY KEY, username TEXT NOT NULL, label TEXT NOT NULL, \
secret_sha256 TEXT NOT NULL, created_at REAL NOT NULL, last_used_at REAL, revoked_at REAL, scope TEXT);";
const VERSION: &str = "CREATE TABLE schema_version (version INTEGER NOT NULL);";

// -- fail closed -------------------------------------------------------------------------------

#[test]
fn a_missing_db_is_refused_without_create() {
    let dir = tempfile::tempdir().unwrap();
    let err = AuthStore::open(db_path(&dir), false).expect_err("refused");
    assert!(matches!(err, AuthStoreError::Missing(_)), "{err}");
    assert!(err.to_string().contains("AWM_AUTH_INIT=1"));
    assert!(!db_path(&dir).exists(), "a refused open creates nothing");
}

#[test]
fn a_created_db_has_its_secret_and_keeps_it() {
    let (dir, store) = fresh();
    let secret = store.secret().unwrap();
    assert_eq!(secret.len(), 64, "token_urlsafe(48)");
    assert!(secret.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'));
    drop(store);
    for create in [false, true] {
        assert_eq!(AuthStore::open_for_start(db_path(&dir), create).unwrap().secret().unwrap(), secret);
    }
    assert_eq!(version(&db_path(&dir)), SCHEMA_VERSION);
}

#[test]
fn an_existing_db_with_no_secret_is_never_given_one() {
    let dir = tempfile::tempdir().unwrap();
    let path = db_path(&dir);
    seed(&path, &format!("{SECRET}{DEVICES_V5}{VERSION} INSERT INTO schema_version VALUES (7);"));
    for create in [false, true] {
        let err = AuthStore::open_for_start(&path, create).expect_err("refused");
        assert!(matches!(err, AuthStoreError::NoSecret(_)), "{err}");
    }
    let store = AuthStore::open(&path, true).unwrap();
    assert!(matches!(store.secret(), Err(AuthStoreError::NoSecret(_))));
    let n: i64 = Connection::open(&path).unwrap().query_row("SELECT COUNT(*) FROM awm_secret", [], |r| r.get(0)).unwrap();
    assert_eq!(n, 0);
}

#[test]
fn an_empty_file_where_a_db_stood_is_given_no_secret() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(db_path(&dir), b"").unwrap();
    assert!(matches!(AuthStore::open_for_start(db_path(&dir), true).err(), Some(AuthStoreError::NoSecret(_))));
}

#[test]
fn a_db_from_a_newer_binary_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    seed(&db_path(&dir), &format!("{SECRET}{VERSION} INSERT INTO schema_version VALUES (8);"));
    assert!(AuthStore::open(db_path(&dir), false).is_err());
}

#[test]
fn a_schema_7_db_is_read_in_place() {
    let dir = tempfile::tempdir().unwrap();
    let path = db_path(&dir);
    seed(
        &path,
        &format!(
            "{SECRET}{DEVICES_V5}{VERSION} INSERT INTO schema_version VALUES (7);
             INSERT INTO awm_secret VALUES (1, 'awm-test-secret-0001');
             INSERT INTO awm_device_tokens VALUES ('0123456789abcdef', 'device:0123456789abcdef', 'kiosk', 'd', 1.5, NULL, NULL, 'avatar-stage');
             CREATE TABLE awm_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
             INSERT INTO awm_settings VALUES ('service_config', '{{\"session_ttl_hours\": 12.0, \"max_session_days\": 30.0}}', '2026-01-01T00:00:00+00:00');"
        ),
    );
    let store = AuthStore::open_for_start(&path, false).unwrap();
    assert_eq!(store.secret().unwrap(), "awm-test-secret-0001");
    assert_eq!(store.settings().unwrap(), AuthSettings { session_ttl_hours: 12.0, max_session_days: 30.0 });
    let live = store.device_live().unwrap();
    assert_eq!(live.len(), 1);
    assert_eq!((live[0].created_at, live[0].scope.as_deref()), (1.5, Some(AVATAR_STAGE)));
    assert!(!dir.path().read_dir().unwrap().any(|e| e.unwrap().file_name().to_string_lossy().contains(".bak")), "no migration ran");
}

// -- settings ----------------------------------------------------------------------------------

#[test]
fn settings_are_written_as_python_wrote_them() {
    let v: Value = serde_json::from_str(include_str!("../vectors/auth_verbs.json")).unwrap();
    let (_d, store) = fresh();
    for case in v["saved"].as_array().unwrap() {
        let answer = awm_plugin_auth::verbs::config_set(&store, &json!({"values": case["patch"]})).unwrap();
        assert_eq!(answer, case["answer"], "{case}");
        let stored: String = store
            .db()
            .connection()
            .unwrap()
            .query_row("SELECT value FROM awm_settings WHERE key = 'service_config'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(stored, case["stored"].as_str().unwrap());
    }
}

#[test]
fn a_setting_pydantic_would_refuse_fails_the_read() {
    let (_d, store) = fresh();
    store
        .db()
        .connection()
        .unwrap()
        .execute_batch(
            "CREATE TABLE IF NOT EXISTS awm_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
             INSERT INTO awm_settings VALUES ('service_config', '{\"session_ttl_hours\": null}', 'x');",
        )
        .unwrap();
    assert!(store.settings().is_err());
}

// -- device rows -------------------------------------------------------------------------------

#[test]
fn revoke_keeps_the_first_revoked_at() {
    let (_d, store) = fresh();
    store.device_insert("0123456789abcdef", "device:0123456789abcdef", "a", "d", Some(AVATAR_STAGE), 10.0).unwrap();
    assert!(store.device_revoke("0123456789abcdef", 20.0).unwrap());
    assert!(store.device_revoke("0123456789abcdef", 30.0).unwrap());
    assert_eq!(store.device_get("0123456789abcdef").unwrap().unwrap().revoked_at, Some(20.0));
    assert!(!store.device_revoke("fedcba9876543210", 30.0).unwrap());
    assert!(store.device_live().unwrap().is_empty());
    assert_eq!(store.device_list().unwrap().len(), 1);
}

#[test]
fn touch_moves_last_used_forward_only() {
    let (_d, store) = fresh();
    store.device_insert("0123456789abcdef", "device:0123456789abcdef", "a", "d", None, 10.0).unwrap();
    let used = |at: f64| BTreeMap::from([("0123456789abcdef".to_string(), at), ("ffffffffffffffff".to_string(), at)]);
    assert_eq!(store.device_touch(&used(2000.0)).unwrap(), 1);
    assert_eq!(store.device_touch(&used(1000.0)).unwrap(), 0);
    assert_eq!(store.device_touch(&BTreeMap::new()).unwrap(), 0);
    assert_eq!(store.device_get("0123456789abcdef").unwrap().unwrap().last_used_at, Some(2000.0));
}

// -- migration (test_no_login.py and test_device_tokens.py) ------------------------------------

#[test]
fn test_v1_db_migrates_in_place() {
    let dir = tempfile::tempdir().unwrap();
    let path = db_path(&dir);
    seed(&path, &format!("{SECRET}{CREDENTIALS}{VERSION} INSERT INTO schema_version VALUES (1); INSERT INTO awm_secret VALUES (1, 'keep-me');"));
    let store = AuthStore::open_for_start(&path, false).unwrap();
    assert_eq!(store.secret().unwrap(), "keep-me");
    let t = tables(&path);
    assert!(["awm_users", "awm_login_fail", "awm_credentials"].iter().all(|n| !t.contains(*n)), "{t:?}");
    assert_eq!(version(&path), 7);
}

#[test]
fn test_a_v3_db_gains_the_device_token_table() {
    let dir = tempfile::tempdir().unwrap();
    let path = db_path(&dir);
    seed(
        &path,
        &format!("{SECRET}{CREDENTIALS}{USERS}{LOGIN_FAIL}{VERSION} INSERT INTO schema_version (version) VALUES (3); INSERT INTO awm_users VALUES ('tony', 'h', 's', 'p', 0, 0);"),
    );
    let store = AuthStore::open(&path, false).unwrap();
    assert!(store.device_list().unwrap().is_empty());
    store.device_insert("0123456789abcdef", "tony", "x", "d", None, 1.0).unwrap();
    let live: Vec<String> = store.device_live().unwrap().into_iter().map(|t| t.id).collect();
    assert_eq!(live, ["0123456789abcdef"]);
}

#[test]
fn test_a_v4_db_gains_the_scope_column() {
    let dir = tempfile::tempdir().unwrap();
    let path = db_path(&dir);
    seed(
        &path,
        &format!(
            "{USERS}{DEVICES_V4}{VERSION} INSERT INTO schema_version (version) VALUES (4);
             INSERT INTO awm_users VALUES ('tony', 'h', 's', 'p', 0, 0);
             INSERT INTO awm_device_tokens VALUES ('0123456789abcdef', 'tony', 'x', 'd', 0, NULL, NULL);"
        ),
    );
    let store = AuthStore::open(&path, false).unwrap();
    assert_eq!(store.device_get("0123456789abcdef").unwrap().unwrap().scope, None);
    store.device_insert("fedcba9876543210", "tony", "y", "e", Some(AVATAR_STAGE), 1.0).unwrap();
    assert_eq!(store.device_get("fedcba9876543210").unwrap().unwrap().scope.as_deref(), Some(AVATAR_STAGE));
}

#[test]
fn test_the_v5_db_drops_the_users_and_the_plaintext_penpot_tables() {
    let dir = tempfile::tempdir().unwrap();
    let path = db_path(&dir);
    seed(
        &path,
        &format!(
            "{USERS}{DEVICES_V5}
             CREATE TABLE awm_penpot (username TEXT PRIMARY KEY, email TEXT NOT NULL, password TEXT NOT NULL, rotated_at REAL NOT NULL, created_at REAL NOT NULL);
             INSERT INTO awm_penpot VALUES ('tony', 't@x', 'plaintext', 0, 0);
             {VERSION} INSERT INTO schema_version (version) VALUES (5);"
        ),
    );
    AuthStore::open(&path, false).unwrap();
    let t = tables(&path);
    assert!(!t.contains("awm_penpot") && !t.contains("awm_users"));
    assert_eq!(version(&path), 7);
}

#[test]
fn test_a_v5_db_drops_its_static_accounts() {
    let dir = tempfile::tempdir().unwrap();
    let path = db_path(&dir);
    seed(
        &path,
        &format!(
            "{USERS}{LOGIN_FAIL}{DEVICES_V5}{VERSION}
             INSERT INTO schema_version (version) VALUES (5);
             INSERT INTO awm_users VALUES ('tony', 'h', 's', 'p', 0, 0);
             INSERT INTO awm_users VALUES ('ada', 'h', 's', 'p', 0, 1);
             INSERT INTO awm_login_fail VALUES ('ip:9.9.9.9', 2, 0, 0);
             INSERT INTO awm_device_tokens VALUES
             ('aaaaaaaaaaaaaaaa', 'tony', 'live', 'd', 1, NULL, NULL, 'avatar-stage'),
             ('eeeeeeeeeeeeeeee', 'tony', 'unscoped', 'd', 5, NULL, NULL, NULL),
             ('bbbbbbbbbbbbbbbb', 'ada', 'disabled owner', 'd', 2, NULL, NULL, NULL),
             ('cccccccccccccccc', 'ghost', 'missing owner', 'd', 3, NULL, NULL, NULL),
             ('dddddddddddddddd', 'tony', 'revoked', 'd', 4, NULL, 7.0, NULL);"
        ),
    );
    let store = AuthStore::open(&path, false).unwrap();
    let t = tables(&path);
    assert!(["awm_users", "awm_login_fail", "awm_credentials"].iter().all(|n| !t.contains(*n)));
    let live: Vec<String> = store.device_live().unwrap().into_iter().map(|t| t.id).collect();
    assert_eq!(live, ["aaaaaaaaaaaaaaaa"]);
    let rows: BTreeMap<String, Option<f64>> = store.device_list().unwrap().into_iter().map(|t| (t.id, t.revoked_at)).collect();
    for id in ["bbbbbbbbbbbbbbbb", "cccccccccccccccc", "eeeeeeeeeeeeeeee"] {
        assert!(rows[id].is_some(), "{id}");
    }
    assert_eq!(rows["dddddddddddddddd"], Some(7.0));
}

#[test]
fn test_a_v6_db_drops_its_credentials_and_lockout() {
    let dir = tempfile::tempdir().unwrap();
    let path = db_path(&dir);
    seed(
        &path,
        &format!(
            "{CREDENTIALS}{LOGIN_FAIL}{DEVICES_V5}{VERSION}
             INSERT INTO schema_version (version) VALUES (6);
             INSERT INTO awm_credentials VALUES (1, 'typed-daily', 'peer-x', 0, 9e12);"
        ),
    );
    AuthStore::open(&path, false).unwrap();
    let t = tables(&path);
    assert!(!t.contains("awm_credentials") && !t.contains("awm_login_fail"));
}
