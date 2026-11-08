//! The host half of every channel, on the public host: the rooms, who holds them, what waits for
//! each, and every machine slot.
//!
//! A member node's `channel` calls this through the public host's door, naming the member; the
//! door stamps the node (fleet.md R98). Nothing here calls out: no push, no door, no edge (R79).
//! A member's node holds a `feed` long-poll instead, which returns what arrived after each
//! member's cursor and renews each member's lease. A member whose node stops renewing it
//! lapses, and a room with no member left ends (channels.md R72 item 5).
//!
//! Messages wait in the store, so a restart loses none that a member has not fetched. An
//! encrypted room keeps only sealed text and the key exchange, so it has one member (R73).

pub mod rooms;
pub mod store;

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use awm_plugin_sdk::{Backoff, VerbError};
use serde_json::{json, Value};

use crate::channel::text;
use rooms::{Rooms, RoomsError};
use store::{Member, Room, Store, GUEST, MEMBER, NOTICE};

/// The longest one feed waits for something to arrive.
const FEED_MAX_S: f64 = 60.0;
pub const PRUNE: Duration = Duration::from_secs(30);
/// How long a closed room's messages are kept for members still to fetch them.
const KEEP_CLOSED_S: f64 = 7.0 * 24.0 * 3600.0;
/// The keys of a message a member may send: plaintext, a sealed message, or a key exchange.
const BODY_KEYS: [&str; 4] = ["text", "sealed", "n", "spake2"];
const LASTS: [&str; 2] = ["task", "persistent"];

/// rooms.sealed: no encryption, an offer is out, our side has sent sealed text.
const PLAIN: i64 = 0;
const OFFERED: i64 = 1;
const SEALED: i64 = 2;

/// A refusal, under the class the Python plugin raised.
pub fn refuse(msg: impl Into<String>) -> VerbError {
    VerbError::new("RelayRefused", msg)
}

type Res<T> = Result<T, VerbError>;

fn refused(e: RoomsError) -> VerbError {
    match e {
        RoomsError::Gone(m) | RoomsError::Refused(m) | RoomsError::Unreachable(m) => refuse(m),
    }
}

pub struct Relay {
    pub store: Store,
    pub rooms: Rooms,
    lease_s: f64,
    following: Mutex<HashMap<String, tokio::task::JoinHandle<()>>>,
    changed: tokio::sync::watch::Sender<u64>,
    stopping: AtomicBool,
}

impl Relay {
    pub fn new(store: Store, rooms: Rooms, lease_s: f64) -> Arc<Self> {
        Arc::new(Self { store, rooms, lease_s, following: Default::default(), changed: tokio::sync::watch::channel(0).0, stopping: AtomicBool::new(false) })
    }

    /// `AWM_RELAY_LEASE_S`, else 180 s: how long a member stays without its node renewing it.
    pub fn lease_from_env() -> f64 {
        std::env::var("AWM_RELAY_LEASE_S").ok().and_then(|v| v.trim().parse().ok()).filter(|v: &f64| *v > 0.0).unwrap_or(180.0)
    }

    // -- verbs ---------------------------------------------------------------

    pub async fn open(self: &Arc<Self>, m: &Member, persistent: bool) -> Res<Value> {
        let lasts = LASTS[persistent as usize];
        let (name, token) = self.rooms.open(lasts).await.map_err(refused)?;
        let row = self.store.add_room(&name, lasts, &self.rooms.link(&name), &token).map_err(|e| VerbError::runtime(e.to_string()))?;
        self.store.add_member(&name, m);
        self.follow(&name);
        Ok(public(&row))
    }

    /// A machine slot for `node`'s operator daemon, or its held slot put back.
    pub async fn issue(&self, node: &str, restore: Option<Value>) -> Res<Value> {
        let issued = self.rooms.issue(restore.as_ref()).await.map_err(refused)?;
        match &restore {
            None => tracing::info!("issued slot {} to {node}", issued["slot"]),
            Some(r) => tracing::info!("slot {} for {node}: {}", r["slot"], if issued["restored"] == true { "restored".to_string() } else { issued["refused"].to_string() }),
        }
        Ok(issued)
    }

    pub async fn join(&self, m: &Member, name: &str) -> Res<Value> {
        let row = self.open_room(name)?;
        if row.sealed != PLAIN {
            return Err(refuse(format!("room {name} is encrypted, and an encrypted room has one member: its key never leaves that member's node")));
        }
        self.store.add_member(name, m);
        Ok(public(&row))
    }

