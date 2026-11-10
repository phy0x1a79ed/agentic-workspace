//! The seat tables of `social.db`: the chats that are seats, the queue each host pulls, the
//! routed ids, the latest line per agent, and the reads a forward grants.

use rusqlite::{params, Connection, OptionalExtension, Row as SqlRow};
use serde_json::{json, Value};

use crate::db::Db;

/// One `seat_registry` row: what social declares, and what the seat's host last reported.
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
    /// The agent's session name, `seat:<app>/<server>/<chat>`.
    pub name: String,
    pub state: String,
    pub session_id: String,
    pub remote_control: bool,
    pub rc_url: String,
    /// The host's own view of the seat, as `seats` shows it.
    pub report: Value,
}

const COLS: &str = "account, channel_id, channel_name, kind, server, slug, node, scope, level, name, state, session_id, remote_control, rc_url, report";

impl Seat {
    fn from_row(r: &SqlRow) -> rusqlite::Result<Self> {
        let report: String = r.get(14)?;
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
            name: r.get(9)?,
            state: r.get(10)?,
            session_id: r.get(11)?,
            remote_control: r.get::<_, i64>(12)? != 0,
            rc_url: r.get(13)?,
            report: serde_json::from_str(&report).unwrap_or(Value::Null),
        })
    }

    /// The seat's id outside social: `<account>/<channel_id>`, which holds no other `/`.
    pub fn id(&self) -> String {
        format!("{}/{}", self.account, self.channel_id)
    }

    /// What `seats` shows: the host's last report, or the row itself before its first one.
    pub fn public(&self) -> Value {
        if self.report.as_object().is_some_and(|o| !o.is_empty()) {
            return self.report.clone();
        }
        json!({"state": self.state, "session_id": if self.session_id.is_empty() { Value::Null } else { json!(self.session_id) }, "node": self.node, "level": self.level, "scope": self.scope,
            "adopted": false, "queued": 0, "held": Value::Null, "breaker": Value::Null, "id": self.id(), "remote_control": self.remote_control, "rc_url": self.rc_url})
    }
}

/// One `seat_queue` row: something waiting for a host's pull.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Row {
    pub id: i64,
    pub node: String,
    pub kind: String,
    pub account: String,
    pub channel_id: String,
    pub message_id: String,
    pub thread_id: String,
    pub line: String,
    pub body: Value,
}

fn save(c: &Connection, s: &Seat) -> rusqlite::Result<usize> {
    c.execute(
        &format!(
            "INSERT INTO seat_registry ({COLS}) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15) \
             ON CONFLICT (account, channel_id) DO UPDATE SET channel_name = excluded.channel_name, kind = excluded.kind, server = excluded.server, slug = excluded.slug, \
             node = excluded.node, scope = excluded.scope, level = excluded.level, name = excluded.name"
        ),
        params![s.account, s.channel_id, s.channel_name, s.kind, s.server, s.slug, s.node, s.scope, s.level, s.name, s.state, s.session_id, s.remote_control as i64, s.rc_url, s.report.to_string()],
    )
}

/// The seat tables, over the plugin's `Db`.
#[derive(Clone)]
pub struct Store(pub Db);

impl Store {
    pub async fn get(&self, account: &str, channel: &str) -> Option<Seat> {
        let (a, ch) = (account.to_string(), channel.to_string());
        self.0.run(move |c| c.query_row(&format!("SELECT {COLS} FROM seat_registry WHERE account = ?1 AND channel_id = ?2"), params![a, ch], Seat::from_row).optional().ok().flatten()).await
    }

    /// Write what social declares. The host's report on the row is left alone.
    pub async fn save(&self, s: &Seat) {
        let s = s.clone();
        if let Err(e) = self.0.run(move |c| save(c, &s)).await {
            tracing::warn!("seat save failed: {e}");
        }
    }

    /// Store what a host reported. True when a field its `social/seats` event carries changed.
    pub async fn mirror(&self, account: &str, channel: &str, state: &str, session: &str, remote_control: bool, rc_url: &str, report: Value) -> bool {
        let v = [account, channel, state, session, rc_url].map(str::to_string);
        self.0
            .run(move |c| -> bool {
                let before: Option<(String, String, i64, String)> = c.query_row("SELECT state, session_id, remote_control, rc_url FROM seat_registry WHERE account = ?1 AND channel_id = ?2", params![v[0], v[1]], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).optional().ok().flatten();
                let Some(before) = before else { return false };
                let _ = c.execute("UPDATE seat_registry SET state = ?1, session_id = ?2, remote_control = ?3, rc_url = ?4, report = ?5 WHERE account = ?6 AND channel_id = ?7", params![v[2], v[3], remote_control as i64, v[4], report.to_string(), v[0], v[1]]);
                before != (v[2].clone(), v[3].clone(), remote_control as i64, v[4].clone())
            })
            .await
    }

