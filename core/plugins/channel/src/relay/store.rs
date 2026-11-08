//! The rooms this relay carries, their members and their messages, in
//! `services/relay/channels.db`, read in place.
//!
//! The file outlives a restart, so a message no member has fetched waits here (channels.md R72
//! item 5). It holds each room's host token, so its directory is this user's alone. In an
//! encrypted room a message is only the sealed text or the key exchange, which the member's own
//! node opens (R73). A member is one session on one node, `(room, node, session)`: its `cursor`
//! is the last message id it acknowledged, and `lease_at` the last time its node renewed it.

use std::path::Path;
use std::sync::Mutex;

use rusqlite::{params, Connection, OptionalExtension, Row as SqlRow};
use serde_json::Value;

use crate::db;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS rooms (
    name      TEXT PRIMARY KEY,
    lasts     TEXT NOT NULL,
    link      TEXT NOT NULL,
    token     TEXT NOT NULL,
    cursor    INTEGER NOT NULL DEFAULT 0,
    sealed    INTEGER NOT NULL DEFAULT 0,
    open      INTEGER NOT NULL DEFAULT 1,
    ended     TEXT,
    opened_at REAL NOT NULL,
    closed_at REAL
);
CREATE TABLE IF NOT EXISTS members (
    room      TEXT NOT NULL,
    node      TEXT NOT NULL,
    session   TEXT NOT NULL,
    name      TEXT NOT NULL DEFAULT '',
    level     TEXT NOT NULL DEFAULT 'untrusted',
    cursor    INTEGER NOT NULL DEFAULT 0,
    lease_at  REAL NOT NULL,
    joined_at REAL NOT NULL,
    left_at   REAL,
    left_why  TEXT,
    PRIMARY KEY (room, node, session)
);
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    room      TEXT NOT NULL,
    seq       INTEGER,
    by        TEXT NOT NULL,
    node      TEXT NOT NULL DEFAULT '',
    session   TEXT NOT NULL DEFAULT '',
    who       TEXT NOT NULL DEFAULT '',
    level     TEXT NOT NULL DEFAULT 'untrusted',
    body      TEXT NOT NULL,
    at        REAL NOT NULL,
    UNIQUE (room, seq)
);";

/// Who wrote a message: the guest over the link, one of our members, or the relay.
pub const GUEST: &str = "guest";
pub const MEMBER: &str = "member";
pub const NOTICE: &str = "notice";

#[derive(Debug, Clone)]
pub struct Room {
    pub name: String,
    pub lasts: String,
    pub link: String,
    pub token: String,
    pub cursor: i64,
    pub sealed: i64,
    pub open: bool,
    pub ended: Option<String>,
}

impl Room {
    fn read(r: &SqlRow) -> rusqlite::Result<Self> {
        Ok(Self {
            name: r.get("name")?,
            lasts: r.get("lasts")?,
            link: r.get("link")?,
            token: r.get("token")?,
            cursor: r.get("cursor")?,
            sealed: r.get("sealed")?,
            open: r.get::<_, i64>("open")? != 0,
            ended: r.get("ended")?,
        })
    }
}

#[derive(Debug, Clone)]
pub struct Membership {
    pub room: String,
    pub node: String,
    pub session: String,
    pub cursor: i64,
    pub left_at: Option<f64>,
    pub left_why: Option<String>,
}

impl Membership {
    fn read(r: &SqlRow) -> rusqlite::Result<Self> {
        Ok(Self { room: r.get("room")?, node: r.get("node")?, session: r.get("session")?, cursor: r.get("cursor")?, left_at: r.get("left_at")?, left_why: r.get("left_why")? })
    }
}

/// A member as the calling node names it, with the node the door stamped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Member {
    pub node: String,
    pub session: String,
    pub name: String,
    pub level: String,
    pub who: String,
}

#[derive(Debug, Clone)]
pub struct Message {
    pub id: i64,
    pub room: String,
    pub seq: Option<i64>,
    pub by: String,
    pub who: String,
    pub level: String,
    pub body: Value,
}

pub struct Store(Mutex<Connection>);

impl Store {
    pub fn open(path: &Path) -> anyhow::Result<Self> {
        let conn = db::open(path)?;
        conn.execute_batch(SCHEMA)?;
        Ok(Self(Mutex::new(conn)))
    }

