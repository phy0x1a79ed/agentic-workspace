//! What social knows of the seats. A seat runs on its host's `seats` plugin. Social owns which
//! node each seated chat belongs to, and a durable queue per host that the host pulls and acks.
//! alcor has no route to a seat host, so the host opens every connection.

pub mod line;
pub mod store;

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};

use crate::config::{AccountConfig, SeatsConfig};
use crate::connectors::{ChatKind, Inbound};
use crate::db::Db;
use line::Place;
use store::{Row, Seat, Store};

/// How long a forward lets the receiving seat read the source chat.
pub const READ_S: f64 = 24.0 * 3600.0;
/// The longest one pull is held.
const MAX_WAIT_S: f64 = 55.0;
/// The most rows one pull carries.
const PULL_ROWS: i64 = 100;

pub const NOTICE: &str = "social (an automatic notice, not a message from Tony): ";

/// What became of one inbound message.
#[derive(Debug, Clone, PartialEq)]
pub enum Routed {
    /// The chat is a seat, and its host's queue took the message.
    Seat,
    /// The chat is a seat and saw this message before.
    Duplicate,
    /// The chat is no seat: a hand mount or nobody takes it.
    NotSeat,
}

pub struct Seats {
    pub store: Store,
    pub cfg: SeatsConfig,
    accounts: Vec<AccountConfig>,
    hosts: HashSet<String>,
    arrived: tokio::sync::Notify,
}

fn kind_str(k: ChatKind) -> &'static str {
    match k {
        ChatKind::Dm => "dm",
        ChatKind::Group => "group",
        ChatKind::Channel => "channel",
    }
}

fn kind_of(s: &str) -> ChatKind {
    match s {
        "dm" => ChatKind::Dm,
        "group" => ChatKind::Group,
        _ => ChatKind::Channel,
    }
}

impl Seats {
    pub fn new(db: Db, cfg: SeatsConfig, accounts: Vec<AccountConfig>) -> Arc<Self> {
        let hosts = cfg.rules.iter().map(|r| r.node.clone()).chain(cfg.chats.iter().map(|c| c.node.clone())).collect();
        Arc::new(Self { store: Store(db), cfg, accounts, hosts, arrived: tokio::sync::Notify::new() })
    }

    /// Whether `node` hosts seats, so lines for it wait here for its pull.
    pub fn is_host(&self, node: &str) -> bool {
        self.hosts.contains(node)
    }

    fn platform(&self, account: &str) -> String {
        self.accounts.iter().find(|a| a.name == account).map(|a| a.platform.clone()).unwrap_or_default()
    }

    /// The seat's session name, worked out from the row, so a rename reaches every old row.
    pub fn name(&self, s: &Seat) -> String {
        self.place_of(s).session_name()
    }

    fn place_of(&self, s: &Seat) -> Place {
        Place { platform: self.platform(&s.account), server: s.server.clone(), kind: kind_of(&s.kind), chat_name: s.channel_name.clone(), person: s.slug.clone() }
    }

    /// What a host needs to know of a seat, with the labels only social can work out.
    fn decl(&self, s: &Seat) -> Value {
        let place = self.place_of(s);
        let (model, effort) = self.cfg.agent_for(&s.account, &s.channel_id, &s.kind).unwrap_or_default();
        json!({
            "model": model, "effort": effort,
            "account": s.account, "channel_id": s.channel_id, "channel_name": s.channel_name, "kind": s.kind, "server": s.server, "slug": s.slug,
            "node": s.node, "scope": s.scope, "level": s.level, "platform": place.platform, "name": place.session_name(),
            "server_label": place.server_label(), "chat_label": place.chat_label(), "brief": line::brief_file(&place, &s.level),
        })
    }

    /// The seat this message's chat is: its row, or a new one when the config declares it.
    async fn declare(&self, m: &Inbound) -> Option<(Seat, bool)> {
        if let Some(mut s) = self.store.get(&m.account, &m.channel_id).await {
            if !self.declares(&s, &m.sender_id) {
                self.forget(&s).await;
                self.roster(&s.node, &self.store.all().await).await;
                return None;
            }
            if (!m.channel_name.is_empty() && m.channel_name != s.channel_name) || (!m.server.is_empty() && m.server != s.server) {
                s.channel_name = if m.channel_name.is_empty() { s.channel_name } else { m.channel_name.clone() };
                s.server = if m.server.is_empty() { s.server } else { m.server.clone() };
                self.store.save(&s).await;
            }
            return Some((s, false));
        }
        let (node, level, scope) = self.cfg.seat_for(&m.account, &m.channel_id, kind_str(m.kind), &m.sender_id)?;
        let place = Place::of(m);
        let slug = place.scope_slug();
        let seat = Seat {
            account: m.account.clone(),
            channel_id: m.channel_id.clone(),
            channel_name: m.channel_name.clone(),
            kind: kind_str(m.kind).into(),
            server: m.server.clone(),
            scope: scope.replace("{slug}", &slug),
            slug,
            node,
            level: level.as_str().into(),
            state: "empty".into(),
            ..Default::default()
        };
        self.store.save(&seat).await;
        Some((seat, true))
    }

    fn declares(&self, s: &Seat, sender: &str) -> bool {
        self.cfg.seat_for(&s.account, &s.channel_id, &s.kind, sender).is_some()
    }

    async fn forget(&self, s: &Seat) {
        tracing::info!("seat {} is no longer declared: dropped", s.id());
        self.store.delete(&s.account, &s.channel_id).await;
        self.store.0.unmount(&s.account, &s.channel_id).await;
    }

