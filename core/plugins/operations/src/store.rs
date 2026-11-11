//! The task store on alcor: every card, its sessions, its messages and the event feed
//! (fleet.md R106, queue.md R61 to R66).
//!
//! Only the store writes a card's section and status (R66). Sessions move their card with
//! `task_report`; Tony moves one only through an action the vault API verified, which calls
//! the store in process. Every change appends to `events`, and each appended event is
//! announced to the `on_event` hook once its transaction commits: `vault_push` carries
//! `board_changes` to the vault from that announcement.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use rusqlite::{params, Connection, OptionalExtension, Row, Transaction, TransactionBehavior};
use serde_json::{json, Map, Value};

use crate::ids::{is_task_id, mint_task_id};

pub const NEEDS_YOU: &str = "needs_you";
pub const WORKING: &str = "working";
pub const STALE: &str = "stale";
pub const HISTORY: &str = "history";
pub const SECTIONS: [&str; 4] = [NEEDS_YOU, WORKING, STALE, HISTORY];
/// A section older rows may still hold; no card moves into it.
const PROPOSED: &str = "proposed";
pub const ROLES: [&str; 3] = ["main", "worker", "joined"];
pub const APPROVAL_OUTCOMES: [&str; 4] = ["approved", "denied", "expired", "cancelled"];
pub const PAGE: i64 = 50;
/// The meta key that marks a Needs-you card its session's block put there.
const BLOCKED_BY_STATE: &str = "blocked_by_state";
pub const PLAN_CHARS: usize = 20_000;
pub const TODO_ITEMS: usize = 200;
pub const MESSAGE_CHARS: usize = 20_000;
pub const CHANGES_MAX: i64 = 500;

