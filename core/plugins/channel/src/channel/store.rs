//! Every channel this node holds, in `services/channel/channels.db`, read in place.
//!
//! A holder is a session on this node: `node` and `session` name it, and its lines are typed
//! into `session`. For a machine, `token` is the tether slot and `ref` the session id the
//! stream gave its mint. `trust` is the other side's level, always untrusted, and
//! `holder_level` the holder's own. `cursor` follows the conversation and `log_cursor` the tool
//! log, each in the daemon epoch beside it. `fed` is the relay's id of the last room message
//! typed, so a message the relay sends again after a restart is typed once.

use std::path::Path;
use std::sync::Mutex;

use rusqlite::{params, Connection, OptionalExtension, Row as SqlRow};
use serde_json::Value;

use crate::db;

pub const UNTRUSTED: &str = "untrusted";

const SCHEMA: &str = "CREATE TABLE IF NOT EXISTS channels (
    name      TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    trust     TEXT NOT NULL,
    node      TEXT NOT NULL,
    session   TEXT NOT NULL DEFAULT '',
    holder    TEXT NOT NULL DEFAULT '',
    link      TEXT NOT NULL DEFAULT '',
    token     TEXT NOT NULL DEFAULT '',
    cursor    INTEGER NOT NULL DEFAULT 0,
    open      INTEGER NOT NULL DEFAULT 1,
    ended     TEXT,
    opened_at REAL NOT NULL,
    closed_at REAL,
    crypto    TEXT,
    lasts     TEXT NOT NULL DEFAULT 'task',
    ref       TEXT NOT NULL DEFAULT '',
    epoch     INTEGER,
    log_cursor INTEGER NOT NULL DEFAULT 0,
    log_epoch INTEGER,
    holder_level TEXT NOT NULL DEFAULT 'untrusted',
    fed       INTEGER NOT NULL DEFAULT 0
)";

/// Columns a file written before they existed lacks.
const ADDED: [(&str, &str); 8] = [
    ("session", "TEXT NOT NULL DEFAULT ''"),
    ("lasts", "TEXT NOT NULL DEFAULT 'task'"),
    ("ref", "TEXT NOT NULL DEFAULT ''"),
    ("epoch", "INTEGER"),
    ("log_cursor", "INTEGER NOT NULL DEFAULT 0"),
    ("log_epoch", "INTEGER"),
    ("holder_level", "TEXT NOT NULL DEFAULT 'untrusted'"),
    ("fed", "INTEGER NOT NULL DEFAULT 0"),
];

#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    pub name: String,
    pub kind: String,
    pub trust: String,
    pub node: String,
    pub session: String,
    pub holder: String,
    pub link: String,
    pub token: String,
    pub cursor: i64,
    pub open: bool,
    pub ended: Option<String>,
    pub crypto: Option<Value>,
    pub lasts: String,
    pub r#ref: String,
    pub epoch: Option<i64>,
    pub log_cursor: i64,
    pub log_epoch: Option<i64>,
    pub holder_level: String,
    pub fed: i64,
}

impl Row {
    fn read(r: &SqlRow) -> rusqlite::Result<Self> {
        let crypto: Option<String> = r.get("crypto")?;
        Ok(Self {
            name: r.get("name")?,
            kind: r.get("kind")?,
            trust: r.get("trust")?,
            node: r.get("node")?,
            session: r.get("session")?,
            holder: r.get("holder")?,
            link: r.get("link")?,
            token: r.get("token")?,
            cursor: r.get("cursor")?,
            open: r.get::<_, i64>("open")? != 0,
            ended: r.get("ended")?,
            crypto: crypto.filter(|c| !c.is_empty()).and_then(|c| serde_json::from_str(&c).ok()),
            lasts: r.get("lasts")?,
            r#ref: r.get("ref")?,
            epoch: r.get("epoch")?,
            log_cursor: r.get("log_cursor")?,
            log_epoch: r.get("log_epoch")?,
            holder_level: r.get("holder_level")?,
            fed: r.get("fed")?,
        })
    }

    pub fn phase(&self) -> Option<&str> {
        self.crypto.as_ref().and_then(|c| c.get("phase")).and_then(Value::as_str)
    }

    /// The tether slot of a machine channel.
    pub fn slot(&self) -> Option<i64> {
        self.token.parse().ok()
    }
}

/// Who a new row is held by.
pub struct New<'a> {
    pub name: &'a str,
    pub kind: &'a str,
    pub holder: &'a super::Holder,
    pub link: &'a str,
    pub token: &'a str,
    pub lasts: &'a str,
    pub r#ref: &'a str,
    pub epoch: Option<i64>,
}

pub struct Store(Mutex<Connection>);