    /// The seats of `seats` that `node` hosts. The host forgets any seat the list lacks.
    async fn roster(&self, node: &str, seats: &[Seat]) {
        let ids: Vec<String> = seats.iter().filter(|s| s.node == node).map(Seat::id).collect();
        self.queue(Row { node: node.into(), kind: "roster".into(), body: json!({"seats": ids}), ..Default::default() }).await;
    }

    /// At startup: forget each stored seat the config no longer declares, send each host the
    /// current declaration of the rest, so a seat that hears nothing still carries today's name,
    /// then each host's roster, which also clears a seat whose drop a host missed.
    pub async fn sync(&self) {
        let mut kept = vec![];
        for s in self.store.all().await {
            if self.declares(&s, "") {
                self.queue(Self::row(&s, "decl", json!({}))).await;
                kept.push(s);
            } else {
                self.forget(&s).await;
            }
        }
        for node in &self.hosts {
            self.roster(node, &kept).await;
        }
    }

    async fn queue(&self, row: Row) {
        self.store.enqueue(row, crate::db::now()).await;
        self.arrived.notify_waiters();
    }

    fn row(seat: &Seat, kind: &str, body: Value) -> Row {
        Row { node: seat.node.clone(), kind: kind.into(), account: seat.account.clone(), channel_id: seat.channel_id.clone(), body, ..Default::default() }
    }

    /// Route one inbound message, already cleared for a holder and outside a quiet thread.
    pub async fn route(&self, m: &Inbound) -> Routed {
        let Some((seat, new)) = self.declare(m).await else { return Routed::NotSeat };
        if !self.store.routed(&m.account, &m.channel_id, &m.message_id, crate::db::now()).await {
            return Routed::Duplicate;
        }
        let outside = if !m.outside { String::new() } else if m.sender_name.is_empty() { m.sender_id.clone() } else { m.sender_name.clone() };
        self.queue(Row { message_id: m.message_id.clone(), thread_id: m.thread_id.clone(), line: line::push_line(m, &seat.level), ..Self::row(&seat, "line", json!({"outside": outside, "new": new})) }).await;
        Routed::Seat
    }

    /// A seat row for a chat the config declares, without a message, for mount and forward.
    pub async fn declared(&self, account: &str, channel: &str, place: &Place) -> Option<Seat> {
        let m = Inbound { account: account.into(), channel_id: channel.into(), channel_name: place.chat_name.clone(), server: place.server.clone(), kind: place.kind, platform: place.platform.clone(), sender_name: place.person.clone(), ..Default::default() };
        self.declare(&m).await.map(|(s, _)| s)
    }

    /// One line for a hand-mounted chat's holder on a seat host.
    pub async fn hand(&self, node: &str, job: &str, text: &str) {
        self.queue(Row { node: node.into(), kind: "hand".into(), line: text.into(), body: json!({"job": job}), ..Default::default() }).await;
    }

    /// Queue a forwarded line for another seat, and let it read the source chat for a day.
    pub async fn forward(&self, target: &Seat, source: (&str, &str), text: &str) {
        self.queue(Row { line: text.into(), ..Self::row(target, "line", json!({})) }).await;
        self.store.grant_read((&target.account, &target.channel_id), source, crate::db::now() + READ_S).await;
    }

    /// `job` let go of the chat by hand.
    pub async fn released(&self, seat: &Seat, job: &str) {
        self.queue(Self::row(seat, "released", json!({"job": job}))).await;
    }

    pub async fn set_remote_control(&self, seat: &Seat, on: bool) {
        self.queue(Self::row(seat, "remote_control", json!({"on": on}))).await;
    }

    pub async fn set_refresh(&self, seat: &Seat, model: &str, effort: &str) {
        self.queue(Self::row(seat, "refresh", json!({"model": model, "effort": effort}))).await;
    }

    /// Take `node`'s ack, then answer with what waits for it, holding up to `wait_s` for a row.
    pub async fn pull(&self, node: &str, ack_through: i64, wait_s: f64) -> Value {
        self.store.ack(node, ack_through, crate::db::now()).await;
        let deadline = tokio::time::Instant::now() + Duration::from_secs_f64(wait_s.clamp(0.0, MAX_WAIT_S));
        let rows = loop {
            let arrived = self.arrived.notified();
            tokio::pin!(arrived);
            arrived.as_mut().enable();
            let rows = self.store.waiting(node, PULL_ROWS).await;
            if !rows.is_empty() {
                break rows;
            }
            tokio::select! {
                _ = arrived => {}
                _ = tokio::time::sleep_until(deadline) => break rows,
            }
        };
        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            out.push(self.wire(row).await);
        }
        json!({"rows": out, "settings": {"paused": self.cfg.paused, "project": self.cfg.project, "model": self.cfg.model, "effort": self.cfg.effort, "docs": self.cfg.docs}})
    }

    async fn wire(&self, row: Row) -> Value {
        let mut out = row.body.as_object().cloned().unwrap_or_default();
        out.extend([
            ("id".to_string(), json!(row.id)),
            ("kind".to_string(), json!(row.kind)),
            ("account".to_string(), json!(row.account)),
            ("channel_id".to_string(), json!(row.channel_id)),
            ("message_id".to_string(), json!(row.message_id)),
            ("thread_id".to_string(), json!(row.thread_id)),
            ("line".to_string(), json!(row.line)),
        ]);
        if let Some(seat) = self.store.get(&row.account, &row.channel_id).await {
            out.insert("seat".to_string(), self.decl(&seat));
        }
        Value::Object(out)
    }
}

#[cfg(test)]
mod tests;
