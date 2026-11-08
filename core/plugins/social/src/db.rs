//! `social.db` (`dao.py`): operators, a bot's synced history, mounts, quiet threads, and the
//! seats' tables.
//!
//! The Python schema is v8. v9 adds the seat tables and v10 the seats' Remote Control columns,
//! and nothing else: the keeper's tables and `social_accounts` stay, unread. Every query runs on the blocking pool, off the async loop.

use std::path::Path;
use std::sync::{Arc, Mutex};

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

pub const SCHEMA_VERSION: i64 = 10;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS social_accounts (
    name         TEXT NOT NULL PRIMARY KEY,
    platform     TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'bot',
    display_name TEXT NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1,
    added_at     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS social_operators (
    platform         TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    awm_user         TEXT NOT NULL DEFAULT '',
    added_at         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (platform, platform_user_id)
);
";

const HISTORY: &str = "
CREATE TABLE IF NOT EXISTS social_history (
    account     TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    thread_id   TEXT NOT NULL DEFAULT '',
    sender_id   TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '[]',
    ts          TEXT NOT NULL DEFAULT '',
    own         INTEGER NOT NULL DEFAULT 0,
    edited      INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account, channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS ix_social_history_ts ON social_history (account, ts);
CREATE TABLE IF NOT EXISTS social_sync (
    account      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    channel_name TEXT NOT NULL DEFAULT '',
    synced_ts    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account, channel_id)
);
";

const MOUNTS: &str = "
CREATE TABLE IF NOT EXISTS social_mounts (
    account      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    channel_name TEXT NOT NULL DEFAULT '',
    node         TEXT NOT NULL,
    job          TEXT NOT NULL,
    session_name TEXT NOT NULL DEFAULT '',
    mounted_at   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account, channel_id)
);
";

const KEEPERS_V5: &str = "
CREATE TABLE IF NOT EXISTS social_keepers (
    seat         TEXT NOT NULL PRIMARY KEY,
    node         TEXT NOT NULL,
    job          TEXT NOT NULL,
    spec_hash    TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL DEFAULT ''
);
";

const KEEPER_STATE: &str = "
CREATE TABLE IF NOT EXISTS social_keeper_state (
    seat         TEXT NOT NULL PRIMARY KEY,
    state        TEXT NOT NULL DEFAULT '{}',
    updated_at   TEXT NOT NULL DEFAULT ''
);
";

const QUIET: &str = "
CREATE TABLE IF NOT EXISTS social_quiet_threads (
    account      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    thread_id    TEXT NOT NULL,
    quiet_at     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account, channel_id, thread_id)
);
";

/// v9: the seats (`seats/` owns every row).
pub const SEATS: &str = "
CREATE TABLE IF NOT EXISTS seat_registry (
    account      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    channel_name TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'channel',
    server       TEXT NOT NULL DEFAULT '',
    slug         TEXT NOT NULL DEFAULT '',
    node         TEXT NOT NULL DEFAULT '',
    scope        TEXT NOT NULL DEFAULT '',
    level        TEXT NOT NULL DEFAULT 'untrusted',
    state        TEXT NOT NULL DEFAULT 'empty',
    job          TEXT NOT NULL DEFAULT '',
    job_node     TEXT NOT NULL DEFAULT '',
    adopted      INTEGER NOT NULL DEFAULT 0,
    since        REAL NOT NULL DEFAULT 0,
    last_message REAL NOT NULL DEFAULT 0,
    done_at      REAL NOT NULL DEFAULT 0,
    deadline     REAL NOT NULL DEFAULT 0,
    previous     TEXT NOT NULL DEFAULT '{}',
    breaker      TEXT NOT NULL DEFAULT '{}',
    held         TEXT NOT NULL DEFAULT '',
    replied      INTEGER NOT NULL DEFAULT 0,
    missed       INTEGER NOT NULL DEFAULT 0,
    name         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account, channel_id)
);
CREATE TABLE IF NOT EXISTS seat_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    message_id   TEXT NOT NULL DEFAULT '',
    thread_id    TEXT NOT NULL DEFAULT '',
    line         TEXT NOT NULL,
    queued_at    REAL NOT NULL,
    briefed_to   TEXT NOT NULL DEFAULT '',
    delivered_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_seat_queue_chat ON seat_queue (account, channel_id, delivered_at);
