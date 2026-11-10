//! `seats.db`: this node's seats, their waiting lines, the latest line per agent, and the outbox
//! to social.

use std::path::Path;
use std::sync::{Arc, Mutex};

use rusqlite::{params, Connection, OptionalExtension, Row};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

const SCHEMA: &str = "
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
    platform     TEXT NOT NULL DEFAULT '',
    server_label TEXT NOT NULL DEFAULT '',
    chat_label   TEXT NOT NULL DEFAULT '',
    brief        TEXT NOT NULL DEFAULT '',
    name         TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL DEFAULT 'empty',
    job          TEXT NOT NULL DEFAULT '',
    adopted      INTEGER NOT NULL DEFAULT 0,
    last_message REAL NOT NULL DEFAULT 0,
    deadline     REAL NOT NULL DEFAULT 0,
    previous     TEXT NOT NULL DEFAULT '{}',
    breaker      TEXT NOT NULL DEFAULT '{}',
    held         TEXT NOT NULL DEFAULT '',
    missed       INTEGER NOT NULL DEFAULT 0,
    remote_control INTEGER NOT NULL DEFAULT 0,
    rc_url       TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    effort       TEXT NOT NULL DEFAULT '',
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
    delivered_at REAL NOT NULL DEFAULT 0,
    origin       INTEGER UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_seat_queue_chat ON seat_queue (account, channel_id, delivered_at);
CREATE TABLE IF NOT EXISTS seat_lines (
    job          TEXT PRIMARY KEY,
    account      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    thread_id    TEXT NOT NULL DEFAULT '',
    message_id   TEXT NOT NULL DEFAULT '',
    at           REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
";

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum State {
    #[default]
    Empty,
    Starting,
    Occupied,
}

impl State {
    pub fn as_str(self) -> &'static str {
        match self {
            State::Empty => "empty",
            State::Starting => "starting",
            State::Occupied => "occupied",
        }
    }
    fn parse(s: &str) -> Self {
        match s {
            "starting" => State::Starting,
            "occupied" => State::Occupied,
            _ => State::Empty,
        }
    }
}

/// The start breaker: 4 failed starts in an hour hold the seat for 6 hours.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Breaker {
    #[serde(default)]
    pub attempts: Vec<f64>,
    #[serde(default)]
    pub next_try: f64,
    #[serde(default)]
    pub hold_until: f64,
}

pub const BUDGET: usize = 4;
pub const DECAY_S: f64 = 3600.0;
pub const BACKOFF_S: [f64; 4] = [60.0, 120.0, 300.0, 600.0];
pub const HOLD_S: f64 = 6.0 * 3600.0;

/// Where a failed start leaves its streak.
#[derive(Debug, PartialEq)]
pub enum Failure {
    First,
    Again,
    Tripped,
}

impl Breaker {
    /// Count one failed start.
    pub fn fail(&mut self, now: f64) -> Failure {
        self.attempts.retain(|a| now - a < DECAY_S);
        self.attempts.push(now);
        let n = self.attempts.len();
        if n >= BUDGET {
            self.hold_until = now + HOLD_S;
            self.next_try = self.hold_until;
            self.attempts.clear();
            return Failure::Tripped;
        }
        self.next_try = now + BACKOFF_S[n - 1];
        if n == 1 { Failure::First } else { Failure::Again }
    }

    pub fn allows(&self, now: f64) -> bool {
        now >= self.next_try && now >= self.hold_until
    }

    #[cfg(test)]
    pub fn held(&self, now: f64) -> bool {
        now < self.hold_until
    }
}

/// The agent before the current one, for the next brief.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Previous {
    #[serde(default)]
    pub job: String,
    #[serde(default)]
    pub node: String,
    /// A session the seat took over rather than started, so no later start may replace it.
    #[serde(default)]
    pub adopted: bool,
    /// The seat stopped it for idleness, so the next message resumes its conversation.
    #[serde(default)]
    pub stopped: bool,
}