    pub async fn write(&self, m: &Member, name: &str, message: &Value) -> Res<Value> {
        let row = self.open_room(name)?;
        if !self.store.is_member(name, &m.node, &m.session) {
            return Err(refuse(format!("you are not a member of room {name}; `join` it first")));
        }
        let mut body = serde_json::Map::new();
        for k in BODY_KEYS {
            if let Some(v) = message.get(k) {
                body.insert(k.into(), v.clone());
            }
        }
        if body.is_empty() {
            return Err(refuse("a message carries `text`, `sealed` with `n`, or `spake2`"));
        }
        if body.contains_key("spake2") {
            if self.store.members(name).len() > 1 {
                return Err(refuse(format!("room {name} has more than one member, so it cannot be encrypted: its key would have to pass through this relay")));
            }
            self.store.seal(name, OFFERED);
        }
        if body.contains_key("sealed") {
            self.store.seal(name, SEALED);
        } else if body.contains_key("text") && row.sealed == SEALED {
            return Err(refuse(format!("room {name} is encrypted, so plaintext is refused")));
        }
        self.store.renew(&m.node, Some(std::slice::from_ref(&m.session)));
        let mut wire = serde_json::Map::new();
        wire.insert("from".into(), json!(m.who));
        wire.extend(body.clone());
        let seq = match self.rooms.send(name, &row.token, &Value::Object(wire)).await {
            Ok(s) => s,
            Err(RoomsError::Gone(_)) => {
                self.ended(name, "the relay no longer has this room");
                return Err(refuse(format!("room {name} has ended; open a new one")));
            }
            Err(RoomsError::Unreachable(e)) => return Err(refuse(format!("the tether relay is unreachable, nothing was sent: {e}"))),
            Err(RoomsError::Refused(e)) => return Err(refuse(e)),
        };
        self.store.add_message(name, MEMBER, &Value::Object(body), Some(seq), Some(m), &m.who);
        self.wake();
        Ok(json!({"name": name, "sent": seq}))
    }

    pub async fn close(&self, m: &Member, name: &str) -> Res<Value> {
        if !self.store.leave(name, &m.node, &m.session, "it left") {
            return Err(refuse(format!("you are not a member of room {name}")));
        }
        if self.store.members(name).is_empty() {
            self.shut(name, "its last member left").await;
        }
        Ok(json!({"closed": name}))
    }

    pub fn status(&self, m: &Member) -> Value {
        let rooms: Vec<Value> = self
            .store
            .of_session(&m.node, &m.session)
            .into_iter()
            .map(|(room, mine)| {
                let mut item = public(&room);
                item["member_left"] = if mine.left_at.is_some() { json!(mine.left_why) } else { Value::Null };
                item["members"] = json!(self.store.members(&room.name).len());
                item
            })
            .collect();
        json!({"rooms": rooms})
    }

    /// What waits for `node`'s members in `live`, after their cursors. `acks` move cursors past
    /// what the node has typed. `live` renews those members' leases, and None renews all of the
    /// node's members, for a node that cannot tell which of its sessions run.
    pub async fn feed(&self, node: &str, live: Option<Vec<String>>, acks: &[Value], wait: f64) -> Value {
        for ack in acks {
            let through = ack.get("through").and_then(Value::as_i64).unwrap_or(0);
            self.store.ack(&text(ack, "room"), node, &text(ack, "session"), through);
        }
        let deadline = tokio::time::Instant::now() + Duration::from_secs_f64(wait.clamp(0.0, FEED_MAX_S));
        loop {
            let mut changed = self.changed.subscribe();
            self.store.renew(node, live.as_deref());
            let (messages, members) = self.waiting(node, live.as_deref());
            if !messages.is_empty() || tokio::time::Instant::now() >= deadline || self.stopping.load(Ordering::SeqCst) {
                return json!({"messages": messages, "members": members});
            }
            let _ = tokio::time::timeout_at(deadline, changed.changed()).await;
        }
    }

    // -- lifecycle -----------------------------------------------------------

    /// Follow every open room again, from its saved cursor.
    pub fn resume(self: &Arc<Self>) {
        for row in self.store.open_rooms() {
            self.follow(&row.name);
        }
    }

    /// Drop every member whose lease lapsed, and end each room left with none.
    pub async fn prune(&self) -> Vec<String> {
        let mut gone = vec![];
        for m in self.store.lapsed(crate::db::now() - self.lease_s) {
            self.store.leave(&m.room, &m.node, &m.session, "its session ended");
            tracing::info!("room {}: member {}@{} lapsed", m.room, m.session, m.node);
            gone.push(format!("{}@{}", m.session, m.node));
            if self.store.room(&m.room).is_some_and(|r| r.open) && self.store.members(&m.room).is_empty() {
                self.shut(&m.room, "its last member's session ended").await;
            }
        }
        self.store.forget(crate::db::now() - KEEP_CLOSED_S);
        gone
    }

    pub async fn prune_forever(self: Arc<Self>) {
        loop {
            tokio::time::sleep(PRUNE).await;
            self.prune().await;
        }
    }

