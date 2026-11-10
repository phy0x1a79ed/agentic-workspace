//! The node side of operations: forward to alcor, keep an outbox while it is away
//! (fleet.md R106 item 6).
//!
//! A write that cannot reach alcor goes into a local outbox under a sequence number, and the
//! session gets its answer at once. Once anything waits in the outbox, later writes queue
//! behind it, so alcor sees one node's writes in the order the node made them. Each write
//! carries `op_id = <node>:<seq>`, and the store applies an op_id once, so a replay after a
//! lost reply does nothing twice. The relay also takes the orders alcor signed for this node.

use std::path::Path;
use std::sync::{Arc, Mutex};

use async_trait_free::BoxFut;
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

use crate::ids::{mint_task_id, token};

pub const WRITES: [&str; 13] = [
    "task_autotitle", "task_open", "task_attach", "task_join", "task_report", "task_session_end", "task_claim", "task_detail", "task_message",
    "approval_request", "approval_cancel", "order_done", "ops_post",
];
pub const READS: [&str; 7] = ["task_for_session", "task_sessions", "task_get", "task_list", "task_messages", "approval_get", "keys"];
/// The writes that put a session on a card, so the relay can answer task_for_session offline.
const SESSION_WRITES: [&str; 4] = ["task_open", "task_attach", "task_join", "task_claim"];
/// Long polls go straight to alcor: a stale copy would answer them wrongly.
pub const POLLS: [&str; 2] = ["approval_wait", "order_pull"];

#[derive(Debug, Clone, thiserror::Error)]
pub enum ClientError {
    /// alcor did not answer. The write is safe to replay.
    #[error("{0}")]
    Unreachable(String),
    /// alcor answered with an error. Replaying the same write would fail the same way.
    #[error("{0}")]
    Refused(String),
}

/// The way to alcor's home verbs: in process on alcor, through the door elsewhere.
pub trait StoreClient: Send + Sync {
    fn call<'a>(&'a self, verb: &'a str, args: Value) -> BoxFut<'a, Result<Value, ClientError>>;
}