CREATE TABLE IF NOT EXISTS seat_routed (
    account      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    message_id   TEXT NOT NULL,
    routed_at    REAL NOT NULL,
    PRIMARY KEY (account, channel_id, message_id)
);
CREATE TABLE IF NOT EXISTS seat_lines (
    node         TEXT NOT NULL,
    job          TEXT NOT NULL,
    account      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    thread_id    TEXT NOT NULL DEFAULT '',
    message_id   TEXT NOT NULL DEFAULT '',
    at           REAL NOT NULL,
    PRIMARY KEY (node, job)
);
CREATE TABLE IF NOT EXISTS seat_reads (
    seat_account TEXT NOT NULL,
    seat_channel TEXT NOT NULL,
    account      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    until        REAL NOT NULL,
    PRIMARY KEY (seat_account, seat_channel, account, channel_id)
);
CREATE TABLE IF NOT EXISTS seat_cursor (
    account      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    last_id      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account, channel_id)
);
";

/// v10: each seat's Remote Control switch, and its live session's claude.ai link.
pub const SEATS_V10: &str = "
ALTER TABLE seat_registry ADD COLUMN remote_control INTEGER NOT NULL DEFAULT 0;
ALTER TABLE seat_registry ADD COLUMN rc_url TEXT NOT NULL DEFAULT '';
";

/// The Python migrations, kept so a DB of any old version opens. Each is additive except
/// 1 -> 2, which dropped a table v1 had and nothing since reads.
fn migration(from: i64) -> String {
    match from {
        1 => "DROP INDEX IF EXISTS ux_social_messages_dedupe;\nDROP INDEX IF EXISTS ix_social_messages_channel;\nDROP TABLE IF EXISTS social_messages;\n".into(),
        2 => HISTORY.into(),
        3 => MOUNTS.into(),
        4 => KEEPERS_V5.into(),
        5 => "ALTER TABLE social_keepers ADD COLUMN seen_at TEXT NOT NULL DEFAULT '';\n".into(),
        6 => KEEPER_STATE.into(),
        7 => QUIET.into(),
        // Every message the Python saw counts as routed, so the first resync replays none.
        8 => format!("{SEATS}INSERT OR IGNORE INTO seat_routed (account, channel_id, message_id, routed_at) SELECT account, channel_id, message_id, 0 FROM social_history;\n"),
        9 => SEATS_V10.into(),
        _ => String::new(),
    }
}

pub fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}

pub fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

fn version(conn: &Connection) -> rusqlite::Result<Option<i64>> {
    let has: bool = conn.query_row("SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'", [], |r| r.get::<_, i64>(0))? > 0;
    if !has {
        return Ok(None);
    }
    conn.query_row("SELECT max(version) FROM schema_version", [], |r| r.get(0))
}

fn tables(conn: &Connection) -> rusqlite::Result<i64> {
    conn.query_row("SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'", [], |r| r.get(0))
}

/// Open `social.db`, creating it at the current version or carrying an older one forward by its migrations.
pub fn open_conn(path: &Path) -> anyhow::Result<Connection> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let conn = Connection::open(path)?;
    conn.busy_timeout(std::time::Duration::from_secs(5))?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "foreign_keys", "ON")?;
    match version(&conn)? {
        None if tables(&conn)? == 0 => {
            conn.execute_batch(&format!("{SCHEMA}{HISTORY}{MOUNTS}{KEEPERS_V5}{KEEPER_STATE}{QUIET}{SEATS}{SEATS_V10}"))?;
            conn.execute_batch("ALTER TABLE social_keepers ADD COLUMN seen_at TEXT NOT NULL DEFAULT '';")?;
            conn.execute_batch("CREATE TABLE schema_version (version INTEGER NOT NULL);")?;
            conn.execute("INSERT INTO schema_version (version) VALUES (?1)", [SCHEMA_VERSION])?;
        }
        None => anyhow::bail!("{}: has tables but no schema_version, so it is not a social.db this build knows", path.display()),
        Some(v) if v > SCHEMA_VERSION => anyhow::bail!("{}: schema v{v} is newer than this build's v{SCHEMA_VERSION}", path.display()),
        Some(v) => {
            for from in v..SCHEMA_VERSION {
                let tx = conn.unchecked_transaction()?;
                tx.execute_batch(&migration(from))?;
                tx.execute("UPDATE schema_version SET version = ?1", [from + 1])?;
                tx.commit()?;
            }
        }
    }
    Ok(conn)
}