/// What social declares about a chat. It rides on every row social sends.
#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
#[serde(default)]
pub struct Decl {
    pub account: String,
    pub channel_id: String,
    pub channel_name: String,
    pub kind: String,
    pub server: String,
    pub slug: String,
    pub node: String,
    pub scope: String,
    pub level: String,
    pub platform: String,
    pub server_label: String,
    pub chat_label: String,
    /// The seat's brief, relative to the seat docs.
    pub brief: String,
    /// The agent's session name, `seat:<app>/<server>/<chat>`.
    pub name: String,
    /// The seat's own model and effort. Empty means the node's `[seats]` pair.
    pub model: String,
    pub effort: String,
}

/// A seat's state as social's database held it before seats moved to their hosts.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Seed {
    pub state: String,
    pub job: String,
    pub job_node: String,
    pub adopted: i64,
    pub last_message: f64,
    pub deadline: f64,
    pub previous: String,
    pub breaker: String,
    pub held: String,
    pub missed: i64,
    pub remote_control: i64,
    pub rc_url: String,
}

/// One `seat_registry` row.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Seat {
    pub decl: Decl,
    pub state: State,
    pub job: String,
    pub adopted: bool,
    pub last_message: f64,
    /// Starting: when the agent must be up.
    pub deadline: f64,
    pub previous: Previous,
    pub breaker: Breaker,
    /// Why the seat holds its lines: `outside:<sender>`, `scope`, or `admitted`.
    pub held: String,
    /// Messages that arrived while the seats were paused and the seat was empty, for the
    /// summary at unpause and the next agent's first prompt, which clears it.
    pub missed: i64,
    /// The seat's Remote Control switch, which every agent it starts follows.
    pub remote_control: bool,
    /// The live agent's claude.ai link, while the switch is on.
    pub rc_url: String,
}

const COLS: &str = "account, channel_id, channel_name, kind, server, slug, node, scope, level, platform, server_label, chat_label, brief, name, state, job, adopted, last_message, deadline, previous, breaker, held, missed, remote_control, rc_url, model, effort";

impl Seat {
    fn from_row(r: &Row) -> rusqlite::Result<Self> {
        let previous: String = r.get(19)?;
        let breaker: String = r.get(20)?;
        Ok(Self {
            decl: Decl {
                account: r.get(0)?,
                channel_id: r.get(1)?,
                channel_name: r.get(2)?,
                kind: r.get(3)?,
                server: r.get(4)?,
                slug: r.get(5)?,
                node: r.get(6)?,
                scope: r.get(7)?,
                level: r.get(8)?,
                platform: r.get(9)?,
                server_label: r.get(10)?,
                chat_label: r.get(11)?,
                brief: r.get(12)?,
                name: r.get(13)?,
                model: r.get(25)?,
                effort: r.get(26)?,
            },
            state: State::parse(&r.get::<_, String>(14)?),
            job: r.get(15)?,
            adopted: r.get::<_, i64>(16)? != 0,
            last_message: r.get(17)?,
            deadline: r.get(18)?,
            previous: serde_json::from_str(&previous).unwrap_or_default(),
            breaker: serde_json::from_str(&breaker).unwrap_or_default(),
            held: r.get(21)?,
            missed: r.get(22)?,
            remote_control: r.get::<_, i64>(23)? != 0,
            rc_url: r.get(24)?,
        })
    }

    /// A seat from its old row. A job on another node stays there, so the seat starts empty.
    pub fn seeded(decl: Decl, seed: Seed) -> Self {
        let elsewhere = !seed.job.is_empty() && seed.job_node != decl.node;
        let previous: Previous = serde_json::from_str(&seed.previous).unwrap_or_default();
        Self {
            state: if elsewhere { State::Empty } else { State::parse(&seed.state) },
            job: if elsewhere { String::new() } else { seed.job.clone() },
            adopted: !elsewhere && seed.adopted != 0,
            last_message: seed.last_message,
            deadline: seed.deadline,
            previous: if elsewhere { Previous { job: seed.job, node: seed.job_node, ..Default::default() } } else { previous },
            breaker: serde_json::from_str(&seed.breaker).unwrap_or_default(),
            held: seed.held,
            missed: seed.missed,
            remote_control: seed.remote_control != 0,
            rc_url: if elsewhere { String::new() } else { seed.rc_url },
            decl,
        }
    }

