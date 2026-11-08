//! `rlm_sessions` in the service's own `rlm-browser.db`, read in place.
//!
//! One row is one realm session: how to reach its Chrome again after a restart. Tabs are not
//! stored, since they are live CDP targets. v3 adds `guest_context`, the browser context a
//! guest of an attached Chrome is confined to.

use std::path::Path;

use awm_store::rusqlite::{self, params, Connection, OptionalExtension};
use awm_store::{InitOutcome, Migration, StoreError};
use serde_json::{json, Value};

pub const SCHEMA_VERSION: i64 = 3;

const SCHEMA_SQL: &str = "\
CREATE TABLE IF NOT EXISTS rlm_sessions (
    session_id      TEXT NOT NULL PRIMARY KEY,
    game            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active',
    mode            TEXT NOT NULL DEFAULT 'headless',
    backend         TEXT NOT NULL DEFAULT 'host',
    cdp_port        INTEGER NOT NULL DEFAULT 0,
    user_data_dir   TEXT NOT NULL DEFAULT '',
    container_name  TEXT NOT NULL DEFAULT '',
    vnc_port        INTEGER NOT NULL DEFAULT 0,
    egress          TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    guest_context   TEXT NOT NULL DEFAULT ''
);";

/// Each step takes the database from version `from` to the next.
const MIGRATIONS: [Migration; 2] = [
    Migration {
        from: 1,
        sql: "ALTER TABLE rlm_sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'headless';
ALTER TABLE rlm_sessions ADD COLUMN backend TEXT NOT NULL DEFAULT 'host';
ALTER TABLE rlm_sessions ADD COLUMN cdp_port INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rlm_sessions ADD COLUMN user_data_dir TEXT NOT NULL DEFAULT '';
ALTER TABLE rlm_sessions ADD COLUMN container_name TEXT NOT NULL DEFAULT '';
ALTER TABLE rlm_sessions ADD COLUMN vnc_port INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rlm_sessions ADD COLUMN egress TEXT NOT NULL DEFAULT '';",
    },
    Migration { from: 2, sql: "ALTER TABLE rlm_sessions ADD COLUMN guest_context TEXT NOT NULL DEFAULT '';" },
];

const COLS: &str = "session_id, game, status, mode, backend, cdp_port, user_data_dir, container_name, vnc_port, egress, created_at, updated_at";

#[derive(Clone)]
pub struct Store {
    db: awm_store::Store,
}

/// Python's `datetime.now(timezone.utc).isoformat()`.
pub fn now() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string()
}

/// Whether the file holds the sessions table but no `schema_version`. Its version cannot be
/// told, and awm-store would stamp it current.
fn unversioned(conn: &Connection) -> rusqlite::Result<bool> {
    let has = |name: &str| conn.query_row("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1", [name], |_| Ok(())).optional().map(|r| r.is_some());
    Ok(has("rlm_sessions")? && !has("schema_version")?)
}

impl Store {
    /// Open the database, create it when it is new, and migrate an older one after a backup.
    pub fn open(path: &Path) -> anyhow::Result<Self> {
        let db = awm_store::Store::open(path)?;
        if db.blocking_read(|c| unversioned(c).map_err(StoreError::from))? {
            anyhow::bail!("{} holds rlm_sessions but no schema_version: refusing to guess its version", path.display());
        }
        if let InitOutcome::Migrated { from, to, backup } = db.init(SCHEMA_SQL, SCHEMA_VERSION, &MIGRATIONS)? {
            tracing::info!(from, to, backup = %backup.display(), "migrated rlm-browser.db");
        }
        Ok(Store { db })
    }