/// The database, shared. Each call runs its closure on the blocking pool.
#[derive(Clone)]
pub struct Db(Arc<Mutex<Connection>>);

impl Db {
    pub fn open(path: &Path) -> anyhow::Result<Self> {
        Ok(Self(Arc::new(Mutex::new(open_conn(path)?))))
    }

    pub async fn run<R, F>(&self, f: F) -> R
    where
        F: FnOnce(&Connection) -> R + Send + 'static,
        R: Send + 'static,
    {
        let me = self.0.clone();
        tokio::task::spawn_blocking(move || f(&me.lock().unwrap_or_else(|p| p.into_inner()))).await.expect("a db call panicked")
    }

    // -- operators ----------------------------------------------------------

    pub async fn add_operator(&self, platform: &str, uid: &str, user: &str) -> rusqlite::Result<Value> {
        let (p, u, a, at) = (platform.trim().to_string(), uid.trim().to_string(), user.trim().to_string(), now_iso());
        self.run(move |c| {
            c.execute(
                "INSERT INTO social_operators (platform, platform_user_id, awm_user, added_at) VALUES (?1, ?2, ?3, ?4) \
                 ON CONFLICT(platform, platform_user_id) DO UPDATE SET awm_user = excluded.awm_user",
                params![p, u, a, at],
            )?;
            Ok(json!({"platform": p, "platform_user_id": u, "awm_user": a, "added_at": at}))
        })
        .await
    }

    pub async fn remove_operator(&self, platform: &str, uid: &str) -> rusqlite::Result<bool> {
        let (p, u) = (platform.trim().to_string(), uid.trim().to_string());
        self.run(move |c| Ok(c.execute("DELETE FROM social_operators WHERE platform = ?1 AND platform_user_id = ?2", params![p, u])? > 0)).await
    }

    pub async fn list_operators(&self, platform: Option<String>) -> rusqlite::Result<Vec<Value>> {
        self.run(move |c| {
            let row = |r: &rusqlite::Row| -> rusqlite::Result<Value> {
                Ok(json!({"platform": r.get::<_, String>(0)?, "platform_user_id": r.get::<_, String>(1)?, "awm_user": r.get::<_, String>(2)?, "added_at": r.get::<_, String>(3)?}))
            };
            let cols = "SELECT platform, platform_user_id, awm_user, added_at FROM social_operators";
            match platform.map(|p| p.trim().to_string()).filter(|p| !p.is_empty()) {
                Some(p) => c.prepare(&format!("{cols} WHERE platform = ?1 ORDER BY added_at"))?.query_map([p], row)?.collect(),
                None => c.prepare(&format!("{cols} ORDER BY platform, added_at"))?.query_map([], row)?.collect(),
            }
        })
        .await
    }

    pub async fn lookup(&self, platform: &str, uid: &str) -> Option<String> {
        if platform.is_empty() || uid.is_empty() {
            return None;
        }
        let (p, u) = (platform.trim().to_string(), uid.trim().to_string());
        self.run(move |c| c.query_row("SELECT awm_user FROM social_operators WHERE platform = ?1 AND platform_user_id = ?2", params![p, u], |r| r.get(0)).optional().ok().flatten()).await
    }