    fn db(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.0.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn all<T>(&self, sql: &str, args: impl rusqlite::Params, read: fn(&SqlRow) -> rusqlite::Result<T>) -> Vec<T> {
        let db = self.db();
        let Ok(mut stmt) = db.prepare(sql) else { return vec![] };
        stmt.query_map(args, read).map(|it| it.filter_map(Result::ok).collect()).unwrap_or_default()
    }

    // -- rooms ---------------------------------------------------------------

    pub fn add_room(&self, name: &str, lasts: &str, link: &str, token: &str) -> rusqlite::Result<Room> {
        self.db().execute("INSERT INTO rooms (name, lasts, link, token, opened_at) VALUES (?1, ?2, ?3, ?4, ?5)", params![name, lasts, link, token, db::now()])?;
        self.room(name).ok_or(rusqlite::Error::QueryReturnedNoRows)
    }

    pub fn room(&self, name: &str) -> Option<Room> {
        self.db().query_row("SELECT * FROM rooms WHERE name = ?1", [name], Room::read).optional().ok().flatten()
    }

    pub fn open_rooms(&self) -> Vec<Room> {
        self.all("SELECT * FROM rooms WHERE open = 1 ORDER BY opened_at", [], Room::read)
    }

    pub fn set_cursor(&self, name: &str, cursor: i64) {
        let _ = self.db().execute("UPDATE rooms SET cursor = ?1 WHERE name = ?2 AND cursor < ?1", params![cursor, name]);
    }

    /// Raise how far the room has moved to encryption. It never goes back.
    pub fn seal(&self, name: &str, level: i64) {
        let _ = self.db().execute("UPDATE rooms SET sealed = MAX(sealed, ?1) WHERE name = ?2", params![level, name]);
    }

    /// Mark a room closed and forget its host token. False when it was closed already.
    pub fn close_room(&self, name: &str, why: &str) -> bool {
        self.db()
            .execute("UPDATE rooms SET open = 0, ended = ?1, closed_at = ?2, token = '' WHERE name = ?3 AND open = 1", params![why, db::now(), name])
            .is_ok_and(|n| n == 1)
    }

    // -- members -------------------------------------------------------------

    /// Add a member, or bring one back. A new member's cursor starts at the room's last
    /// message, so it sees no history.
    pub fn add_member(&self, room: &str, m: &Member) {
        let now = db::now();
        let _ = self.db().execute(
            "INSERT INTO members (room, node, session, name, level, cursor, lease_at, joined_at)
             VALUES (?1, ?2, ?3, ?4, ?5, (SELECT COALESCE(MAX(id), 0) FROM messages WHERE room = ?1), ?6, ?6)
             ON CONFLICT (room, node, session) DO UPDATE SET left_at = NULL, left_why = NULL,
             lease_at = excluded.lease_at, name = excluded.name, level = excluded.level",
            params![room, m.node, m.session, m.name, m.level, now],
        );
    }

    pub fn is_member(&self, room: &str, node: &str, session: &str) -> bool {
        self.db()
            .query_row("SELECT 1 FROM members WHERE room = ?1 AND node = ?2 AND session = ?3 AND left_at IS NULL", [room, node, session], |_| Ok(()))
            .optional()
            .ok()
            .flatten()
            .is_some()
    }

    pub fn members(&self, room: &str) -> Vec<Membership> {
        self.all("SELECT * FROM members WHERE room = ?1 AND left_at IS NULL ORDER BY joined_at", [room], Membership::read)
    }

    /// Every present membership of `node`'s sessions.
    pub fn of_node(&self, node: &str) -> Vec<Membership> {
        self.all("SELECT m.* FROM members m JOIN rooms r ON r.name = m.room WHERE m.node = ?1 AND m.left_at IS NULL ORDER BY m.joined_at", [node], Membership::read)
    }

    /// The rooms a session is or was in, with how it left.
    pub fn of_session(&self, node: &str, session: &str) -> Vec<(Room, Membership)> {
        let db = self.db();
        let Ok(mut stmt) = db.prepare(
            "SELECT r.*, m.room, m.node, m.session, m.cursor AS member_cursor, m.left_at, m.left_why FROM members m
             JOIN rooms r ON r.name = m.room WHERE m.node = ?1 AND m.session = ?2 ORDER BY r.opened_at",
        ) else {
            return vec![];
        };
        let read = |r: &SqlRow| -> rusqlite::Result<(Room, Membership)> {
            let room = Room::read(r)?;
            let m = Membership { room: r.get("room")?, node: r.get("node")?, session: r.get("session")?, cursor: r.get("member_cursor")?, left_at: r.get("left_at")?, left_why: r.get("left_why")? };
            Ok((room, m))
        };
        stmt.query_map([node, session], read).map(|it| it.filter_map(Result::ok).collect()).unwrap_or_default()
    }

    /// Renew the lease of `node`'s members in `sessions`, or of all of them.
    pub fn renew(&self, node: &str, sessions: Option<&[String]>) {
        let now = db::now();
        let db = self.db();
        match sessions {
            None => {
                let _ = db.execute("UPDATE members SET lease_at = ?1 WHERE node = ?2 AND left_at IS NULL", params![now, node]);
            }
            Some(list) => {
                for s in list {
                    let _ = db.execute("UPDATE members SET lease_at = ?1 WHERE node = ?2 AND session = ?3 AND left_at IS NULL", params![now, node, s]);
                }
            }
        }
    }

    pub fn ack(&self, room: &str, node: &str, session: &str, through: i64) {
        let _ = self.db().execute("UPDATE members SET cursor = ?1 WHERE room = ?2 AND node = ?3 AND session = ?4 AND cursor < ?1", params![through, room, node, session]);
    }

    pub fn leave(&self, room: &str, node: &str, session: &str, why: &str) -> bool {
        self.db()
            .execute("UPDATE members SET left_at = ?1, left_why = ?2 WHERE room = ?3 AND node = ?4 AND session = ?5 AND left_at IS NULL", params![db::now(), why, room, node, session])
            .is_ok_and(|n| n == 1)
    }

    pub fn lapsed(&self, before: f64) -> Vec<Membership> {
        self.all("SELECT * FROM members WHERE left_at IS NULL AND lease_at < ?1", [before], Membership::read)
    }

    // -- messages ------------------------------------------------------------

    /// Keep one message. The relay's `seq` is unique per room, so a message carried twice is
    /// kept once.
    pub fn add_message(&self, room: &str, by: &str, body: &Value, seq: Option<i64>, m: Option<&Member>, who: &str) {
        let (node, session, level) = m.map_or(("", "", "untrusted"), |m| (m.node.as_str(), m.session.as_str(), m.level.as_str()));
        let _ = self.db().execute(
            "INSERT INTO messages (room, seq, by, node, session, who, level, body, at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
             ON CONFLICT (room, seq) DO NOTHING",
            params![room, seq, by, node, session, who, if level.is_empty() { "untrusted" } else { level }, body.to_string(), db::now()],
        );
    }

    /// The messages after `m`'s cursor that another side wrote.
    pub fn pending(&self, m: &Membership) -> Vec<Message> {
        let read = |r: &SqlRow| -> rusqlite::Result<Message> {
            let body: String = r.get("body")?;
            Ok(Message {
                id: r.get("id")?,
                room: r.get("room")?,
                seq: r.get("seq")?,
                by: r.get("by")?,
                who: r.get("who")?,
                level: r.get("level")?,
                body: serde_json::from_str(&body).unwrap_or(Value::Null),
            })
        };
        let db = self.db();
        let Ok(mut stmt) = db.prepare("SELECT * FROM messages WHERE room = ?1 AND id > ?2 AND NOT (by = ?3 AND node = ?4 AND session = ?5) ORDER BY id") else { return vec![] };
        stmt.query_map(params![m.room, m.cursor, MEMBER, m.node, m.session], read).map(|it| it.filter_map(Result::ok).collect()).unwrap_or_default()
    }

    /// Drop the messages of rooms that closed before `before`.
    pub fn forget(&self, before: f64) {
        let _ = self.db().execute("DELETE FROM messages WHERE room IN (SELECT name FROM rooms WHERE open = 0 AND closed_at < ?1)", [before]);
    }
}