/// `task_report` state -> (section, status). Working carries no status tag (R61 item 3).
pub fn report_state(state: &str) -> Option<(&'static str, Option<&'static str>)> {
    Some(match state {
        "working" => (WORKING, None),
        "planned" => (NEEDS_YOU, Some("planned")),
        "blocked" => (NEEDS_YOU, Some("blocked")),
        "review" => (NEEDS_YOU, Some("review")),
        "done" => (HISTORY, Some("done")),
        _ => return None,
    })
}
pub const REPORT_STATES: [&str; 5] = ["working", "planned", "blocked", "review", "done"];

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'task',
    section TEXT NOT NULL,
    status TEXT,
    peer TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    body_ref TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    parent_task TEXT,
    folded_into TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    done_at REAL
);
CREATE INDEX IF NOT EXISTS tasks_section ON tasks(section, updated);
CREATE INDEX IF NOT EXISTS tasks_done ON tasks(done_at);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    role TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    node TEXT,
    name TEXT,
    rc_url TEXT,
    started REAL NOT NULL,
    ended REAL
);
CREATE INDEX IF NOT EXISTS sessions_task ON sessions(task_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS applied_ops (
    op_id TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts REAL NOT NULL,
    author TEXT,
    node TEXT,
    session_id TEXT,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_task ON messages(task_id, seq);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
";

/// What the port adds. Each step is additive and runs once; `downgrade` undoes the rename.
const ADDITIVE: &str = "
CREATE TABLE IF NOT EXISTS ops_notes (
    kind TEXT NOT NULL,
    node TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (kind, node)
);
";

#[derive(Debug, thiserror::Error)]
pub enum QueueError {
    #[error("{0}")]
    Refused(String),
    #[error("sqlite: {0}")]
    Db(#[from] rusqlite::Error),
}

pub type Result<T> = std::result::Result<T, QueueError>;

fn refuse<T>(msg: impl Into<String>) -> Result<T> {
    Err(QueueError::Refused(msg.into()))
}

/// One appended event.
#[derive(Debug, Clone, PartialEq)]
pub struct BoardEvent {
    pub task_id: String,
    pub seq: i64,
    pub ts: f64,
}

pub type EventHook = Arc<dyn Fn(&BoardEvent) + Send + Sync>;
pub type NoticeHook = Arc<dyn Fn() + Send + Sync>;
/// A task's approvals as `board_changes` carries them, read from the approver's own store.
pub type ApprovalsOf = Arc<dyn Fn(&str) -> Vec<Value> + Send + Sync>;
pub type Clock = Arc<dyn Fn() -> f64 + Send + Sync>;

/// A live session as telemetry lists it, matched on `session_id`.
#[derive(Debug, Clone, Default)]
pub struct LiveSession {
    pub session_id: Option<String>,
    pub name: Option<String>,
    pub link: Option<String>,
    pub project: Option<String>,
    pub scope: Option<String>,
    /// `working`, `blocked` or `done`, from the session's own record.
    pub state: Option<String>,
    pub needs: Option<String>,
}

/// A new card's fields beyond its title.
#[derive(Debug, Clone, Default)]
pub struct Open {
    pub session_id: Option<String>,
    pub task_id: Option<String>,
    pub kind: Option<String>,
    pub peer: Option<String>,
    pub node: Option<String>,
    pub section: Option<String>,
    pub status: Option<String>,
    pub tags: Option<Vec<String>>,
    pub meta: Option<Map<String, Value>>,
    pub name: Option<String>,
    pub source_note: Option<String>,
}

/// A session joining a card.
#[derive(Debug, Clone, Default)]
pub struct Seat {
    pub node: Option<String>,
    pub name: Option<String>,
}

pub struct Store {
    path: PathBuf,
    db: Mutex<Connection>,
    clock: Clock,
    on_event: Mutex<Option<EventHook>>,
    on_notice: Mutex<Option<NoticeHook>>,
    approvals: Mutex<Option<ApprovalsOf>>,
}

struct Tx<'a> {
    tx: Transaction<'a>,
    now: f64,
    events: Vec<BoardEvent>,
}

impl Tx<'_> {
    fn event(&mut self, task_id: &str, kind: &str, data: Value) -> Result<()> {
        self.tx.execute(
            "INSERT INTO events(task_id, ts, kind, data) VALUES (?,?,?,?)",
            params![task_id, self.now, kind, data.to_string()],
        )?;
        self.events.push(BoardEvent { task_id: task_id.into(), seq: self.tx.last_insert_rowid(), ts: self.now });
        Ok(())
    }
}

fn json_col(s: Option<String>, empty: Value) -> Value {
    s.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or(empty)
}

fn has_column(db: &Connection, table: &str, column: &str) -> rusqlite::Result<bool> {
    let mut st = db.prepare(&format!("PRAGMA table_info({table})"))?;
    let names: Vec<String> = st.query_map([], |r| r.get::<_, String>(1))?.collect::<rusqlite::Result<_>>()?;
    Ok(names.iter().any(|n| n == column))
}

/// The task row, as the store reads it.
#[derive(Debug, Clone)]
struct TaskRow {
    task_id: String,
    title: String,
    kind: String,
    section: String,
    status: Option<String>,
    peer: Option<String>,
    tags: Value,
    body_ref: Option<String>,
    body: Option<String>,
    source_note: Option<String>,
    meta: Map<String, Value>,
    parent_task: Option<String>,
    folded_into: Option<String>,
    created: f64,
    updated: f64,
    done_at: Option<f64>,
}

const TASK_COLS: &str = "task_id, title, kind, section, status, peer, tags, body_ref, body, source_note, meta, parent_task, folded_into, created, updated, done_at";

fn task_row(r: &Row) -> rusqlite::Result<TaskRow> {
    let meta = match json_col(r.get(10)?, json!({})) {
        Value::Object(m) => m,
        _ => Map::new(),
    };
    Ok(TaskRow {
        task_id: r.get(0)?,
        title: r.get(1)?,
        kind: r.get(2)?,
        section: r.get(3)?,
        status: r.get(4)?,
        peer: r.get(5)?,
        tags: json_col(r.get(6)?, json!([])),
        body_ref: r.get(7)?,
        body: r.get(8)?,
        source_note: r.get(9)?,
        meta,
        parent_task: r.get(11)?,
        folded_into: r.get(12)?,
        created: r.get(13)?,
        updated: r.get(14)?,
        done_at: r.get(15)?,
    })
}

fn task_by_id(db: &Connection, task_id: &str) -> Result<TaskRow> {
    let mut id = task_id.to_string();
    for _ in 0..64 {
        let row = db.query_row(&format!("SELECT {TASK_COLS} FROM tasks WHERE task_id=?"), [&id], task_row).optional()?;
        let Some(row) = row else { return refuse(format!("no task {task_id:?}")) };
        match row.folded_into.clone() {
            Some(into) if !into.is_empty() => id = into,
            _ => return Ok(row),
        }
    }
    refuse(format!("task {task_id:?} folds into a loop"))
}

fn raw_task(db: &Connection, task_id: &str) -> Result<Option<TaskRow>> {
    Ok(db.query_row(&format!("SELECT {TASK_COLS} FROM tasks WHERE task_id=?"), [task_id], task_row).optional()?)
}

fn sessions_of(db: &Connection, task_id: &str) -> Result<Vec<Value>> {
    let mut st = db.prepare(
        "SELECT session_id, name, role, active, node, rc_url, started, ended, project, scope FROM sessions WHERE task_id=? ORDER BY started",
    )?;
    let rows = st.query_map([task_id], |r| {
        Ok(json!({
            "session_id": r.get::<_, String>(0)?,
            "name": r.get::<_, Option<String>>(1)?, "role": r.get::<_, String>(2)?,
            "active": r.get::<_, i64>(3)? != 0, "node": r.get::<_, Option<String>>(4)?,
            "rc_url": r.get::<_, Option<String>>(5)?, "started": r.get::<_, f64>(6)?,
            "ended": r.get::<_, Option<f64>>(7)?, "project": r.get::<_, Option<String>>(8)?,
            "scope": r.get::<_, Option<String>>(9)?,
        }))
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

fn messages_of(db: &Connection, task_id: &str, after: i64) -> Result<Vec<Value>> {
    let mut st = db.prepare("SELECT seq, ts, author, node, session_id, body FROM messages WHERE task_id=? AND seq>? ORDER BY seq")?;
    let rows = st.query_map(params![task_id, after], |r| {
        Ok(json!({
            "seq": r.get::<_, i64>(0)?, "ts": r.get::<_, f64>(1)?, "author": r.get::<_, Option<String>>(2)?,
            "node": r.get::<_, Option<String>>(3)?, "session_id": r.get::<_, Option<String>>(4)?,
            "body": r.get::<_, String>(5)?,
        }))
    })?;
    Ok(rows.collect::<rusqlite::Result<_>>()?)
}

fn live_sessions(db: &Connection, task_id: &str) -> Result<i64> {
    Ok(db.query_row("SELECT COUNT(*) FROM sessions WHERE task_id=? AND active=1", [task_id], |r| r.get(0))?)
}

impl Store {
    /// Open the store at `path`. A missing file is refused unless `create` is set: alcor's
    /// queue is the source of truth, and a fresh empty one would hide every card.
    pub fn open(path: impl AsRef<Path>, create: bool) -> anyhow::Result<Store> {
        Store::open_with_clock(path, create, Arc::new(crate::now))
    }

    pub fn open_with_clock(path: impl AsRef<Path>, create: bool, clock: Clock) -> anyhow::Result<Store> {
        let path = path.as_ref().to_path_buf();
        if !path.exists() {
            if !create {
                anyhow::bail!("{} is missing: refusing to start with an empty queue (set AWM_OPERATIONS_INIT=1 to create one)", path.display());
            }
            if let Some(dir) = path.parent() {
                std::fs::create_dir_all(dir)?;
            }
        }
        let db = Connection::open(&path)?;
        db.pragma_update(None, "journal_mode", "WAL")?;
        db.pragma_update(None, "foreign_keys", "ON")?;
        db.pragma_update(None, "busy_timeout", 5000)?;
        db.execute_batch(SCHEMA)?;
        migrate(&db)?;
        Ok(Store { path, db: Mutex::new(db), clock, on_event: Mutex::new(None), on_notice: Mutex::new(None), approvals: Mutex::new(None) })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn set_on_event(&self, hook: EventHook) {
        *self.on_event.lock().expect("hook lock") = Some(hook);
    }

    pub fn set_on_notice(&self, hook: NoticeHook) {
        *self.on_notice.lock().expect("hook lock") = Some(hook);
    }

    pub fn set_approvals(&self, f: ApprovalsOf) {
        *self.approvals.lock().expect("approvals lock") = Some(f);
    }

    fn now(&self) -> f64 {
        (self.clock)()
    }

    fn conn(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.db.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Run `f` in one IMMEDIATE transaction, replaying a relay write's first answer for a
    /// seen `op_id` (fleet.md R106 item 6), and announce its events after the commit.
    fn write<R>(&self, op_id: Option<&str>, f: impl FnOnce(&mut Tx) -> Result<R>) -> Result<Value>
    where
        R: Into<Value>,
    {
        let mut db = self.conn();
        if let Some(op) = op_id.filter(|s| !s.is_empty()) {
            let seen: Option<String> = db.query_row("SELECT result FROM applied_ops WHERE op_id=?", [op], |r| r.get(0)).optional()?;
            if let Some(s) = seen {
                return Ok(serde_json::from_str(&s).unwrap_or(Value::Null));
            }
        }
        let now = self.now();
        let (out, events) = {
            let mut tx = Tx { tx: db.transaction_with_behavior(TransactionBehavior::Immediate)?, now, events: vec![] };
            let out: Value = f(&mut tx)?.into();
            if let Some(op) = op_id.filter(|s| !s.is_empty()) {
                tx.tx.execute("INSERT INTO applied_ops VALUES (?,?,?)", params![op, out.to_string(), now])?;
            }
            let Tx { tx, events, .. } = tx;
            tx.commit()?;
            (out, events)
        };
        drop(db);
        self.announce(&events);
        Ok(out)
    }

    fn announce(&self, events: &[BoardEvent]) {
        let hook = self.on_event.lock().expect("hook lock").clone();
        if let Some(h) = hook {
            for e in events {
                h(e);
            }
        }
    }

    fn set_card(tx: &mut Tx, task_id: &str, section: &str, status: Option<&str>, why: &str, data: Value) -> Result<()> {
        let done_at = (section == HISTORY).then_some(tx.now);
        tx.tx.execute(
            "UPDATE tasks SET section=?, status=?, updated=?, done_at=? WHERE task_id=?",
            params![section, status, tx.now, done_at, task_id],
        )?;
        let mut d = json!({"section": section, "status": status});
        if let (Value::Object(m), Value::Object(extra)) = (&mut d, data) {
            m.extend(extra);
        }
        tx.event(task_id, why, d)
    }

    fn add_session(tx: &mut Tx, task_id: &str, session_id: &str, role: &str, seat: &Seat) -> Result<()> {
        if !ROLES.contains(&role) {
            return refuse(format!("role must be one of {ROLES:?}, not {role:?}"));
        }
        tx.tx.execute(
            "INSERT INTO sessions(session_id, task_id, role, node, name, started) VALUES (?,?,?,?,?,?) \
             ON CONFLICT(session_id) DO UPDATE SET task_id=excluded.task_id, role=excluded.role, active=1, ended=NULL, \
             node=COALESCE(excluded.node, node), name=COALESCE(excluded.name, name)",
            params![session_id, task_id, role, seat.node, seat.name, tx.now],
        )?;
        tx.event(task_id, "session", json!({"session_id": session_id, "role": role}))
    }

    // -- session verbs (sessions.md R57) ----------------------------------------------

    /// A new card. A relay may pass the id it minted while alcor was down.
    pub fn task_open(&self, title: &str, o: Open, op_id: Option<&str>) -> Result<Value> {
        let title = title.trim().to_string();
        if title.is_empty() {
            return refuse("a task needs a title");
        }
        let section = o.section.clone().unwrap_or_else(|| WORKING.into());
        if !SECTIONS.contains(&section.as_str()) {
            return refuse(format!("section must be one of {SECTIONS:?}"));
        }
        self.write(op_id, |tx| {
            let tid = o.task_id.clone().unwrap_or_else(mint_task_id);
            if !is_task_id(&tid) {
                return refuse(format!("malformed task id {tid:?}"));
            }
            if raw_task(&tx.tx, &tid)?.is_some() {
                return refuse(format!("task id {tid:?} is taken"));
            }
            let peer = o.peer.clone().or_else(|| o.node.clone());
            tx.tx.execute(
                "INSERT INTO tasks(task_id, title, kind, section, status, peer, tags, meta, source_note, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                params![
                    tid, title, o.kind.clone().unwrap_or_else(|| "task".into()), section, o.status, peer,
                    json!(o.tags.clone().unwrap_or_default()).to_string(),
                    Value::Object(o.meta.clone().unwrap_or_default()).to_string(), o.source_note, tx.now, tx.now
                ],
            )?;
            tx.event(&tid, "open", json!({"title": title, "section": section, "status": o.status}))?;
            if let Some(sid) = o.session_id.as_deref().filter(|s| !s.is_empty()) {
                let seat = Seat { node: o.node.clone(), name: o.name.clone() };
                Store::add_session(tx, &tid, sid, "main", &seat)?;
            }
            Ok(json!({"task_id": tid}))
        })
    }

    /// Put a session on an existing card (R57 item 3).
    pub fn task_attach(&self, task_id: &str, session_id: &str, role: &str, seat: Seat, op_id: Option<&str>) -> Result<Value> {
        self.write(op_id, |tx| {
            let tid = task_by_id(&tx.tx, task_id)?.task_id;
            Store::add_session(tx, &tid, session_id, role, &seat)?;
            Ok(json!({"task_id": tid}))
        })
    }

    /// Join a running task and fold the joiner's own card into it (R57 item 5). The caller
    /// tells `main_session_id`: the store notifies nobody.
    pub fn task_join(&self, task_id: &str, session_id: &str, seat: Seat, op_id: Option<&str>) -> Result<Value> {
        self.write(op_id, |tx| {
            let tid = task_by_id(&tx.tx, task_id)?.task_id;
            let own: Option<String> = tx.tx.query_row("SELECT task_id FROM sessions WHERE session_id=?", [session_id], |r| r.get(0)).optional()?;
            let mut folded = None;
            if let Some(own) = own.filter(|o| *o != tid) {
                let others: i64 = tx.tx.query_row(
                    "SELECT COUNT(*) FROM sessions WHERE task_id=? AND session_id<>?",
                    params![own, session_id],
                    |r| r.get(0),
                )?;
                if others == 0 {
                    tx.tx.execute("UPDATE tasks SET folded_into=?, updated=? WHERE task_id=?", params![tid, tx.now, own])?;
                    tx.event(&own, "fold", json!({"into": tid}))?;
                    folded = Some(own);
                }
            }
            let joined = Seat { node: seat.node.clone(), ..Seat::default() };
            Store::add_session(tx, &tid, session_id, "joined", &joined)?;
            let main: Option<(String, Option<String>, Option<String>)> = tx
                .tx
                .query_row(
                    "SELECT session_id, node, name FROM sessions WHERE task_id=? AND role='main' ORDER BY started LIMIT 1",
                    [&tid],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .optional()?;
            let (ms, mn, mname) = main.map(|(a, b, c)| (Some(a), b, c)).unwrap_or_default();
            Ok(json!({"task_id": tid, "main_session_id": ms, "main_node": mn, "main_name": mname, "folded_task_id": folded}))
        })
    }

    pub fn task_for_session(&self, session_id: &str) -> Result<Option<String>> {
        let db = self.conn();
        let row: Option<String> = db.query_row("SELECT task_id FROM sessions WHERE session_id=?", [session_id], |r| r.get(0)).optional()?;
        match row {
            None => Ok(None),
            Some(t) => Ok(Some(task_by_id(&db, &t)?.task_id)),
        }
    }

    pub fn task_sessions(&self, task_id: &str) -> Result<Vec<Value>> {
        let db = self.conn();
        let tid = task_by_id(&db, task_id)?.task_id;
        sessions_of(&db, &tid)
    }

    /// A session moves its own card: working, planned, blocked, review or done.
    pub fn task_report(&self, task_id: &str, session_id: Option<&str>, state: &str, reason: Option<&str>, op_id: Option<&str>) -> Result<Value> {
        let Some((section, status)) = report_state(state) else {
            return refuse(format!("state must be one of {REPORT_STATES:?}, not {state:?}"));
        };
        self.write(op_id, |tx| {
            let row = task_by_id(&tx.tx, task_id)?;
            if row.kind == "approval" {
                return refuse("an approval card moves only through the approver");
            }
            let tid = row.task_id.clone();
            let mut meta = row.meta.clone();
            meta.insert("reason".into(), if section == NEEDS_YOU { json!(reason) } else { Value::Null });
            meta.remove(BLOCKED_BY_STATE);
            tx.tx.execute("UPDATE tasks SET meta=? WHERE task_id=?", params![Value::Object(meta).to_string(), tid])?;
            Store::set_card(tx, &tid, section, status, "report", json!({"session_id": session_id, "reason": reason}))?;
            Ok(json!({"task_id": tid, "section": section, "status": status}))
        })
    }

    /// Title a card still named by `agent_card`, so a session's name or an earlier plan stays.
    pub fn task_autotitle(&self, task_id: &str, title: &str, op_id: Option<&str>) -> Result<Value> {
        self.write(op_id, |tx| {
            let row = task_by_id(&tx.tx, task_id)?;
            let default = row.meta.get("from") == Some(&json!("vault")) && row.title.starts_with("Agent on ");
            if title.is_empty() || !default {
                return Ok(json!({"task_id": row.task_id, "retitled": false}));
            }
            tx.tx.execute("UPDATE tasks SET title=?, updated=? WHERE task_id=?", params![title, tx.now, row.task_id])?;
            tx.event(&row.task_id, "title", json!({"title": title}))?;
            Ok(json!({"task_id": row.task_id, "retitled": true}))
        })
    }

    pub fn task_session_end(&self, task_id: &str, session_id: &str, op_id: Option<&str>) -> Result<Value> {
        self.write(op_id, |tx| {
            let tid = task_by_id(&tx.tx, task_id)?.task_id;
            tx.tx.execute(
                "UPDATE sessions SET active=0, ended=? WHERE session_id=? AND task_id=?",
                params![tx.now, session_id, tid],
            )?;
            tx.event(&tid, "session_end", json!({"session_id": session_id}))?;
            Ok(json!({"task_id": tid}))
        })
    }

    /// A session takes a card as its main session. A Needs-you or Stale card moves to Working.
    pub fn task_claim(&self, task_id: &str, session_id: &str, seat: Seat, op_id: Option<&str>) -> Result<Value> {
        self.write(op_id, |tx| {
            let row = task_by_id(&tx.tx, task_id)?;
            if row.kind == "approval" {
                return refuse("an approval card is not work to claim");
            }
            if row.section == HISTORY {
                return refuse(format!("task {} is in History: start a new one from it", row.task_id));
            }
            let tid = row.task_id.clone();
            Store::add_session(tx, &tid, session_id, "main", &seat)?;
            let (mut section, mut status) = (row.section.clone(), row.status.clone());
            if [NEEDS_YOU, STALE, PROPOSED].contains(&section.as_str()) {
                if row.kind == "note" {
                    tx.tx.execute("UPDATE tasks SET kind='task' WHERE task_id=?", [&tid])?;
                }
                section = WORKING.into();
                status = None;
                Store::set_card(tx, &tid, WORKING, None, "claim", json!({"session_id": session_id}))?;
            }
            Ok(json!({"task_id": tid, "title": row.title, "section": section, "status": status}))
        })
    }

    /// Record a session's plan or todo list on its card. Neither moves the card.
    pub fn task_detail(&self, task_id: &str, plan: Option<&str>, todos: Option<&[Value]>, op_id: Option<&str>) -> Result<Value> {
        self.write(op_id, |tx| {
            let row = task_by_id(&tx.tx, task_id)?;
            let mut meta = row.meta.clone();
            if let Some(p) = plan {
                meta.insert("plan".into(), json!(p.chars().take(PLAN_CHARS).collect::<String>()));
            }
            if let Some(items) = todos {
                let list: Vec<Value> = items
                    .iter()
                    .take(TODO_ITEMS)
                    .filter_map(Value::as_object)
                    .map(|i| {
                        let f = |k: &str| match i.get(k) {
                            None | Some(Value::Null) => String::new(),
                            Some(Value::String(s)) => s.clone(),
                            Some(Value::Bool(false)) => String::new(),
                            Some(v) => crate::api::py_str(v),
                        };
                        json!({"id": f("id"), "subject": f("subject"), "status": f("status")})
                    })
                    .collect();
                meta.insert("todos".into(), Value::Array(list));
            }
            tx.tx.execute(
                "UPDATE tasks SET meta=?, updated=? WHERE task_id=?",
                params![Value::Object(meta).to_string(), tx.now, row.task_id],
            )?;
            tx.event(&row.task_id, "detail", json!({"plan": plan.is_some(), "todos": todos.is_some()}))?;
            Ok(json!({"task_id": row.task_id}))
        })
    }

    /// A message on a card, for Tony and every session that reads it. It moves no card.
    pub fn task_message(&self, task_id: &str, body: &str, author: Option<&str>, node: Option<&str>, session_id: Option<&str>, op_id: Option<&str>) -> Result<Value> {
        if body.trim().is_empty() {
            return refuse("a message needs a body");
        }
        let body: String = body.chars().take(MESSAGE_CHARS).collect();
        self.write(op_id, |tx| {
            let tid = task_by_id(&tx.tx, task_id)?.task_id;
            tx.tx.execute(
                "INSERT INTO messages(task_id, ts, author, node, session_id, body) VALUES (?,?,?,?,?,?)",
                params![tid, tx.now, author, node, session_id, body],
            )?;
            let seq = tx.tx.last_insert_rowid();
            tx.event(&tid, "message", json!({"seq": seq}))?;
            Ok(json!({"task_id": tid, "seq": seq}))
        })
    }

    pub fn task_messages(&self, task_id: &str, after: i64) -> Result<Vec<Value>> {
        let db = self.conn();
        let tid = task_by_id(&db, task_id)?.task_id;
        messages_of(&db, &tid, after)
    }

    /// One card with its sessions, as `task_get` answers.
    pub fn task_get(&self, task_id: &str) -> Result<Value> {
        let db = self.conn();
        let row = task_by_id(&db, task_id)?;
        let mut card = card_json(&row);
        card["sessions"] = Value::Array(sessions_of(&db, &row.task_id)?);
        Ok(card)
    }

    /// Cards newest first, one page at a time (R61 item 4). History orders on `done_at`, the
    /// other sections on `updated`. `next_before` is the next page's cursor, or null.
    pub fn task_list(&self, section: Option<&str>, q: Option<&str>, before: Option<f64>, limit: Option<i64>) -> Result<Value> {
        let limit = limit.unwrap_or(PAGE).clamp(1, 200);
        let mut sql = format!("SELECT {TASK_COLS} FROM tasks WHERE folded_into IS NULL");
        let mut args: Vec<rusqlite::types::Value> = vec![];
        if let Some(s) = section.filter(|s| !s.is_empty()) {
            if !SECTIONS.contains(&s) {
                return refuse(format!("section must be one of {SECTIONS:?}"));
            }
            sql.push_str(" AND section=?");
            args.push(s.to_string().into());
        }
        if let Some(q) = q.filter(|s| !s.is_empty()) {
            sql.push_str(" AND (title LIKE ? OR task_id=? OR tags LIKE ? OR peer=?)");
            args.extend([format!("%{q}%").into(), q.to_string().into(), format!("%{q}%").into(), q.to_string().into()]);
        }
        let order = if section == Some(HISTORY) { "done_at" } else { "updated" };
        if let Some(b) = before {
            sql.push_str(&format!(" AND {order} < ?"));
            args.push(b.into());
        }
        sql.push_str(&format!(" ORDER BY {order} DESC LIMIT ?"));
        args.push((limit + 1).into());
        let db = self.conn();
        let mut st = db.prepare(&sql)?;
        let rows: Vec<TaskRow> = st.query_map(rusqlite::params_from_iter(args), task_row)?.collect::<rusqlite::Result<_>>()?;
        let more = rows.len() as i64 > limit;
        let cards: Vec<Value> = rows.iter().take(limit as usize).map(card_json).collect();
        let next = if more {
            let last = &rows[limit as usize - 1];
            if order == "done_at" { json!(last.done_at) } else { json!(last.updated) }
        } else {
            Value::Null
        };
        Ok(json!({"cards": cards, "next_before": next}))
    }

    pub fn kv_get(&self, key: &str) -> Result<Option<String>> {
        Ok(self.conn().query_row("SELECT value FROM kv WHERE key=?", [key], |r| r.get(0)).optional()?)
    }

    pub fn kv_set(&self, key: &str, value: &str) -> Result<()> {
        self.conn().execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", params![key, value])?;
        Ok(())
    }

    pub fn max_event_id(&self) -> Result<i64> {
        Ok(self.conn().query_row("SELECT COALESCE(MAX(id), 0) FROM events", [], |r| r.get(0))?)
    }

    // -- Tony's actions, only through the vault API (R63) -------------------------------

    /// Close a card to History. A Stale card closes the same way (R61 item 4).
    pub fn card_close(&self, task_id: &str) -> Result<Value> {
        self.write(None, |tx| {
            let row = task_by_id(&tx.tx, task_id)?;
            if row.kind == "approval" {
                return refuse("an approval closes by approve, reject or cancel");
            }
            Store::set_card(tx, &row.task_id, HISTORY, Some("done"), "close", json!({}))?;
            Ok(json!({"task_id": row.task_id, "section": HISTORY, "status": "done"}))
        })
    }

    /// The card of an `agent` order: Needs you until its session reports, with the note it
    /// started from.
    pub fn agent_card(&self, node: &str, note_id: Option<&str>) -> Result<Value> {
        let title = match note_id {
            Some(n) => format!("Agent on {node} from note {n}"),
            None => format!("Agent on {node}"),
        };
        self.task_open(&title, Open {
            peer: Some(node.into()),
            section: Some(NEEDS_YOU.into()),
            source_note: note_id.map(str::to_string),
            meta: Some(Map::from_iter([("from".into(), json!("vault"))])),
            ..Open::default()
        }, None)
    }

    /// An `agent` order that failed retires its card, unless a session reached it after all.
    pub fn agent_order_failed(&self, task_id: &str, note: &str) -> Result<()> {
        self.write(None, |tx| {
            let row = task_by_id(&tx.tx, task_id)?;
            let sessions: i64 = tx.tx.query_row("SELECT COUNT(*) FROM sessions WHERE task_id=?", [&row.task_id], |r| r.get(0))?;
            if sessions == 0 && row.section == NEEDS_YOU {
                Store::set_card(tx, &row.task_id, HISTORY, Some("failed"), "order_failed", json!({"note": note}))?;
            }
            Ok(json!(null))
        })?;
        Ok(())
    }

    // -- approvals (fleet.md R97 item 1, as amended: a request belongs to its task) -----

    /// Tag the requester's card in Needs you for one new request. A requester with no card
    /// gets one.
    pub fn approval_attach(&self, session_id: Option<&str>, request_id: &str, title: &str, node: Option<&str>, summary: &str) -> Result<Value> {
        let own = match session_id {
            Some(s) => self.task_for_session(s)?,
            None => None,
        };
        let tid = match own {
            Some(t) => t,
            None => {
                let opened = self.task_open(title, Open {
                    session_id: session_id.map(str::to_string),
                    peer: node.map(str::to_string),
                    ..Open::default()
                }, None)?;
                opened["task_id"].as_str().unwrap_or_default().to_string()
            }
        };
        self.write(None, |tx| {
            let row = task_by_id(&tx.tx, &tid)?;
            let mut meta = row.meta.clone();
            meta.insert("reason".into(), json!(summary));
            tx.tx.execute("UPDATE tasks SET meta=? WHERE task_id=?", params![Value::Object(meta).to_string(), row.task_id])?;
            Store::set_card(tx, &row.task_id, NEEDS_YOU, Some("approval"), "approval", json!({"request_id": request_id, "state": "pending"}))?;
            Ok(json!({"task_id": row.task_id}))
        })
    }

    /// One decision on a request. An old approval card goes to History; a task card leaves
    /// Needs you once no other request of its waits.
    pub fn approval_resolve(&self, task_id: &str, request_id: &str, outcome: &str, others_pending: bool) -> Result<Value> {
        if !APPROVAL_OUTCOMES.contains(&outcome) {
            return refuse(format!("outcome must be one of {APPROVAL_OUTCOMES:?}"));
        }
        let state = if outcome == "denied" { "rejected" } else { outcome };
        self.write(None, |tx| {
            let row = task_by_id(&tx.tx, task_id)?;
            let data = json!({"request_id": request_id, "state": state, "outcome": outcome});
            if row.kind == "approval" {
                Store::set_card(tx, &row.task_id, HISTORY, Some(outcome), "approval", data)?;
            } else if row.section == NEEDS_YOU && row.status.as_deref() == Some("approval") && !others_pending {
                let mut meta = row.meta.clone();
                meta.insert("reason".into(), Value::Null);
                tx.tx.execute("UPDATE tasks SET meta=? WHERE task_id=?", params![Value::Object(meta).to_string(), row.task_id])?;
                Store::set_card(tx, &row.task_id, WORKING, None, "approval", data)?;
            } else {
                tx.tx.execute("UPDATE tasks SET updated=? WHERE task_id=?", params![tx.now, row.task_id])?;
                tx.event(&row.task_id, "approval", data)?;
            }
            Ok(json!({"task_id": row.task_id, "status": outcome}))
        })
    }

    // -- the board feed for trilium (CONTRACT.md b11adebc items 2 to 5) -----------------

    /// The cards that changed after event `after`, at most `limit` events at a time.
    pub fn board_changes(&self, after: i64, limit: Option<i64>) -> Result<Value> {
        let limit = limit.unwrap_or(CHANGES_MAX).clamp(1, CHANGES_MAX);
        let approvals = self.approvals.lock().expect("approvals lock").clone();
        let db = self.conn();
        let mut st = db.prepare("SELECT id, task_id FROM events WHERE id>? ORDER BY id LIMIT ?")?;
        let rows: Vec<(i64, String)> = st.query_map(params![after, limit + 1], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
        let more = rows.len() as i64 > limit;
        let page = &rows[..rows.len().min(limit as usize)];
        let mark = page.last().map(|(id, _)| *id).unwrap_or(after);
        let mut seen = HashSet::new();
        let mut cards = vec![];
        for (_, tid) in page {
            if !seen.insert(tid.clone()) {
                continue;
            }
            let Some(row) = raw_task(&db, tid)? else { continue };
            cards.push(board_card(&db, &row, approvals.as_ref())?);
        }
        Ok(json!({"cards": cards, "mark": mark, "more": more}))
    }

    /// The Ops notices, for the vault's Ops book.
    pub fn ops_list(&self) -> Result<Value> {
        let db = self.conn();
        let mut st = db.prepare("SELECT kind, node, title, body, ts FROM ops_notes ORDER BY kind, node")?;
        let notes: Vec<Value> = st
            .query_map([], |r| {
                Ok(json!({"kind": r.get::<_, String>(0)?, "node": r.get::<_, String>(1)?, "title": r.get::<_, String>(2)?,
                          "body": r.get::<_, String>(3)?, "ts": r.get::<_, f64>(4)?}))
            })?
            .collect::<rusqlite::Result<_>>()?;
        Ok(json!({"notes": notes}))
    }

    /// One service's notice, one per kind and node.
    pub fn ops_post(&self, kind: &str, node: &str, title: &str, body: &str, op_id: Option<&str>) -> Result<Value> {
        let out = self.write(op_id, |tx| {
            tx.tx.execute(
                "INSERT INTO ops_notes(kind, node, title, body, ts) VALUES (?,?,?,?,?) ON CONFLICT(kind, node) DO UPDATE SET \
                 title=excluded.title, body=excluded.body, ts=excluded.ts",
                params![kind, node, title, body, tx.now],
            )?;
            Ok(json!({"ok": true}))
        })?;
        let hook = self.on_notice.lock().expect("hook lock").clone();
        if let Some(h) = hook {
            h();
        }
        Ok(out)
    }

    // -- Stale (R61 item 4) ------------------------------------------------------------------

    /// A session that went: its row ends, and a Working or Needs-you card with no live
    /// session left moves to Stale, or to History as done when the session was retired.
    /// Answers the tasks it moved.
    pub fn session_gone(&self, session_id: &str, retired: bool) -> Result<Vec<String>> {
        let v = self.write(None, |tx| {
            let ids = match_rows(&tx.tx, session_id)?;
            let mut moved = vec![];
            for (sid, tid) in ids {
                tx.tx.execute("UPDATE sessions SET active=0, ended=? WHERE session_id=?", params![tx.now, sid])?;
                if let Some(t) = end_if_orphaned(tx, &tid, &sid, retired)? {
                    moved.push(t);
                }
            }
            Ok(json!(moved))
        })?;
        Ok(serde_json::from_value(v).unwrap_or_default())
    }

    /// A session that is live: its row is active again, and a Stale card moves to Working.
    pub fn session_live(&self, session_id: &str) -> Result<Vec<String>> {
        let v = self.write(None, |tx| {
            let ids = match_rows(&tx.tx, session_id)?;
            let mut moved = vec![];
            for (sid, tid) in ids {
                tx.tx.execute("UPDATE sessions SET active=1, ended=NULL WHERE session_id=?", [&sid])?;
                if let Some(t) = unstale(tx, &tid, &sid)? {
                    moved.push(t);
                }
            }
            Ok(json!(moved))
        })?;
        Ok(serde_json::from_value(v).unwrap_or_default())
    }

    /// A main session's live state moves its card (R61 item 2): `blocked` to Needs you with
    /// `needs` as the reason, `working` out of Needs you or Stale, and `done` out of Stale or out
    /// of a Needs you that a block put there, so a `request_human` outlives the turn that ends
    /// after it, or a Needs you with no request. History, approval cards and a pending approval
    /// stay. Answers the tasks it moved.
    pub fn session_state(&self, session_id: &str, state: &str, needs: Option<&str>) -> Result<Vec<String>> {
        let v = self.write(None, |tx| {
            let mut moved = vec![];
            for (sid, tid) in match_rows(&tx.tx, session_id)? {
                let main: bool = tx.tx.query_row("SELECT role='main' FROM sessions WHERE session_id=?", [&sid], |r| r.get(0))?;
                let row = task_by_id(&tx.tx, &tid)?;
                if !main || row.kind == "approval" || row.status.as_deref() == Some("approval") {
                    continue;
                }
                let by_state = row.meta.contains_key(BLOCKED_BY_STATE);
                let blocked = match (state, row.section.as_str()) {
                    ("blocked", NEEDS_YOU) if by_state && row.meta.get("reason").and_then(Value::as_str) == needs => continue,
                    ("blocked", WORKING | NEEDS_YOU | STALE) => true,
                    ("working", NEEDS_YOU | STALE) | ("done", STALE) => false,
                    ("done", NEEDS_YOU) if by_state || row.status.is_none() => false,
                    _ => continue,
                };
                let mut meta = row.meta.clone();
                meta.insert("reason".into(), if blocked { json!(needs) } else { Value::Null });
                match blocked {
                    true => meta.insert(BLOCKED_BY_STATE.into(), json!(true)),
                    false => meta.remove(BLOCKED_BY_STATE),
                };
                tx.tx.execute("UPDATE tasks SET meta=? WHERE task_id=?", params![Value::Object(meta).to_string(), row.task_id])?;
                let (section, status) = if blocked { (NEEDS_YOU, Some("blocked")) } else { (WORKING, None) };
                Store::set_card(tx, &row.task_id, section, status, "state", json!({"session_id": sid, "reason": needs}))?;
                moved.push(row.task_id);
            }
            Ok(json!(moved))
        })?;
        Ok(serde_json::from_value(v).unwrap_or_default())
    }

    /// A session took a new name. Its row keeps the name, and a card it is the main session of
    /// takes the name as its title. Returns the retitled cards.
    pub fn session_renamed(&self, session_id: &str, name: &str) -> Result<Vec<String>> {
        if name.is_empty() {
            return Ok(vec![]);
        }
        let v = self.write(None, |tx| {
            let mut retitled = vec![];
            for (sid, tid) in match_rows(&tx.tx, session_id)? {
                tx.tx.execute("UPDATE sessions SET name=? WHERE session_id=?", params![name, sid])?;
                let n = tx.tx.execute(
                    "UPDATE tasks SET title=?1, updated=?2 WHERE task_id=?3 AND title<>?1 \
                     AND EXISTS (SELECT 1 FROM sessions WHERE session_id=?4 AND task_id=?3 AND role='main')",
                    params![name, tx.now, tid, sid],
                )?;
                if n > 0 {
                    tx.event(&tid, "title", json!({"title": name}))?;
                    retitled.push(tid);
                }
            }
            Ok(json!(retitled))
        })?;
        Ok(serde_json::from_value(v).unwrap_or_default())
    }

    /// Reconcile with telemetry's roster: on each node it lists, an active row with no live
    /// session ends, an ended row whose session is live comes back, and a live session's state
    /// moves its card. Nodes the roster does not list are left alone.
    pub fn reconcile(&self, roster: &HashMap<String, Vec<LiveSession>>) -> Result<Value> {
        let rows: Vec<(String, String, bool, Option<String>)> = {
            let db = self.conn();
            let mut st = db.prepare("SELECT session_id, node, active, name FROM sessions WHERE node IS NOT NULL")?;
            let rows = st
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get::<_, i64>(2)? != 0, r.get(3)?)))?
                .collect::<rusqlite::Result<_>>()?;
            rows
        };
        let (mut gone, mut live, mut moved) = (vec![], vec![], vec![]);
        for (sid, node, active, name) in rows {
            let Some(listed) = roster.get(&node) else { continue };
            let found = listed_session(listed, &sid);
            let listed_name = found.and_then(|l| l.name.as_deref()).filter(|_| active);
            if let Some(now) = listed_name.filter(|n| name.as_deref() != Some(*n)) {
                self.session_renamed(&sid, now)?;
            }
            match (active, found) {
                (true, None) => gone.extend(self.session_gone(&sid, false)?),
                (false, Some(_)) => live.extend(self.session_live(&sid)?),
                _ => {}
            }
            if let Some(l @ LiveSession { state: Some(state), .. }) = found {
                moved.extend(self.session_state(&sid, state, l.needs.as_deref())?);
            }
        }
        Ok(json!({"stale": gone, "working": live, "state": moved}))
    }

    /// Copy telemetry's link, project and scope onto each active session it lists. Only what
    /// differs is written, so an unchanged roster sends the board nothing.
    pub fn session_details(&self, roster: &HashMap<String, Vec<LiveSession>>) -> Result<()> {
        type Row = (String, String, String, Option<String>, Option<String>, Option<String>);
        let rows: Vec<Row> = {
            let db = self.conn();
            let mut st = db.prepare("SELECT session_id, task_id, node, rc_url, project, scope FROM sessions WHERE node IS NOT NULL AND active=1")?;
            let rows = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)))?
                .collect::<rusqlite::Result<_>>()?;
            rows
        };
        let changed: Vec<(String, String, &LiveSession)> = rows.into_iter().filter_map(|(sid, tid, node, link, project, scope)| {
            let found = listed_session(roster.get(&node)?, &sid)?;
            (found.link != link || found.project != project || found.scope != scope).then_some((sid, tid, found))
        }).collect();
        if changed.is_empty() {
            return Ok(());
        }
        self.write(None, |tx| {
            for (sid, tid, s) in &changed {
                tx.tx.execute("UPDATE sessions SET rc_url=?, project=?, scope=? WHERE session_id=?", params![s.link, s.project, s.scope, sid])?;
                tx.event(tid, "session_details", json!({"session_id": sid}))?;
            }
            Ok(json!(null))
        })?;
        Ok(())
    }

    /// Undo the port's section rename, for a rollback to the Python store.
    pub fn downgrade_sections(&self) -> Result<Value> {
        let db = self.conn();
        let h = db.execute("UPDATE tasks SET section='done' WHERE section='history'", [])?;
        let s = db.execute("UPDATE tasks SET section='working' WHERE section='stale'", [])?;
        Ok(json!({"history_to_done": h, "stale_to_working": s}))
    }
}

fn listed_session<'a>(listed: &'a [LiveSession], session_id: &str) -> Option<&'a LiveSession> {
    listed.iter().find(|l| l.session_id.as_deref().is_some_and(|s| same_session(s, session_id)))
}