    pub async fn upsert_account(&self, name: &str, platform: &str, kind: &str, display_name: &str, enabled: bool) {
        let (n, p, k, d, at) = (name.to_string(), platform.to_string(), kind.to_string(), display_name.to_string(), now_iso());
        let _ = self
            .run(move |c| {
                c.execute(
                    "INSERT INTO social_accounts (name, platform, kind, display_name, enabled, added_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6) \
                     ON CONFLICT(name) DO UPDATE SET platform = excluded.platform, kind = excluded.kind, display_name = excluded.display_name, enabled = excluded.enabled",
                    params![n, p, k, d, enabled as i64, at],
                )
            })
            .await;
    }

    // -- history --------------------------------------------------------------

    /// Stored messages oldest first: with `since`, the `limit` right after it, else the newest.
    pub async fn history(&self, account: &str, channel: Option<&str>, since: Option<&str>, limit: i64, include_deleted: bool) -> rusqlite::Result<Vec<Value>> {
        let (a, ch, s) = (account.to_string(), channel.map(str::to_string), since.map(str::to_string));
        self.run(move |c| {
            let mut wh = vec!["account = ?1".to_string()];
            let mut ps: Vec<String> = vec![a];
            if let Some(ch) = ch.filter(|c| !c.is_empty()) {
                ps.push(ch);
                wh.push(format!("channel_id = ?{}", ps.len()));
            }
            let since = s.filter(|s| !s.is_empty());
            if let Some(s) = &since {
                ps.push(s.clone());
                wh.push(format!("CAST(ts AS REAL) > CAST(?{} AS REAL)", ps.len()));
            }
            if !include_deleted {
                wh.push("deleted = 0".into());
            }
            let order = if since.is_some() { "ASC" } else { "DESC" };
            let sql = format!(
                "SELECT channel_id, message_id, thread_id, sender_id, sender_name, text, attachments, ts, own, edited, deleted FROM social_history WHERE {} ORDER BY CAST(ts AS REAL) {order} LIMIT {limit}",
                wh.join(" AND ")
            );
            let mut rows: Vec<Value> = c
                .prepare(&sql)?
                .query_map(rusqlite::params_from_iter(ps.iter()), |r| {
                    let att: String = r.get(6)?;
                    Ok(json!({
                        "channel_id": r.get::<_, String>(0)?, "message_id": r.get::<_, String>(1)?, "thread_id": r.get::<_, String>(2)?,
                        "sender_id": r.get::<_, String>(3)?, "sender_name": r.get::<_, String>(4)?, "text": r.get::<_, String>(5)?,
                        "attachments": serde_json::from_str::<Value>(if att.is_empty() { "[]" } else { &att }).unwrap_or(json!([])),
                        "ts": r.get::<_, String>(7)?, "own": r.get::<_, i64>(8)? != 0, "edited": r.get::<_, i64>(9)? != 0, "deleted": r.get::<_, i64>(10)? != 0,
                    }))
                })?
                .collect::<rusqlite::Result<_>>()?;
            if since.is_none() {
                rows.reverse();
            }
            Ok(rows)
        })
        .await
    }

    pub async fn synced_channels(&self, account: &str) -> rusqlite::Result<Vec<Value>> {
        let a = account.to_string();
        self.run(move |c| {
            c.prepare("SELECT channel_id, channel_name, synced_ts FROM social_sync WHERE account = ?1 ORDER BY channel_name")?
                .query_map([a], |r| Ok(json!({"channel_id": r.get::<_, String>(0)?, "channel_name": r.get::<_, String>(1)?, "synced_ts": r.get::<_, String>(2)?})))?
                .collect()
        })
        .await
    }

    // -- mounts ---------------------------------------------------------------

    pub async fn holder(&self, account: &str, channel: &str) -> Option<Mount> {
        let (a, ch) = (account.to_string(), channel.to_string());
        self.run(move |c| c.query_row(&format!("{MOUNT_COLS} WHERE account = ?1 AND channel_id = ?2"), params![a, ch], Mount::from_row).optional().ok().flatten()).await
    }

