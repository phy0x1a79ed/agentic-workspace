//! The seat tables of `social.db`: the registry, the durable queue, the routed ids, the latest
//! line per agent, and the reads a forward grants.

use rusqlite::{params, Connection, OptionalExtension, Row};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::db::Db;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum State {
    #[default]
    Empty,
    Starting,
    Occupied,
    Retiring,
}

impl State {
    pub fn as_str(self) -> &'static str {
        match self {
            State::Empty => "empty",
            State::Starting => "starting",
            State::Occupied => "occupied",
            State::Retiring => "retiring",
        }
    }
    fn parse(s: &str) -> Self {
        match s {
            "starting" => State::Starting,
            "occupied" => State::Occupied,
            "retiring" => State::Retiring,
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

impl Breaker {
    /// Count one failed start. True when it trips.
    pub fn fail(&mut self, now: f64) -> bool {
        self.attempts.retain(|a| now - a < DECAY_S);
        self.attempts.push(now);
        let n = self.attempts.len();
        if n >= BUDGET {
            self.hold_until = now + HOLD_S;
            self.next_try = self.hold_until;
            self.attempts.clear();
            return true;
        }
        self.next_try = now + BACKOFF_S[n - 1];
        false
    }

    pub fn allows(&self, now: f64) -> bool {
        now >= self.next_try && now >= self.hold_until
    }

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
}

/// One `seat_registry` row.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Seat {
    pub account: String,
    pub channel_id: String,
    pub channel_name: String,
    pub kind: String,
    pub server: String,
    pub slug: String,
    pub node: String,
    pub scope: String,
    pub level: String,
    pub state: State,
    pub job: String,
    pub job_node: String,
    pub adopted: bool,
    pub since: f64,
    pub last_message: f64,
    pub done_at: f64,
    /// Starting: when the agent must be up. Retiring: when the next agent starts.
    pub deadline: f64,
    pub previous: Previous,
    pub breaker: Breaker,
    /// Why the seat holds its lines: `outside:<sender>`, `scope`, or `admitted`.
    pub held: String,
    pub replied: bool,
    /// Messages that arrived while the seats were paused, for the summary at unpause.
    pub missed: i64,
    /// The agent's session name, `seat:<app>/<server>/<chat>`.
    pub name: String,
}

const COLS: &str = "account, channel_id, channel_name, kind, server, slug, node, scope, level, state, job, job_node, adopted, since, last_message, done_at, deadline, previous, breaker, held, replied, missed, name";

impl Seat {
    fn from_row(r: &Row) -> rusqlite::Result<Self> {
        let previous: String = r.get(17)?;
        let breaker: String = r.get(18)?;
        Ok(Self {
            account: r.get(0)?,
            channel_id: r.get(1)?,
            channel_name: r.get(2)?,
            kind: r.get(3)?,
            server: r.get(4)?,
            slug: r.get(5)?,
            node: r.get(6)?,
            scope: r.get(7)?,
            level: r.get(8)?,
            state: State::parse(&r.get::<_, String>(9)?),
            job: r.get(10)?,
            job_node: r.get(11)?,
            adopted: r.get::<_, i64>(12)? != 0,
            since: r.get(13)?,
            last_message: r.get(14)?,
            done_at: r.get(15)?,
            deadline: r.get(16)?,
            previous: serde_json::from_str(&previous).unwrap_or_default(),
            breaker: serde_json::from_str(&breaker).unwrap_or_default(),
            held: r.get(19)?,
            replied: r.get::<_, i64>(20)? != 0,
            missed: r.get(21)?,
            name: r.get(22)?,
        })
    }

    pub fn key(&self) -> (String, String) {
        (self.account.clone(), self.channel_id.clone())
    }

    /// What `seats` shows of the seat.
    pub fn public(&self, queued: i64) -> Value {
        json!({
            "state": self.state.as_str(), "job": if self.job.is_empty() { Value::Null } else { json!(self.job) },
            "node": self.node, "level": self.level, "scope": self.scope, "adopted": self.adopted,
            "queued": queued, "done": self.done_at > 0.0, "held": if self.held.is_empty() || self.held == "admitted" { Value::Null } else { json!(self.held) },
            "breaker": if self.breaker.hold_until > 0.0 { json!({"hold_until": self.breaker.hold_until}) } else { Value::Null },
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

fn save(c: &Connection, s: &Seat) -> rusqlite::Result<()> {
    let prev = serde_json::to_string(&s.previous).unwrap_or_default();
    c.execute(
        &format!("INSERT INTO seat_registry ({COLS}) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23) \
         ON CONFLICT (account, channel_id) DO UPDATE SET channel_name = excluded.channel_name, kind = excluded.kind, server = excluded.server, slug = excluded.slug, \
         node = excluded.node, scope = excluded.scope, level = excluded.level, state = excluded.state, job = excluded.job, job_node = excluded.job_node, \
         adopted = excluded.adopted, since = excluded.since, last_message = excluded.last_message, done_at = excluded.done_at, deadline = excluded.deadline, \
         previous = excluded.previous, breaker = excluded.breaker, held = excluded.held, replied = excluded.replied, missed = excluded.missed, name = excluded.name"),
        params![
            s.account, s.channel_id, s.channel_name, s.kind, s.server, s.slug, s.node, s.scope, s.level, s.state.as_str(), s.job, s.job_node, s.adopted as i64,
            s.since, s.last_message, s.done_at, s.deadline, prev, serde_json::to_string(&s.breaker).unwrap_or_default(), s.held, s.replied as i64, s.missed, s.name
        ],
    )?;
    Ok(())
}

/// The seat tables, over the plugin's `Db`.
#[derive(Clone)]
pub struct Store(pub Db);

impl Store {
    pub async fn get(&self, account: &str, channel: &str) -> Option<Seat> {
        let (a, ch) = (account.to_string(), channel.to_string());
        self.0.run(move |c| c.query_row(&format!("SELECT {COLS} FROM seat_registry WHERE account = ?1 AND channel_id = ?2"), params![a, ch], Seat::from_row).optional().ok().flatten()).await
    }

    pub async fn all(&self) -> Vec<Seat> {
        self.0.run(|c| c.prepare(&format!("SELECT {COLS} FROM seat_registry ORDER BY account, channel_id")).and_then(|mut s| s.query_map([], Seat::from_row)?.collect()).unwrap_or_default()).await
    }

    pub async fn save(&self, s: &Seat) {
        let s = s.clone();
        if let Err(e) = self.0.run(move |c| save(c, &s)).await {
            tracing::warn!("seat save failed: {e}");
        }
    }

    /// The seat `(node, job)` is the agent of, if any.
    pub async fn of_job(&self, node: &str, job: &str) -> Vec<Seat> {
        let (n, j) = (node.to_string(), job.to_string());
        self.0.run(move |c| c.prepare(&format!("SELECT {COLS} FROM seat_registry WHERE job_node = ?1 AND job = ?2")).and_then(|mut s| s.query_map(params![n, j], Seat::from_row)?.collect()).unwrap_or_default()).await
    }

    /// Record a message as routed. False when it was routed before, so it goes nowhere again.
    pub async fn routed(&self, account: &str, channel: &str, message_id: &str, at: f64) -> bool {
        if message_id.is_empty() {
            return true;
        }
        let (a, ch, id) = (account.to_string(), channel.to_string(), message_id.to_string());
        self.0.run(move |c| c.execute("INSERT OR IGNORE INTO seat_routed (account, channel_id, message_id, routed_at) VALUES (?1, ?2, ?3, ?4)", params![a, ch, id, at]).map(|n| n > 0).unwrap_or(false)).await
    }

    pub async fn enqueue(&self, account: &str, channel: &str, message_id: &str, thread_id: &str, line: &str, at: f64) {
        let (a, ch, id, t, l) = (account.to_string(), channel.to_string(), message_id.to_string(), thread_id.to_string(), line.to_string());
        let _ = self
            .0
            .run(move |c| c.execute("INSERT INTO seat_queue (account, channel_id, message_id, thread_id, line, queued_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)", params![a, ch, id, t, l, at]))
            .await;
    }

    /// The undelivered lines of a seat, oldest first.
    pub async fn pending(&self, account: &str, channel: &str) -> Vec<Queued> {
        let (a, ch) = (account.to_string(), channel.to_string());
        self.0
            .run(move |c| {
                c.prepare("SELECT id, message_id, thread_id, line, briefed_to FROM seat_queue WHERE account = ?1 AND channel_id = ?2 AND delivered_at = 0 ORDER BY id")
                    .and_then(|mut s| s.query_map(params![a, ch], |r| Ok(Queued { id: r.get(0)?, message_id: r.get(1)?, thread_id: r.get(2)?, line: r.get(3)?, briefed_to: r.get(4)? }))?.collect())
                    .unwrap_or_default()
            })
            .await
    }

    pub async fn brief_to(&self, ids: Vec<i64>, job: &str) {
        let j = job.to_string();
        let _ = self
            .0
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
        self.0.run(move |c| c.execute("UPDATE seat_queue SET delivered_at = ?1 WHERE account = ?2 AND channel_id = ?3 AND briefed_to = ?4 AND delivered_at = 0", params![at, a, ch, j]).unwrap_or(0)).await
    }

    /// Put lines briefed to a job that never came up back in the queue.
    pub async fn unbrief(&self, account: &str, channel: &str) {
        let (a, ch) = (account.to_string(), channel.to_string());
        let _ = self.0.run(move |c| c.execute("UPDATE seat_queue SET briefed_to = '' WHERE account = ?1 AND channel_id = ?2 AND delivered_at = 0", params![a, ch])).await;
    }

    /// The line a queued message became, for a forward.
    pub async fn line_of(&self, account: &str, channel: &str, message_id: &str) -> Option<String> {
        let (a, ch, id) = (account.to_string(), channel.to_string(), message_id.to_string());
        self.0
            .run(move |c| c.query_row("SELECT line FROM seat_queue WHERE account = ?1 AND channel_id = ?2 AND message_id = ?3 ORDER BY id DESC LIMIT 1", params![a, ch, id], |r| r.get(0)).optional().ok().flatten())
            .await
    }

    pub async fn delivered(&self, id: i64, at: f64) {
        let _ = self.0.run(move |c| c.execute("UPDATE seat_queue SET delivered_at = ?1 WHERE id = ?2", params![at, id])).await;
    }

    /// The latest line pushed to `(node, job)`: what `send` and `react` default to.
    pub async fn set_line(&self, node: &str, job: &str, account: &str, channel: &str, thread: &str, message_id: &str, at: f64) {
        let v = [node, job, account, channel, thread, message_id].map(str::to_string);
        let _ = self
            .0
            .run(move |c| {
                c.execute(
                    "INSERT INTO seat_lines (node, job, account, channel_id, thread_id, message_id, at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7) \
                     ON CONFLICT (node, job) DO UPDATE SET account = excluded.account, channel_id = excluded.channel_id, thread_id = excluded.thread_id, \
                     message_id = excluded.message_id, at = excluded.at",
                    params![v[0], v[1], v[2], v[3], v[4], v[5], at],
                )
            })
            .await;
    }

    /// `(account, channel, thread, message_id)` of the latest line `(node, job)` got.
    pub async fn line(&self, node: &str, job: &str) -> Option<(String, String, String, String)> {
        let (n, j) = (node.to_string(), job.to_string());
        self.0
            .run(move |c| c.query_row("SELECT account, channel_id, thread_id, message_id FROM seat_lines WHERE node = ?1 AND job = ?2", params![n, j], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).optional().ok().flatten())
            .await
    }

    pub async fn grant_read(&self, seat: (&str, &str), source: (&str, &str), until: f64) {
        let v = [seat.0, seat.1, source.0, source.1].map(str::to_string);
        let _ = self
            .0
            .run(move |c| {
                c.execute(
                    "INSERT INTO seat_reads (seat_account, seat_channel, account, channel_id, until) VALUES (?1, ?2, ?3, ?4, ?5) \
                     ON CONFLICT (seat_account, seat_channel, account, channel_id) DO UPDATE SET until = excluded.until",
                    params![v[0], v[1], v[2], v[3], until],
                )
            })
            .await;
    }

    /// The chats of `account` that the seats `(seat_account, seat_channel)` may read now.
    pub async fn reads(&self, seats: Vec<(String, String)>, account: &str, now: f64) -> Vec<String> {
        let a = account.to_string();
        self.0
            .run(move |c| {
                let mut out = vec![];
                for (sa, sc) in seats {
                    let got: Vec<String> = c
                        .prepare("SELECT channel_id FROM seat_reads WHERE seat_account = ?1 AND seat_channel = ?2 AND account = ?3 AND until > ?4")
                        .and_then(|mut s| s.query_map(params![sa, sc, a, now], |r| r.get(0))?.collect())
                        .unwrap_or_default();
                    out.extend(got);
                }
                out
            })
            .await
    }
}