    /// Record a message as routed. False when it was routed before, so it goes nowhere again.
    pub async fn routed(&self, account: &str, channel: &str, message_id: &str, at: f64) -> bool {
        if message_id.is_empty() {
            return true;
        }
        let (a, ch, id) = (account.to_string(), channel.to_string(), message_id.to_string());
        self.0.run(move |c| c.execute("INSERT OR IGNORE INTO seat_routed (account, channel_id, message_id, routed_at) VALUES (?1, ?2, ?3, ?4)", params![a, ch, id, at]).map(|n| n > 0).unwrap_or(false)).await
    }

    pub async fn enqueue(&self, row: Row, at: f64) {
        let body = if row.body.is_null() { "{}".to_string() } else { row.body.to_string() };
        let _ = self
            .0
            .run(move |c| c.execute("INSERT INTO seat_queue (node, kind, account, channel_id, message_id, thread_id, line, body, queued_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)", params![row.node, row.kind, row.account, row.channel_id, row.message_id, row.thread_id, row.line, body, at]))
            .await;
    }

    /// The rows `node` has not acked, oldest first.
    pub async fn waiting(&self, node: &str, limit: i64) -> Vec<Row> {
        let n = node.to_string();
        self.0
            .run(move |c| {
                c.prepare("SELECT id, node, kind, account, channel_id, message_id, thread_id, line, body FROM seat_queue WHERE node = ?1 AND delivered_at = 0 ORDER BY id LIMIT ?2")
                    .and_then(|mut s| {
                        s.query_map(params![n, limit], |r| {
                            let body: String = r.get(8)?;
                            Ok(Row { id: r.get(0)?, node: r.get(1)?, kind: r.get(2)?, account: r.get(3)?, channel_id: r.get(4)?, message_id: r.get(5)?, thread_id: r.get(6)?, line: r.get(7)?, body: serde_json::from_str(&body).unwrap_or(Value::Null) })
                        })?
                        .collect()
                    })
                    .unwrap_or_default()
            })
            .await
    }

    /// `node` holds every row through `through`.
    pub async fn ack(&self, node: &str, through: i64, at: f64) {
        let n = node.to_string();
        let _ = self.0.run(move |c| c.execute("UPDATE seat_queue SET delivered_at = ?1 WHERE node = ?2 AND delivered_at = 0 AND id <= ?3", params![at, n, through])).await;
    }

    /// Whether this outbox row of `node` is new: a replay of one already applied is not.
    pub async fn fresh(&self, node: &str, epoch: &str, id: i64) -> bool {
        let (n, e) = (node.to_string(), epoch.to_string());
        self.0
            .run(move |c| c.query_row("SELECT epoch, mark FROM seat_hosts WHERE node = ?1", params![n], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))).optional().ok().flatten().is_none_or(|(held, mark)| held != e || id > mark))
            .await
    }

    pub async fn applied(&self, node: &str, epoch: &str, id: i64) {
        let (n, e) = (node.to_string(), epoch.to_string());
        let _ = self.0.run(move |c| c.execute("INSERT OR REPLACE INTO seat_hosts (node, epoch, mark) VALUES (?1, ?2, ?3)", params![n, e, id])).await;
    }

    /// The line a queued message became, for a forward.
    pub async fn line_of(&self, account: &str, channel: &str, message_id: &str) -> Option<String> {
        let (a, ch, id) = (account.to_string(), channel.to_string(), message_id.to_string());
        self.0
            .run(move |c| c.query_row("SELECT line FROM seat_queue WHERE account = ?1 AND channel_id = ?2 AND message_id = ?3 ORDER BY id DESC LIMIT 1", params![a, ch, id], |r| r.get(0)).optional().ok().flatten())
            .await
    }

    /// The latest line a host pushed to `(node, session)`: what `send` and `react` default to.
    #[allow(clippy::too_many_arguments)]
    pub async fn set_line(&self, node: &str, session: &str, account: &str, channel: &str, thread: &str, message_id: &str, at: f64) {
        let v = [node, session, account, channel, thread, message_id].map(str::to_string);
        let _ = self
            .0
            .run(move |c| {
                c.execute(
                    "INSERT INTO seat_lines (node, session_id, account, channel_id, thread_id, message_id, at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7) \
                     ON CONFLICT (node, session_id) DO UPDATE SET account = excluded.account, channel_id = excluded.channel_id, thread_id = excluded.thread_id, \
                     message_id = excluded.message_id, at = excluded.at",
                    params![v[0], v[1], v[2], v[3], v[4], v[5], at],
                )
            })
            .await;
    }

    /// `(account, channel, thread, message_id)` of the latest line `(node, session)` got.
    pub async fn line(&self, node: &str, session: &str) -> Option<(String, String, String, String)> {
        let (n, j) = (node.to_string(), session.to_string());
        self.0
            .run(move |c| c.query_row("SELECT account, channel_id, thread_id, message_id FROM seat_lines WHERE node = ?1 AND session_id = ?2", params![n, j], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).optional().ok().flatten())
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