    pub async fn mounts(&self, account: Option<&str>) -> Vec<Mount> {
        let a = account.map(str::to_string);
        self.run(move |c| {
            let r: rusqlite::Result<Vec<Mount>> = match a {
                Some(a) => c.prepare(&format!("{MOUNT_COLS} WHERE account = ?1 ORDER BY channel_name")).and_then(|mut s| s.query_map([a], Mount::from_row)?.collect()),
                None => c.prepare(&format!("{MOUNT_COLS} ORDER BY account, channel_name")).and_then(|mut s| s.query_map([], Mount::from_row)?.collect()),
            };
            r.unwrap_or_default()
        })
        .await
    }

    pub async fn mount(&self, m: Mount) -> rusqlite::Result<Mount> {
        self.run(move |c| {
            let at = now_iso();
            c.execute(
                "INSERT INTO social_mounts (account, channel_id, channel_name, node, job, session_name, mounted_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7) \
                 ON CONFLICT (account, channel_id) DO UPDATE SET channel_name = excluded.channel_name, node = excluded.node, job = excluded.job, \
                 session_name = excluded.session_name, mounted_at = excluded.mounted_at",
                params![m.account, m.channel_id, m.channel_name, m.node, m.job, m.session_name, at],
            )?;
            Ok(Mount { mounted_at: at, ..m })
        })
        .await
    }

    pub async fn unmount(&self, account: &str, channel: &str) -> bool {
        let (a, ch) = (account.to_string(), channel.to_string());
        self.run(move |c| c.execute("DELETE FROM social_mounts WHERE account = ?1 AND channel_id = ?2", params![a, ch]).map(|n| n > 0).unwrap_or(false)).await
    }

    /// Free every seat `(node, job)` holds, and return what it held.
    pub async fn release_holder(&self, node: &str, job: &str) -> Vec<Mount> {
        let (n, j) = (node.to_string(), job.to_string());
        self.run(move |c| {
            let rows: Vec<Mount> = c.prepare(&format!("{MOUNT_COLS} WHERE node = ?1 AND job = ?2")).and_then(|mut s| s.query_map(params![n, j], Mount::from_row)?.collect()).unwrap_or_default();
            let _ = c.execute("DELETE FROM social_mounts WHERE node = ?1 AND job = ?2", params![n, j]);
            rows
        })
        .await
    }

    // -- quiet threads ----------------------------------------------------------

    pub async fn quiet_thread(&self, account: &str, channel: &str, thread: &str) {
        let (a, ch, t, at) = (account.to_string(), channel.to_string(), thread.to_string(), now_iso());
        let _ = self.run(move |c| c.execute("INSERT OR IGNORE INTO social_quiet_threads (account, channel_id, thread_id, quiet_at) VALUES (?1, ?2, ?3, ?4)", params![a, ch, t, at])).await;
    }

    pub async fn is_quiet(&self, account: &str, channel: &str, thread: &str) -> bool {
        if thread.is_empty() {
            return false;
        }
        let (a, ch, t) = (account.to_string(), channel.to_string(), thread.to_string());
        self.run(move |c| c.query_row("SELECT 1 FROM social_quiet_threads WHERE account = ?1 AND channel_id = ?2 AND thread_id = ?3", params![a, ch, t], |_| Ok(())).optional().ok().flatten().is_some()).await
    }
}

const MOUNT_COLS: &str = "SELECT account, channel_id, channel_name, node, job, session_name, mounted_at FROM social_mounts";

/// One `social_mounts` row: the session a chat's lines are pushed to.
#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize)]
pub struct Mount {
    pub account: String,
    pub channel_id: String,
    pub channel_name: String,
    pub node: String,
    pub job: String,
    pub session_name: String,
    pub mounted_at: String,
}

impl Mount {
    fn from_row(r: &rusqlite::Row) -> rusqlite::Result<Self> {
        Ok(Self { account: r.get(0)?, channel_id: r.get(1)?, channel_name: r.get(2)?, node: r.get(3)?, job: r.get(4)?, session_name: r.get(5)?, mounted_at: r.get(6)? })
    }