    pub fn stop(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        self.wake();
        for (_, task) in self.following.lock().unwrap_or_else(|p| p.into_inner()).drain() {
            task.abort();
        }
    }

    // -- internals -----------------------------------------------------------

    fn open_room(&self, name: &str) -> Res<Room> {
        let row = self.store.room(name).ok_or_else(|| refuse(format!("no room is named {name}")))?;
        if !row.open {
            return Err(refuse(format!("room {name} has ended: {}", row.ended.as_deref().unwrap_or("None"))));
        }
        Ok(row)
    }

    fn waiting(&self, node: &str, live: Option<&[String]>) -> (Vec<Value>, Vec<Value>) {
        let alive: Option<HashSet<&str>> = live.map(|l| l.iter().map(String::as_str).collect());
        let (mut messages, mut members) = (vec![], vec![]);
        for m in self.store.of_node(node) {
            if alive.as_ref().is_some_and(|a| !a.contains(m.session.as_str())) {
                continue;
            }
            let pending = self.store.pending(&m);
            if pending.is_empty() && self.store.room(&m.room).is_some_and(|r| !r.open) {
                self.store.leave(&m.room, node, &m.session, "the room ended");
                continue;
            }
            members.push(json!({"room": m.room, "session": m.session}));
            for msg in pending {
                messages.push(json!({"id": msg.id, "room": msg.room, "session": m.session, "seq": msg.seq, "by": msg.by, "who": msg.who, "level": msg.level, "body": msg.body}));
            }
        }
        (messages, members)
    }

    fn wake(&self) {
        self.changed.send_modify(|n| *n += 1);
    }

    fn follow(self: &Arc<Self>, name: &str) {
        let mut following = self.following.lock().unwrap_or_else(|p| p.into_inner());
        if following.get(name).is_some_and(|t| !t.is_finished()) {
            return;
        }
        let (me, n) = (self.clone(), name.to_string());
        following.insert(name.to_string(), tokio::spawn(async move { me.follow_room(&n).await }));
    }

    /// Keep what the guest sends. Our members' messages are kept as they write them.
    async fn follow_room(&self, name: &str) {
        let mut backoff = Backoff::doubling(1.0, 30.0);
        while !self.stopping.load(Ordering::SeqCst) {
            let Some(row) = self.store.room(name).filter(|r| r.open) else { break };
            let batch = match self.rooms.recv(name, &row.token, row.cursor).await {
                Ok(b) => b,
                Err(RoomsError::Gone(_)) => {
                    self.ended(name, "the relay no longer has this room");
                    break;
                }
                Err(e) => {
                    tracing::info!("room {name}: receive failed, retrying: {e:?}");
                    tokio::time::sleep(backoff.wait()).await;
                    continue;
                }
            };
            backoff.reset();
            for message in batch.get("messages").and_then(Value::as_array).into_iter().flatten() {
                if message.get("by").and_then(Value::as_str) == Some("host") {
                    continue;
                }
                let mut body = message.as_object().cloned().unwrap_or_default();
                body.retain(|k, _| !["seq", "by", "from"].contains(&k.as_str()));
                let who = message.get("from").and_then(Value::as_str).filter(|w| !w.is_empty()).unwrap_or("the guest");
                let seq = message.get("seq").and_then(Value::as_i64).unwrap_or(0);
                self.store.add_message(name, GUEST, &Value::Object(body), Some(seq), None, who);
            }
            self.store.set_cursor(name, batch.get("next").and_then(Value::as_i64).unwrap_or(0));
            self.wake();
            if let Some(why) = batch.get("ended").filter(|e| !e.is_null() && **e != json!(false) && **e != json!("")) {
                self.ended(name, why.as_str().map(str::to_string).unwrap_or_else(|| why.to_string()).as_str());
                break;
            }
        }
        self.following.lock().unwrap_or_else(|p| p.into_inner()).remove(name);
    }

    /// The room ended on the relay's side: tell every member.
    fn ended(&self, name: &str, why: &str) {
        if !self.store.close_room(name, why) {
            return;
        }
        self.store.add_message(name, NOTICE, &json!({"text": why, "ended": true}), None, None, "");
        self.wake();
    }

    /// End the room from our side.
    async fn shut(&self, name: &str, why: &str) {
        let Some(row) = self.store.room(name) else { return };
        if !self.store.close_room(name, why) {
            return;
        }
        if let Some(task) = self.following.lock().unwrap_or_else(|p| p.into_inner()).remove(name) {
            task.abort();
        }
        self.rooms.leave(name, &row.token).await;
        self.store.add_message(name, NOTICE, &json!({"text": why, "ended": true}), None, None, "");
        self.wake();
    }
}

fn public(row: &Room) -> Value {
    json!({"name": row.name, "lasts": row.lasts, "link": row.link, "open": row.open, "ended": row.ended, "encrypted": row.sealed == SEALED})
}