    /// The seat's current agent, as the next one's predecessor.
    pub fn as_previous(&self, stopped: bool) -> Previous {
        Previous { job: self.job.clone(), node: self.decl.node.clone(), adopted: self.adopted, stopped }
    }

    pub fn account(&self) -> &str {
        &self.decl.account
    }

    pub fn channel(&self) -> &str {
        &self.decl.channel_id
    }

    pub fn key(&self) -> (String, String) {
        (self.decl.account.clone(), self.decl.channel_id.clone())
    }

    /// The seat's id outside this plugin: `<account>/<channel_id>`, which holds no other `/`.
    pub fn id(&self) -> String {
        format!("{}/{}", self.decl.account, self.decl.channel_id)
    }

    /// The `social/seats` event of this row.
    pub fn event(&self, ts: &str) -> Value {
        let mut e = json!({"seat": self.id(), "label": self.decl.name, "state": self.state.as_str(), "remote_control": self.remote_control, "node": self.decl.node});
        if !self.job.is_empty() {
            e["job"] = json!(self.job);
        }
        if !self.rc_url.is_empty() {
            e["rc_url"] = json!(self.rc_url);
        }
        e["ts"] = json!(ts);
        e
    }

    /// What `seats list` and `social seats` show of the seat.
    pub fn public(&self, queued: i64) -> Value {
        json!({
            "state": self.state.as_str(), "job": if self.job.is_empty() { Value::Null } else { json!(self.job) },
            "node": self.decl.node, "level": self.decl.level, "scope": self.decl.scope, "adopted": self.adopted,
            "queued": queued, "held": if self.held.is_empty() || self.held == "admitted" { Value::Null } else { json!(self.held) },
            "breaker": if self.breaker.hold_until > 0.0 { json!({"hold_until": self.breaker.hold_until}) } else { Value::Null },
            "id": self.id(), "remote_control": self.remote_control, "rc_url": self.rc_url,
        })
    }
}

/// One `seat_queue` row: a line no agent has taken yet.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Queued {
    pub id: i64,
    pub message_id: String,
    pub thread_id: String,
    pub line: String,
    pub briefed_to: String,
}

/// The chat and message of the latest line an agent got.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Latest {
    pub account: String,
    pub channel: String,
    pub thread: String,
    pub message_id: String,
}

/// One outbox row.
#[derive(Debug, Clone, PartialEq)]
pub struct Outgoing {
    pub id: i64,
    pub kind: String,
    pub body: Value,
    pub at: f64,
}

fn save(c: &Connection, s: &Seat) -> rusqlite::Result<bool> {
    let before = c.query_row(&format!("SELECT {COLS} FROM seat_registry WHERE account = ?1 AND channel_id = ?2"), params![s.decl.account, s.decl.channel_id], Seat::from_row).optional()?;
    let d = &s.decl;
    c.execute(
        &format!("INSERT OR REPLACE INTO seat_registry ({COLS}) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23, ?24, ?25, ?26, ?27)"),
        params![
            d.account, d.channel_id, d.channel_name, d.kind, d.server, d.slug, d.node, d.scope, d.level, d.platform, d.server_label, d.chat_label, d.brief, d.name,
            s.state.as_str(), s.job, s.adopted as i64, s.last_message, s.deadline,
            serde_json::to_string(&s.previous).unwrap_or_default(), serde_json::to_string(&s.breaker).unwrap_or_default(),
            s.held, s.missed, s.remote_control as i64, s.rc_url, d.model, d.effort
        ],
    )?;
    Ok(before.is_none_or(|b| b.public(0) != s.public(0) || b.decl.name != s.decl.name))
}

/// The plugin's database.
#[derive(Clone)]
pub struct Store(Arc<Mutex<Connection>>);