    /// The `holder` object `seats` and `mount` show.
    pub fn holder(&self) -> Value {
        json!({"node": self.node, "job": self.job, "session_name": self.session_name, "mounted_at": self.mounted_at})
    }
}

/// One account's synced history, as a bot connector writes it (`history.py`).
#[derive(Clone)]
pub struct History {
    pub db: Db,
    pub account: String,
}

impl History {
    pub fn new(db: Db, account: &str) -> Self {
        Self { db, account: account.into() }
    }

    /// Store one message. A redelivery rewrites it, and an edit marks it edited for good.
    pub async fn record(&self, m: &crate::connectors::Inbound, own: bool, edited: bool) {
        let a = self.account.clone();
        let att = serde_json::to_string(&m.attachments).unwrap_or_else(|_| "[]".into());
        let m = m.clone();
        let _ = self
            .db
            .run(move |c| {
                c.execute(
                    "INSERT INTO social_history (account, channel_id, message_id, thread_id, sender_id, sender_name, text, attachments, ts, own, edited) \
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11) ON CONFLICT (account, channel_id, message_id) DO UPDATE SET \
                     thread_id = excluded.thread_id, sender_id = excluded.sender_id, sender_name = excluded.sender_name, text = excluded.text, \
                     attachments = excluded.attachments, own = excluded.own, edited = MAX(social_history.edited, excluded.edited)",
                    params![a, m.channel_id, m.message_id, m.thread_id, m.sender_id, m.sender_name, m.text, att, m.ts, own as i64, edited as i64],
                )
            })
            .await;
    }

    pub async fn mark_deleted(&self, channel: &str, message_id: &str) {
        let (a, ch, id) = (self.account.clone(), channel.to_string(), message_id.to_string());
        let _ = self.db.run(move |c| c.execute("UPDATE social_history SET deleted = 1 WHERE account = ?1 AND channel_id = ?2 AND message_id = ?3", params![a, ch, id])).await;
    }

    /// Whether a person, not this account, wrote in the chat (users.md R126).
    pub async fn has_inbound(&self, channel: &str) -> bool {
        let (a, ch) = (self.account.clone(), channel.to_string());
        self.db
            .run(move |c| {
                c.query_row(
                    "SELECT 1 FROM social_history WHERE account = ?1 AND channel_id = ?2 AND own = 0 AND (sender_id LIKE 'U%' OR sender_id LIKE 'W%') LIMIT 1",
                    params![a, ch],
                    |_| Ok(()),
                )
                .optional()
                .ok()
                .flatten()
                .is_some()
            })
            .await
    }

    pub async fn synced_ts(&self, channel: &str) -> Option<String> {
        let (a, ch) = (self.account.clone(), channel.to_string());
        self.db.run(move |c| c.query_row("SELECT synced_ts FROM social_sync WHERE account = ?1 AND channel_id = ?2", params![a, ch], |r| r.get(0)).optional().ok().flatten()).await
    }

    /// Threads with a stored message newer than `since_ts`.
    pub async fn active_threads(&self, channel: &str, since_ts: &str) -> Vec<String> {
        let (a, ch, s) = (self.account.clone(), channel.to_string(), since_ts.to_string());
        self.db
            .run(move |c| {
                c.prepare("SELECT DISTINCT thread_id FROM social_history WHERE account = ?1 AND channel_id = ?2 AND thread_id != '' AND CAST(ts AS REAL) > CAST(?3 AS REAL)")
                    .and_then(|mut st| st.query_map(params![a, ch, s], |r| r.get(0))?.collect())
                    .unwrap_or_default()
            })
            .await
    }