impl Store {
    pub fn open(path: &Path) -> anyhow::Result<Self> {
        let conn = db::open(path)?;
        conn.execute_batch(SCHEMA)?;
        db::add_columns(&conn, "channels", &ADDED)?;
        Ok(Self(Mutex::new(conn)))
    }

    fn db(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.0.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn rows(&self, sql: &str, args: impl rusqlite::Params) -> Vec<Row> {
        let db = self.db();
        let mut stmt = match db.prepare(sql) {
            Ok(s) => s,
            Err(e) => {
                tracing::error!("channels.db: {e}");
                return vec![];
            }
        };
        stmt.query_map(args, Row::read).map(|it| it.filter_map(Result::ok).collect()).unwrap_or_default()
    }

    pub fn add(&self, n: New) -> rusqlite::Result<Row> {
        self.db().execute(
            "INSERT OR REPLACE INTO channels (name, kind, trust, node, session, holder, link, token, opened_at, lasts, ref, epoch, log_epoch, holder_level)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?12, ?13)",
            params![n.name, n.kind, UNTRUSTED, n.holder.node, n.holder.session, n.holder.name, n.link, n.token, db::now(), n.lasts, n.r#ref, n.epoch, n.holder.level],
        )?;
        self.get(n.name).ok_or(rusqlite::Error::QueryReturnedNoRows)
    }

    pub fn get(&self, name: &str) -> Option<Row> {
        self.db().query_row("SELECT * FROM channels WHERE name = ?1", [name], Row::read).optional().ok().flatten()
    }

    pub fn open_rows(&self) -> Vec<Row> {
        self.rows("SELECT * FROM channels WHERE open = 1 ORDER BY opened_at", [])
    }

    pub fn of_holder(&self, node: &str, session: &str, open_only: bool) -> Vec<Row> {
        let open = if open_only { " AND open = 1" } else { "" };
        self.rows(&format!("SELECT * FROM channels WHERE node = ?1 AND session = ?2{open} ORDER BY opened_at"), [node, session])
    }

    pub fn set_cursor(&self, name: &str, cursor: i64) {
        let _ = self.db().execute("UPDATE channels SET cursor = ?1 WHERE name = ?2 AND cursor < ?1", params![cursor, name]);
    }

    /// The relay's id of the last room message typed into the holder. False when it was typed.
    pub fn set_fed(&self, name: &str, fed: i64) -> bool {
        self.db().execute("UPDATE channels SET fed = ?1 WHERE name = ?2 AND fed < ?1", params![fed, name]).is_ok_and(|n| n == 1)
    }

    /// A machine's conversation cursor, which starts again in a new epoch.
    pub fn set_follow(&self, name: &str, cursor: i64, epoch: Option<i64>) {
        let _ = self.db().execute("UPDATE channels SET cursor = ?1, epoch = ?2 WHERE name = ?3", params![cursor, epoch, name]);
    }

    pub fn set_log(&self, name: &str, cursor: i64, epoch: Option<i64>) {
        let _ = self.db().execute("UPDATE channels SET log_cursor = ?1, log_epoch = ?2 WHERE name = ?3", params![cursor, epoch, name]);
    }

    pub fn set_ref(&self, name: &str, r: &str) {
        let _ = self.db().execute("UPDATE channels SET ref = ?1 WHERE name = ?2", params![r, name]);
    }

    pub fn set_crypto(&self, name: &str, crypto: Option<&Value>) {
        let text = crypto.map(Value::to_string);
        let _ = self.db().execute("UPDATE channels SET crypto = ?1 WHERE name = ?2", params![text, name]);
    }

    /// Take the next outbound count of a keyed room: its key and the count, bumped in one step.
    pub fn next_out(&self, name: &str) -> Option<(String, i64)> {
        let db = self.db();
        let text: Option<String> = db.query_row("SELECT crypto FROM channels WHERE name = ?1", [name], |r| r.get(0)).optional().ok().flatten().flatten();
        let mut crypto: Value = serde_json::from_str(&text?).ok()?;
        let key = crypto.get("out")?.as_str()?.to_string();
        let n = crypto.get("n_out").and_then(Value::as_i64).unwrap_or(0);
        crypto["n_out"] = Value::from(n + 1);
        db.execute("UPDATE channels SET crypto = ?1 WHERE name = ?2", params![crypto.to_string(), name]).ok()?;
        Some((key, n))
    }

    /// Mark a channel closed and forget its slot and keys. False when it was closed already.
    pub fn close(&self, name: &str, why: &str) -> bool {
        self.db()
            .execute("UPDATE channels SET open = 0, ended = ?1, closed_at = ?2, token = '', crypto = NULL WHERE name = ?3 AND open = 1", params![why, db::now(), name])
            .is_ok_and(|n| n == 1)
    }
}