    async fn run<T: Send + 'static>(&self, f: impl FnOnce(&Connection) -> rusqlite::Result<T> + Send + 'static) -> anyhow::Result<T> {
        Ok(self.db.write(move |tx| f(tx).map_err(StoreError::from)).await?)
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn create(&self, game: &str, mode: &str, backend: &str, cdp_port: u16, user_data_dir: &str, container_name: &str, vnc_port: u16, egress: Option<Value>, guest_context: &str) -> anyhow::Result<Value> {
        let id = format!("rlm-browser-{}", &uuid::Uuid::new_v4().simple().to_string()[..12]);
        let egress = egress.filter(|e| !e.is_null() && e != &json!({})).map(|e| e.to_string()).unwrap_or_default();
        let (sid, game, mode, backend, udd, cname, guest, at) = (id.clone(), game.trim().to_string(), mode.to_string(), backend.to_string(), user_data_dir.to_string(), container_name.to_string(), guest_context.to_string(), now());
        self.run(move |c| {
            c.execute(
                "INSERT INTO rlm_sessions (session_id, game, status, mode, backend, cdp_port, user_data_dir, container_name, vnc_port, egress, created_at, updated_at, guest_context) VALUES (?1, ?2, 'active', ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?10, ?11)",
                params![sid, game, mode, backend, cdp_port, udd, cname, vnc_port, egress, at, guest],
            )
            .map(|_| ())
        })
        .await?;
        self.get(&id).await?.ok_or_else(|| anyhow::anyhow!("the session row {id} vanished"))
    }

    pub async fn get(&self, session_id: &str) -> anyhow::Result<Option<Value>> {
        let id = session_id.trim().to_string();
        if id.is_empty() {
            return Ok(None);
        }
        self.run(move |c| c.query_row(&format!("SELECT {COLS}, guest_context FROM rlm_sessions WHERE session_id = ?1"), [id], row).optional()).await
    }

    pub async fn list(&self) -> anyhow::Result<Vec<Value>> {
        self.run(|c| {
            let mut st = c.prepare(&format!("SELECT {COLS}, guest_context FROM rlm_sessions ORDER BY created_at"))?;
            let rows = st.query_map([], row)?.collect::<rusqlite::Result<Vec<_>>>();
            rows
        })
        .await
    }

    pub async fn set_status(&self, session_id: &str, status: &str) -> anyhow::Result<()> {
        let (id, status, at) = (session_id.trim().to_string(), status.trim().to_string(), now());
        self.run(move |c| c.execute("UPDATE rlm_sessions SET status = ?1, updated_at = ?2 WHERE session_id = ?3", params![status, at, id]).map(|_| ())).await
    }

    pub async fn delete(&self, session_id: &str) -> anyhow::Result<bool> {
        let id = session_id.trim().to_string();
        self.run(move |c| c.execute("DELETE FROM rlm_sessions WHERE session_id = ?1", [id]).map(|n| n > 0)).await
    }
}

/// A row as Python's DAO returned it, `egress` decoded. `guest_context` stays internal.
fn row(r: &rusqlite::Row) -> rusqlite::Result<Value> {
    let egress: String = r.get(9)?;
    let egress = if egress.is_empty() { Value::Null } else { serde_json::from_str(&egress).unwrap_or(Value::Null) };
    Ok(json!({
        "session_id": r.get::<_, String>(0)?,
        "game": r.get::<_, String>(1)?,
        "status": r.get::<_, String>(2)?,
        "mode": r.get::<_, String>(3)?,
        "backend": r.get::<_, String>(4)?,
        "cdp_port": r.get::<_, i64>(5)?,
        "user_data_dir": r.get::<_, String>(6)?,
        "container_name": r.get::<_, String>(7)?,
        "vnc_port": r.get::<_, i64>(8)?,
        "egress": egress,
        "created_at": r.get::<_, String>(10)?,
        "updated_at": r.get::<_, String>(11)?,
        "_guest_context": r.get::<_, String>(12)?,
    }))
}

/// A row without its internal keys, as `status` shows it.
pub fn public(mut row: Value) -> Value {
    if let Some(m) = row.as_object_mut() {
        m.remove("_guest_context");
    }
    row
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_v2_database_migrates_in_place_after_a_backup() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("rlm-browser.db");
        {
            let c = Connection::open(&path).unwrap();
            c.execute_batch(&SCHEMA_SQL.replace(",\n    guest_context   TEXT NOT NULL DEFAULT ''", "")).unwrap();
            c.execute_batch("CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version VALUES (2);").unwrap();
            c.execute("INSERT INTO rlm_sessions (session_id, game, cdp_port) VALUES ('rlm-browser-old', 'chess', 9333)", []).unwrap();
        }
        let s = Store::open(&path).unwrap();
        let row = s.get("rlm-browser-old").await.unwrap().unwrap();
        assert_eq!((row["game"].as_str(), row["cdp_port"].as_i64(), row["_guest_context"].as_str()), (Some("chess"), Some(9333), Some("")));
        assert!(tmp.path().join("rlm-browser.db.v2.bak").exists());
        let v: i64 = s.db.blocking_read(|c| c.query_row("SELECT version FROM schema_version", [], |r| r.get(0)).map_err(StoreError::from)).unwrap();
        assert_eq!(v, 3);
    }

}