    /// Advance a chat's cursor to `ts`, never backwards, keeping a known name.
    pub async fn set_synced(&self, channel: &str, ts: &str, channel_name: &str) {
        let (a, ch, t, n) = (self.account.clone(), channel.to_string(), ts.to_string(), channel_name.to_string());
        let _ = self
            .db
            .run(move |c| {
                c.execute(
                    "INSERT INTO social_sync (account, channel_id, channel_name, synced_ts) VALUES (?1, ?2, ?3, ?4) ON CONFLICT (account, channel_id) DO UPDATE SET \
                     channel_name = CASE WHEN excluded.channel_name != '' THEN excluded.channel_name ELSE social_sync.channel_name END, \
                     synced_ts = CASE WHEN CAST(excluded.synced_ts AS REAL) > CAST(social_sync.synced_ts AS REAL) OR social_sync.synced_ts = '' \
                     THEN excluded.synced_ts ELSE social_sync.synced_ts END",
                    params![a, ch, n, t],
                )
            })
            .await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_fresh_db_is_current_and_a_v8_db_gains_only_the_seat_tables() {
        let tmp = tempfile::tempdir().unwrap();
        let fresh = tmp.path().join("fresh.db");
        let c = open_conn(&fresh).unwrap();
        assert_eq!(version(&c).unwrap(), Some(SCHEMA_VERSION));

        let old = tmp.path().join("old.db");
        {
            let c = Connection::open(&old).unwrap();
            c.execute_batch(&format!("{SCHEMA}{HISTORY}{MOUNTS}{KEEPERS_V5}{KEEPER_STATE}{QUIET}")).unwrap();
            c.execute_batch("ALTER TABLE social_keepers ADD COLUMN seen_at TEXT NOT NULL DEFAULT '';CREATE TABLE schema_version (version INTEGER NOT NULL);INSERT INTO schema_version VALUES (8);").unwrap();
            c.execute("INSERT INTO social_mounts (account, channel_id, node, job) VALUES ('a', 'c', 'n', 'j')", []).unwrap();
            c.execute("INSERT INTO social_history (account, channel_id, message_id) VALUES ('a', 'c', 'm1')", []).unwrap();
        }
        let before: Vec<String> = Connection::open(&old).unwrap().prepare("SELECT name FROM sqlite_master WHERE type = 'table'").unwrap().query_map([], |r| r.get(0)).unwrap().map(Result::unwrap).collect();
        let c = open_conn(&old).unwrap();
        assert_eq!(version(&c).unwrap(), Some(SCHEMA_VERSION));
        let after: Vec<String> = c.prepare("SELECT name FROM sqlite_master WHERE type = 'table'").unwrap().query_map([], |r| r.get(0)).unwrap().map(Result::unwrap).collect();
        assert!(before.iter().all(|t| after.contains(t)), "no table dropped");
        assert_eq!(c.query_row("SELECT count(*) FROM social_mounts", [], |r| r.get::<_, i64>(0)).unwrap(), 1);
        assert_eq!(c.query_row("SELECT count(*) FROM seat_routed WHERE message_id = 'm1'", [], |r| r.get::<_, i64>(0)).unwrap(), 1, "seen messages are routed");
        drop(c);
        assert_eq!(version(&open_conn(&old).unwrap()).unwrap(), Some(SCHEMA_VERSION), "reopening is a no-op");
    }

    #[test]
    fn a_v9_seat_gains_the_switch_off_and_no_link() {
        let tmp = tempfile::tempdir().unwrap();
        let p = tmp.path().join("v9.db");
        {
            let c = Connection::open(&p).unwrap();
            c.execute_batch(&format!("{SCHEMA}{HISTORY}{MOUNTS}{KEEPERS_V5}{KEEPER_STATE}{QUIET}{SEATS}")).unwrap();
            c.execute_batch("ALTER TABLE social_keepers ADD COLUMN seen_at TEXT NOT NULL DEFAULT '';CREATE TABLE schema_version (version INTEGER NOT NULL);INSERT INTO schema_version VALUES (9);").unwrap();
            c.execute("INSERT INTO seat_registry (account, channel_id, state, job) VALUES ('a', 'c', 'occupied', 'j')", []).unwrap();
        }
        let c = open_conn(&p).unwrap();
        assert_eq!(version(&c).unwrap(), Some(SCHEMA_VERSION));
        let (rc, url, state): (i64, String, String) = c.query_row("SELECT remote_control, rc_url, state FROM seat_registry", [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).unwrap();
        assert_eq!((rc, url.as_str(), state.as_str()), (0, "", "occupied"));
    }

}