impl Store {
    pub fn open(path: &Path) -> anyhow::Result<Self> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let conn = Connection::open(path)?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.execute_batch(SCHEMA)?;
        for (column, want, change) in [
            ("model", true, "ADD COLUMN model TEXT NOT NULL DEFAULT ''"),
            ("effort", true, "ADD COLUMN effort TEXT NOT NULL DEFAULT ''"),
            ("done_at", false, "DROP COLUMN done_at"),
        ] {
            let has: bool = conn.query_row("SELECT COUNT(*) > 0 FROM pragma_table_info('seat_registry') WHERE name = ?1", [column], |r| r.get(0))?;
            if has != want {
                conn.execute_batch(&format!("ALTER TABLE seat_registry {change}"))?;
            }
        }
        Ok(Self(Arc::new(Mutex::new(conn))))
    }

    async fn run<R, F>(&self, f: F) -> R
    where
        F: FnOnce(&Connection) -> R + Send + 'static,
        R: Send + 'static,
    {
        let me = self.0.clone();
        tokio::task::spawn_blocking(move || f(&me.lock().unwrap_or_else(|p| p.into_inner()))).await.expect("a db call panicked")
    }

    pub async fn get(&self, account: &str, channel: &str) -> Option<Seat> {
        let (a, ch) = (account.to_string(), channel.to_string());
        self.run(move |c| c.query_row(&format!("SELECT {COLS} FROM seat_registry WHERE account = ?1 AND channel_id = ?2"), params![a, ch], Seat::from_row).optional().ok().flatten()).await
    }

    pub async fn all(&self) -> Vec<Seat> {
        self.run(|c| c.prepare(&format!("SELECT {COLS} FROM seat_registry ORDER BY account, channel_id")).and_then(|mut s| s.query_map([], Seat::from_row)?.collect()).unwrap_or_default()).await
    }

    /// Write the row. True when what social is told about it changed.
    pub async fn save(&self, s: &Seat) -> bool {
        let s = s.clone();
        match self.run(move |c| save(c, &s)).await {
            Ok(changed) => changed,
            Err(e) => {
                tracing::warn!("seat save failed: {e}");
                false
            }
        }
    }

    /// Every seat `job` is the agent of.
    pub async fn of_job(&self, job: &str) -> Vec<Seat> {
        let j = job.to_string();
        self.run(move |c| c.prepare(&format!("SELECT {COLS} FROM seat_registry WHERE job = ?1")).and_then(|mut s| s.query_map(params![j], Seat::from_row)?.collect()).unwrap_or_default()).await
    }

    /// Queue one line. `origin` is social's row id, so a row sent twice is queued once.
    pub async fn enqueue(&self, origin: Option<i64>, account: &str, channel: &str, message_id: &str, thread_id: &str, line: &str, at: f64) {
        let (a, ch, id, t, l) = (account.to_string(), channel.to_string(), message_id.to_string(), thread_id.to_string(), line.to_string());
        let _ = self.run(move |c| c.execute("INSERT OR IGNORE INTO seat_queue (account, channel_id, message_id, thread_id, line, queued_at, origin) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)", params![a, ch, id, t, l, at, origin])).await;
    }

    /// The undelivered lines of a seat, oldest first.
    pub async fn pending(&self, account: &str, channel: &str) -> Vec<Queued> {
        let (a, ch) = (account.to_string(), channel.to_string());
        self.run(move |c| {
            c.prepare("SELECT id, message_id, thread_id, line, briefed_to FROM seat_queue WHERE account = ?1 AND channel_id = ?2 AND delivered_at = 0 ORDER BY id")
                .and_then(|mut s| s.query_map(params![a, ch], |r| Ok(Queued { id: r.get(0)?, message_id: r.get(1)?, thread_id: r.get(2)?, line: r.get(3)?, briefed_to: r.get(4)? }))?.collect())
                .unwrap_or_default()
        })
        .await
    }

    pub async fn brief_to(&self, ids: Vec<i64>, job: &str) {
        let j = job.to_string();
        let _ = self
            .run(move |c| -> rusqlite::Result<()> {
                for id in ids {
                    c.execute("UPDATE seat_queue SET briefed_to = ?1 WHERE id = ?2 AND delivered_at = 0", params![j, id])?;
                }
                Ok(())
            })
            .await;
    }

    /// Mark every line briefed to `job` on this seat delivered.
    pub async fn deliver_briefed(&self, account: &str, channel: &str, job: &str, at: f64) -> usize {
        let (a, ch, j) = (account.to_string(), channel.to_string(), job.to_string());
        self.run(move |c| c.execute("UPDATE seat_queue SET delivered_at = ?1 WHERE account = ?2 AND channel_id = ?3 AND briefed_to = ?4 AND delivered_at = 0", params![at, a, ch, j]).unwrap_or(0)).await
    }

    /// Put lines briefed to a job that never came up back in the queue.
    pub async fn unbrief(&self, account: &str, channel: &str) {
        let (a, ch) = (account.to_string(), channel.to_string());
        let _ = self.run(move |c| c.execute("UPDATE seat_queue SET briefed_to = '' WHERE account = ?1 AND channel_id = ?2 AND delivered_at = 0", params![a, ch])).await;
    }

    pub async fn delivered(&self, id: i64, at: f64) {
        let _ = self.run(move |c| c.execute("UPDATE seat_queue SET delivered_at = ?1 WHERE id = ?2", params![at, id])).await;
    }

    pub async fn set_line(&self, job: &str, latest: &Latest, at: f64) {
        let (j, l) = (job.to_string(), latest.clone());
        let _ = self
            .run(move |c| c.execute("INSERT OR REPLACE INTO seat_lines (job, account, channel_id, thread_id, message_id, at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)", params![j, l.account, l.channel, l.thread, l.message_id, at]))
            .await;
    }

    /// The latest line `job` got: what `seats send` defaults to.
    pub async fn line(&self, job: &str) -> Option<Latest> {
        let j = job.to_string();
        self.run(move |c| c.query_row("SELECT account, channel_id, thread_id, message_id FROM seat_lines WHERE job = ?1", params![j], |r| Ok(Latest { account: r.get(0)?, channel: r.get(1)?, thread: r.get(2)?, message_id: r.get(3)? })).optional().ok().flatten())
            .await
    }

    pub async fn outbox_push(&self, kind: &str, body: &Value, at: f64) -> i64 {
        let (k, b) = (kind.to_string(), body.to_string());
        self.run(move |c| c.execute("INSERT INTO outbox (kind, body, at) VALUES (?1, ?2, ?3)", params![k, b, at]).map(|_| c.last_insert_rowid()).unwrap_or(0)).await
    }

    pub async fn outbox_first(&self) -> Option<Outgoing> {
        self.run(|c| {
            c.query_row("SELECT id, kind, body, at FROM outbox ORDER BY id LIMIT 1", [], |r| {
                let body: String = r.get(2)?;
                Ok(Outgoing { id: r.get(0)?, kind: r.get(1)?, body: serde_json::from_str(&body).unwrap_or(Value::Null), at: r.get(3)? })
            })
            .optional()
            .ok()
            .flatten()
        })
        .await
    }

    pub async fn outbox_delete(&self, id: i64) {
        let _ = self.run(move |c| c.execute("DELETE FROM outbox WHERE id = ?1", params![id])).await;
    }

    pub async fn outbox_len(&self) -> i64 {
        self.run(|c| c.query_row("SELECT count(*) FROM outbox", [], |r| r.get(0)).unwrap_or(0)).await
    }

    pub async fn meta(&self, key: &str) -> Option<String> {
        let k = key.to_string();
        self.run(move |c| c.query_row("SELECT value FROM meta WHERE key = ?1", params![k], |r| r.get(0)).optional().ok().flatten()).await
    }

    pub async fn set_meta(&self, key: &str, value: &str) {
        let (k, v) = (key.to_string(), value.to_string());
        let _ = self.run(move |c| c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?1, ?2)", params![k, v])).await;
    }
}