fn same_session(a: &str, b: &str) -> bool {
    crate::front::norm(a) == crate::front::norm(b)
}

/// The (session_id, task_id) rows a telemetry event names.
fn match_rows(db: &Connection, session_id: &str) -> Result<Vec<(String, String)>> {
    let mut st = db.prepare("SELECT session_id, task_id FROM sessions WHERE session_id = ?")?;
    let rows = st.query_map(params![session_id], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
    Ok(rows)
}

fn end_if_orphaned(tx: &mut Tx, task_id: &str, session_id: &str, retired: bool) -> Result<Option<String>> {
    let row = task_by_id(&tx.tx, task_id)?;
    tx.event(&row.task_id, "session_gone", json!({"session_id": session_id}))?;
    if ![WORKING, NEEDS_YOU].contains(&row.section.as_str()) || live_sessions(&tx.tx, &row.task_id)? > 0 {
        return Ok(None);
    }
    if retired {
        Store::set_card(tx, &row.task_id, HISTORY, Some("done"), "retired", json!({"session_id": session_id}))?;
        return Ok(Some(row.task_id));
    }
    tx.tx.execute("UPDATE tasks SET section=?, updated=? WHERE task_id=?", params![STALE, tx.now, row.task_id])?;
    tx.event(&row.task_id, "stale", json!({"section": STALE, "status": row.status, "from": row.section}))?;
    Ok(Some(row.task_id))
}

fn unstale(tx: &mut Tx, task_id: &str, session_id: &str) -> Result<Option<String>> {
    let row = task_by_id(&tx.tx, task_id)?;
    tx.event(&row.task_id, "session_live", json!({"session_id": session_id}))?;
    if row.section != STALE {
        return Ok(None);
    }
    Store::set_card(tx, &row.task_id, WORKING, None, "live", json!({"session_id": session_id}))?;
    Ok(Some(row.task_id))
}

fn migrate(db: &Connection) -> rusqlite::Result<()> {
    for (table, column) in [("tasks", "body"), ("tasks", "source_note"), ("sessions", "project"), ("sessions", "scope")] {
        if !has_column(db, table, column)? {
            db.execute_batch(&format!("ALTER TABLE {table} ADD COLUMN {column} TEXT"))?;
        }
    }
    db.execute_batch(ADDITIVE)?;
    db.execute("UPDATE tasks SET section='history' WHERE section='done'", [])?;
    Ok(())
}

fn card_json(row: &TaskRow) -> Value {
    json!({
        "task_id": row.task_id, "title": row.title, "kind": row.kind, "section": row.section,
        "status": row.status, "peer": row.peer, "tags": row.tags, "body_ref": row.body_ref,
        "body": row.body, "source_note": row.source_note, "meta": Value::Object(row.meta.clone()),
        "parent_task": row.parent_task, "created": row.created, "updated": row.updated, "done_at": row.done_at,
    })
}

/// A card as `board_changes` carries it (CONTRACT.md item 4).
fn board_card(db: &Connection, row: &TaskRow, approvals: Option<&ApprovalsOf>) -> Result<Value> {
    let meta = &row.meta;
    let mut card = json!({
        "task_id": row.task_id, "title": row.title, "kind": row.kind, "section": row.section,
        "status": row.status, "peer": row.peer, "tags": row.tags,
        "meta": {"reason": meta.get("reason").cloned().unwrap_or(Value::Null),
                 "plan": meta.get("plan").cloned().unwrap_or(Value::Null),
                 "todos": meta.get("todos").cloned().unwrap_or(Value::Null)},
        "created": row.created, "updated": row.updated, "done_at": row.done_at, "parent_task": row.parent_task,
        "sessions": sessions_of(db, &row.task_id)?,
        "source_note": row.source_note,
        "approvals": approvals.map(|f| f(&row.task_id)).unwrap_or_default(),
    });
    if let Some(b) = &row.body {
        card["body"] = json!(b);
    }
    let messages = messages_of(db, &row.task_id, 0)?;
    if !messages.is_empty() {
        card["messages"] = Value::Array(messages);
    }
    if let Some(f) = row.folded_into.as_ref().filter(|f| !f.is_empty()) {
        card["folded_into"] = json!(f);
    }
    Ok(card)
}