/// A boxed future, so the client trait stays object safe without a macro crate.
pub mod async_trait_free {
    pub type BoxFut<'a, T> = std::pin::Pin<Box<dyn std::future::Future<Output = T> + Send + 'a>>;
}

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, verb TEXT NOT NULL,
    args TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dead (seq INTEGER PRIMARY KEY, verb TEXT, args TEXT, error TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
";

pub struct Relay {
    pub node: String,
    db: Mutex<Connection>,
    store: Arc<dyn StoreClient>,
    clock: crate::store::Clock,
    flush_lock: tokio::sync::Mutex<()>,
}

/// Python's `json.dumps(args, sort_keys=True)`, the read cache's key.
fn sorted_key(v: &Value) -> String {
    String::from_utf8(awm_trust::grant::canonical(v)).unwrap_or_default()
}

impl Relay {
    pub fn open(node: &str, path: &Path, store: Arc<dyn StoreClient>) -> anyhow::Result<Relay> {
        Relay::open_with_clock(node, path, store, Arc::new(crate::now))
    }

    pub fn open_with_clock(node: &str, path: &Path, store: Arc<dyn StoreClient>, clock: crate::store::Clock) -> anyhow::Result<Relay> {
        if let Some(d) = path.parent() {
            std::fs::create_dir_all(d)?;
        }
        let db = Connection::open(path)?;
        db.pragma_update(None, "journal_mode", "WAL")?;
        db.pragma_update(None, "busy_timeout", 5000)?;
        db.execute_batch(SCHEMA)?;
        // MIGRATION(session-ids): marks keyed by a job short id are no longer read.
        db.execute("DELETE FROM meta WHERE substr(k, 1, 4) = 'job:' OR (substr(k, 1, 3) = 'rc:' AND k NOT GLOB 'rc:*:cc????????????????????????????????' AND k NOT GLOB 'rc:*:ds*')", [])?;
        Ok(Relay { node: node.into(), db: Mutex::new(db), store, clock, flush_lock: tokio::sync::Mutex::new(()) })
    }

    fn conn(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.db.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn now(&self) -> f64 {
        (self.clock)()
    }

    fn meta_get(&self, k: &str) -> Option<String> {
        self.conn().query_row("SELECT v FROM meta WHERE k=?", [k], |r| r.get(0)).optional().ok().flatten()
    }

    fn meta_set(&self, k: &str, v: &str) {
        let _ = self.conn().execute("INSERT OR REPLACE INTO meta VALUES (?,?)", params![k, v]);
    }

    fn down(&self) {
        if self.meta_get("down_since").filter(|s| !s.is_empty()).is_none() {
            self.meta_set("down_since", &self.now().to_string());
        }
    }

    fn up(&self) {
        self.meta_set("down_since", "");
    }

    pub fn down_since(&self) -> Option<f64> {
        self.meta_get("down_since").and_then(|s| s.parse().ok())
    }

    pub fn pending(&self) -> i64 {
        self.conn().query_row("SELECT COUNT(*) FROM outbox", [], |r| r.get(0)).unwrap_or(0)
    }

    fn enqueue(&self, verb: &str, mut args: Value) -> anyhow::Result<(i64, Value)> {
        let db = self.conn();
        db.execute("INSERT INTO outbox(verb, args, ts) VALUES (?,?,?)", params![verb, "{}", self.now()])?;
        let seq = db.last_insert_rowid();
        args["op_id"] = json!(format!("{}:{seq}", self.node));
        db.execute("UPDATE outbox SET args=? WHERE seq=?", params![args.to_string(), seq])?;
        Ok((seq, args))
    }

    /// Send a write, or queue it behind the outbox. Answers alcor's reply or a provisional one.
    pub async fn write(&self, verb: &str, args: Value) -> Result<Value, ClientError> {
        if !WRITES.contains(&verb) {
            return Err(ClientError::Refused(format!("{verb} is not a write")));
        }
        let mut args = if args.is_object() { args } else { json!({}) };
        let set_default = |a: &mut Value, k: &str, v: Value| {
            if a.get(k).is_none_or(Value::is_null) {
                a[k] = v;
            }
        };
        if verb == "task_open" {
            set_default(&mut args, "task_id", json!(mint_task_id()));
            set_default(&mut args, "node", json!(self.node));
        }
        if ["task_attach", "task_join", "task_claim", "task_message"].contains(&verb) {
            set_default(&mut args, "node", json!(self.node));
        }
        if verb == "approval_request" {
            set_default(&mut args, "request_id", json!(format!("q{}", token(9))));
        }
        let (seq, args) = self.enqueue(verb, args).map_err(|e| ClientError::Unreachable(e.to_string()))?;
        self.flush().await;
        let (queued, done, dead) = {
            let db = self.conn();
            let queued = db.query_row("SELECT 1 FROM outbox WHERE seq=?", [seq], |_| Ok(())).optional().ok().flatten().is_some();
            let done: Option<String> = db.query_row("SELECT value FROM cache WHERE key=?", [format!("op:{seq}")], |r| r.get(0)).optional().ok().flatten();
            let dead: Option<String> = db.query_row("SELECT error FROM dead WHERE seq=?", [seq], |r| r.get(0)).optional().ok().flatten();
            (queued, done, dead)
        };
        if let Some(e) = dead {
            return Err(ClientError::Refused(e));
        }
        if let Some(d) = done {
            return Ok(serde_json::from_str(&d).unwrap_or(Value::Null));
        }
        debug_assert!(queued);
        Ok(self.provisional(verb, &args))
    }

    fn provisional(&self, verb: &str, args: &Value) -> Value {
        let mut out = json!({"queued": true, "outbox": self.pending(), "alcor_down_since": self.down_since()});
        for k in ["task_id", "request_id"] {
            if let Some(v) = args.get(k) {
                out[k] = v.clone();
            }
        }
        if verb == "task_join" {
            out["main_session_id"] = Value::Null;
        }
        if SESSION_WRITES.contains(&verb) {
            if let (Some(s), Some(t)) = (args.get("session_id").and_then(Value::as_str), args.get("task_id").and_then(Value::as_str)) {
                self.remember_session(s, t);
            }
        }
        out
    }

    fn remember_session(&self, session_id: &str, task_id: &str) {
        let _ = self.conn().execute(
            "INSERT OR REPLACE INTO cache VALUES (?,?,?)",
            params![format!("session:{session_id}"), json!(task_id).to_string(), self.now()],
        );
    }

    /// Send the outbox in order. Stops at the first unreachable write. Answers how many went.
    pub async fn flush(&self) -> usize {
        let _guard = self.flush_lock.lock().await;
        let mut sent = 0;
        loop {
            let row: Option<(i64, String, String)> =
                self.conn().query_row("SELECT seq, verb, args FROM outbox ORDER BY seq LIMIT 1", [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).optional().ok().flatten();
            let Some((seq, verb, raw)) = row else { return sent };
            let args: Value = serde_json::from_str(&raw).unwrap_or(json!({}));
            match self.store.call(&verb, args.clone()).await {
                Err(ClientError::Unreachable(_)) => {
                    self.down();
                    return sent;
                }
                Err(ClientError::Refused(e)) => {
                    let op = args.get("op_id").and_then(|v| v.as_str()).unwrap_or("").to_string();
                    tracing::warn!("alcor refused {verb} {op}: {e}");
                    let db = self.conn();
                    let _ = db.execute("INSERT INTO dead VALUES (?,?,?,?,?)", params![seq, verb, raw, e, self.now()]);
                    let _ = db.execute("DELETE FROM outbox WHERE seq=?", [seq]);
                }
                Ok(result) => {
                    self.up();
                    {
                        let db = self.conn();
                        let _ = db.execute("DELETE FROM outbox WHERE seq=?", [seq]);
                        let _ = db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", params![format!("op:{seq}"), result.to_string(), self.now()]);
                    }
                    if SESSION_WRITES.contains(&verb.as_str()) {
                        if let (Some(t), Some(s)) = (result.get("task_id").and_then(Value::as_str), args.get("session_id").and_then(Value::as_str)) {
                            self.remember_session(s, t);
                        }
                    }
                    sent += 1;
                }
            }
        }
    }

    /// Ask alcor. While it is away, answer from the last copy and say how old it is.
    pub async fn read(&self, verb: &str, args: Value) -> Result<Value, ClientError> {
        if !READS.contains(&verb) {
            return Err(ClientError::Refused(format!("{verb} is not a read")));
        }
        let key = format!("read:{verb}:{}", sorted_key(&args));
        if self.pending() == 0 {
            match self.store.call(verb, args.clone()).await {
                Err(ClientError::Unreachable(_)) => self.down(),
                Err(e) => return Err(e),
                Ok(result) => {
                    self.up();
                    let _ = self.conn().execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", params![key, result.to_string(), self.now()]);
                    if verb == "task_for_session" {
                        if let (Some(t), Some(s)) = (result.as_str(), args.get("session_id").and_then(Value::as_str)) {
                            self.remember_session(s, t);
                        }
                    }
                    return Ok(result);
                }
            }
        }
        if verb == "task_for_session" {
            let s = args.get("session_id").and_then(Value::as_str).unwrap_or_default();
            let v: Option<String> = self.conn().query_row("SELECT value FROM cache WHERE key=?", [format!("session:{s}")], |r| r.get(0)).optional().ok().flatten();
            return Ok(v.and_then(|v| serde_json::from_str(&v).ok()).unwrap_or(Value::Null));
        }
        let row: Option<(String, f64)> = self.conn().query_row("SELECT value, ts FROM cache WHERE key=?", [&key], |r| Ok((r.get(0)?, r.get(1)?))).optional().ok().flatten();
        Ok(json!({"stale": true, "alcor_down_since": self.down_since(), "outbox": self.pending(),
                  "as_of": row.as_ref().map(|r| r.1),
                  "result": row.and_then(|r| serde_json::from_str::<Value>(&r.0).ok())}))
    }

    /// A long poll. Unreachable while alcor is away or the outbox holds writes.
    pub async fn poll(&self, verb: &str, args: Value) -> Result<Value, ClientError> {
        if !POLLS.contains(&verb) {
            return Err(ClientError::Refused(format!("{verb} is not a poll")));
        }
        if self.pending() > 0 {
            return Err(ClientError::Unreachable("the outbox still holds writes for alcor".into()));
        }
        match self.store.call(verb, args).await {
            Err(ClientError::Unreachable(e)) => {
                self.down();
                Err(ClientError::Unreachable(e))
            }
            other => {
                if other.is_ok() {
                    self.up();
                }
                other
            }
        }
    }

    /// A small local note, such as the card a session last sent to Needs you.
    pub fn mark(&self, key: &str) -> Option<String> {
        self.meta_get(key)
    }

    pub fn set_mark(&self, key: &str, value: &str) {
        self.meta_set(key, value);
    }

    pub fn clear_mark(&self, key: &str) {
        let _ = self.conn().execute("DELETE FROM meta WHERE k=?", [key]);
    }

    /// Every mark whose key starts with `prefix`.
    pub fn marks(&self, prefix: &str) -> Vec<(String, String)> {
        let db = self.conn();
        let Ok(mut st) = db.prepare("SELECT k, v FROM meta WHERE substr(k, 1, ?) = ?") else { return vec![] };
        st.query_map(params![prefix.chars().count() as i64, prefix], |r| Ok((r.get(0)?, r.get::<_, Option<String>>(1)?.unwrap_or_default())))
            .map(|rows| rows.filter_map(Result::ok).collect())
            .unwrap_or_default()
    }

    pub fn status(&self) -> Value {
        let dead: i64 = self.conn().query_row("SELECT COUNT(*) FROM dead", [], |r| r.get(0)).unwrap_or(0);
        json!({"node": self.node, "outbox": self.pending(), "refused": dead, "alcor_down_since": self.down_since()})
    }
}
