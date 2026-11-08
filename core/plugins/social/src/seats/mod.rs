//! The seats: every message in a seated chat goes to that chat's one agent, which social starts
//! on the seat's node and retires when its task is done (seat-pause DESIGN.md).
//!
//! A seat is `empty`, `starting`, `occupied` or `retiring`. Every line goes into the durable
//! `seat_queue` first, and leaves it only when reflection took it, so no line is lost to a dead
//! agent, a failed push or a restart of social. Each seat's work runs under its own lock, and
//! each transition writes the database before it acts. [`Seats::drive`] reconciles one seat
//! with its queue and the clock, and every event and the tick only call it.

pub mod line;
pub mod store;
pub mod world;

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use awm_plugin_sdk::push::Outcome;
use serde_json::{json, Value};

use crate::config::{AccountConfig, SeatsConfig};
use crate::connectors::{ChatKind, Inbound};
use crate::db::{Db, Mount};
use line::Place;
use store::{Previous, Queued, Seat, State, Store};

/// How long a started agent has to come up.
pub const UP_S: f64 = 180.0;
/// How long a done seat's chat stays quiet before its agent is told to retire.
pub const DONE_S: f64 = 4.0 * 3600.0;
/// How long a retiring agent has before the next one starts beside it.
pub const RETIRE_S: f64 = 240.0;
/// How long one line is retried before the seat counts its agent lost.
pub const PUSH_RETRY_S: f64 = 120.0;
/// How long a forward lets the receiving seat read the source chat.
pub const READ_S: f64 = 24.0 * 3600.0;
pub const TICK_S: u64 = 15;

pub const SHIFT_REPLY: &str = "agent is changing shift, will respond within 5 mins";
pub const RETIRE_LINE: &str = "social: this seat's task is done and its chat has been quiet for 4 hours. Write your handoff journal, then retire.";
pub const NOTICE: &str = "social (an automatic notice, not a message from Tony): ";

/// What `start_other` answered.
#[derive(Debug, Clone, PartialEq)]
pub enum Started {
    Job(String),
    Refused(String),
}

/// Everything a seat does outside social's own database.
#[async_trait]
pub trait World: Send + Sync {
    fn now(&self) -> f64;
    async fn sleep(&self, secs: f64);
    /// `reflection start_other` on `node`.
    async fn start(&self, node: &str, spec: Value) -> Started;
    /// `reflection message`: one line typed into `job` on `node`.
    async fn push(&self, node: &str, job: &str, line: &str) -> Outcome;
    /// The live jobs on `node`, or None when it cannot tell.
    async fn live(&self, node: &str) -> Option<HashSet<String>>;
    /// Whether the seat's scope exists on `node`, or None when it cannot tell.
    async fn scope_ready(&self, node: &str, project: &str, scope: &str) -> Option<bool>;
    /// The transcript of a past job, for the next agent's brief.
    async fn transcript(&self, node: &str, job: &str) -> Option<String>;
    async fn react(&self, account: &str, channel: &str, message_id: &str, emoji: &str);
    async fn say(&self, account: &str, channel: &str, thread: Option<&str>, text: &str) -> bool;
    /// `operations request_human`, kind `blocked` or `review`.
    async fn ask(&self, kind: &str, text: &str);
    /// `journal tag_add`, for a person's `user:<slug>` tag.
    async fn tag(&self, name: &str, description: &str);
    /// Publish one `social/seats` event.
    fn emit(&self, _event: Value) {}
    /// `reflection remote_control` on `job`: its claude.ai link when the answer carries one, or why it failed.
    async fn remote_control(&self, _node: &str, _job: &str, _on: bool, _name: &str) -> Result<Option<String>, String> {
        Err("no reflection".into())
    }
    /// The live job's claude.ai link, from its `list_other` row.
    async fn rc_url(&self, _node: &str, _job: &str) -> Option<String> {
        None
    }
}

/// What a switch did to the seat's live session.
#[derive(Debug, Clone, PartialEq)]
pub enum Live {
    /// The seat has no live session to switch.
    None,
    Applied,
    Failed(String),
}

impl Live {
    pub fn as_str(&self) -> String {
        match self {
            Live::None => "none".into(),
            Live::Applied => "applied".into(),
            Live::Failed(why) => format!("failed: {why}"),
        }
    }
}

/// What became of one inbound message.
#[derive(Debug, Clone, PartialEq)]
pub enum Routed {
    /// The chat is a seat, and the seat took the message.
    Seat,
    /// The chat is a seat and saw this message before.
    Duplicate,
    /// The chat is no seat: a hand mount or nobody takes it.
    NotSeat,
}

type Key = (String, String);

pub struct Seats {
    pub store: Store,
    db: Db,
    pub cfg: SeatsConfig,
    accounts: Vec<AccountConfig>,
    world: Arc<dyn World>,
    locks: Mutex<HashMap<Key, Arc<tokio::sync::Mutex<()>>>>,
}

fn kind_str(k: ChatKind) -> &'static str {
    match k {
        ChatKind::Dm => "dm",
        ChatKind::Group => "group",
        ChatKind::Channel => "channel",
    }
}

fn label(s: &Seat) -> String {
    let server = if s.kind == "dm" || s.server.is_empty() { s.account.clone() } else { s.server.clone() };
    let chat = match s.kind.as_str() {
        "dm" => format!("DM {}", s.slug),
        "group" => s.channel_name.clone(),
        _ => format!("#{}", s.channel_name.trim_start_matches('#')),
    };
    format!("{server}/{chat}")
}

impl Seats {
    pub fn new(db: Db, cfg: SeatsConfig, accounts: Vec<AccountConfig>, world: Arc<dyn World>) -> Arc<Self> {
        Arc::new(Self { store: Store(db.clone()), db, cfg, accounts, world, locks: Mutex::new(HashMap::new()) })
    }

    /// Write the row, and publish its `social/seats` event when the save changed one.
    async fn save(&self, s: &Seat) {
        if self.store.save(s).await {
            self.world.emit(s.event(&crate::db::now_iso()));
        }
    }

    /// The session name a seat's agent gets: Remote Control writes each `/` as `-` (R146).
    fn rc_name(s: &Seat) -> String {
        s.name.replace('/', "-")
    }

    fn lock(&self, key: &Key) -> Arc<tokio::sync::Mutex<()>> {
        self.locks.lock().unwrap().entry(key.clone()).or_default().clone()
    }

    fn platform(&self, account: &str) -> String {
        self.accounts.iter().find(|a| a.name == account).map(|a| a.platform.clone()).unwrap_or_default()
    }

    async fn notice(&self, text: &str) {
        match self.cfg.notice_for(&self.accounts) {
            Some(n) => {
                if !self.world.say(&n.account, &n.channel, None, &format!("{NOTICE}{text}")).await {
                    tracing::warn!("a seat notice did not go out: {text}");
                }
            }
            None => tracing::warn!("no notice chat for: {text}"),
        }
    }

    /// The seat this message's chat is: its row, or a new one when the config declares it.
    async fn declare(&self, m: &Inbound) -> Option<(Seat, bool)> {
        if let Some(mut s) = self.store.get(&m.account, &m.channel_id).await {
            if (!m.channel_name.is_empty() && m.channel_name != s.channel_name) || (!m.server.is_empty() && m.server != s.server) {
                s.channel_name = if m.channel_name.is_empty() { s.channel_name } else { m.channel_name.clone() };
                s.server = if m.server.is_empty() { s.server } else { m.server.clone() };
                if s.kind != "dm" {
                    let kind = if s.kind == "group" { ChatKind::Group } else { ChatKind::Channel };
                    s.name = Place { platform: self.platform(&s.account), server: s.server.clone(), kind, chat_name: s.channel_name.clone(), person: String::new() }.session_name();
                }
                self.save(&s).await;
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
            name: place.session_name(),
            ..Default::default()
        };
        self.save(&seat).await;
        Some((seat, true))
    }

    /// Route one inbound message, already cleared for a holder and outside a quiet thread.
    pub async fn route(self: &Arc<Self>, m: &Inbound) -> Routed {
        let Some((mut seat, new)) = self.declare(m).await else { return Routed::NotSeat };
        let now = self.world.now();
        if !self.store.routed(&m.account, &m.channel_id, &m.message_id, now).await {
            return Routed::Duplicate;
        }
        if self.cfg.paused && seat.state == State::Empty {
            seat.missed += 1;
            self.save(&seat).await;
            return Routed::Seat;
        }
        let line = line::push_line(m, &seat.level);
        self.store.enqueue(&m.account, &m.channel_id, &m.message_id, &m.thread_id, &line, now).await;
        seat.last_message = now;
        seat.done_at = 0.0;
        if m.outside && seat.held != "admitted" && !seat.held.starts_with("outside") {
            seat.held = format!("outside:{}", if m.sender_name.is_empty() { &m.sender_id } else { &m.sender_name });
            self.save(&seat).await;
            self.world.ask("blocked", &format!("A sender outside the lab wrote in the seat {}. Its messages wait until `social seat_admit account={} channel={}`.", label(&seat), seat.account, seat.channel_id)).await;
            return Routed::Seat;
        }
        self.save(&seat).await;
        if new && seat.kind == "dm" {
            self.world.tag(&format!("user:{}", seat.slug), &format!("journals of the seat agent for {}", seat.slug)).await;
            self.world.ask("review", &format!("A new DM seat opened: {} on {}. Its agent starts now.", label(&seat), seat.node)).await;
        }
        let me = self.clone();
        let key = seat.key();
        tokio::spawn(async move { me.drive(&key, None).await });
        Routed::Seat
    }

    /// Bring one seat in line with its queue and the clock. `live` is the seat node's live jobs
    /// when the caller knows them.
    pub async fn drive(self: &Arc<Self>, key: &Key, live: Option<&HashSet<String>>) {
        let lock = self.lock(key);
        let _held = lock.lock().await;
        for _ in 0..3 {
            let Some(seat) = self.store.get(&key.0, &key.1).await else { return };
            if !self.step(seat, live).await {
                return;
            }
        }
    }

    /// One transition. True when the seat should be looked at again at once.
    async fn step(self: &Arc<Self>, mut seat: Seat, live: Option<&HashSet<String>>) -> bool {
        let now = self.world.now();
        let pending = self.store.pending(&seat.account, &seat.channel_id).await;
        let gone = |s: &Seat| live.is_some_and(|l| !l.contains(&s.job));
        match seat.state {
            State::Empty => {
                let held = seat.held.starts_with("outside") || seat.held == "scope";
                if pending.is_empty() || self.cfg.paused || held || !seat.breaker.allows(now) {
                    return false;
                }
                self.cold_start(seat, &pending, None).await;
                false
            }
            State::Starting => {
                if !seat.job.is_empty() && live.is_some_and(|l| l.contains(&seat.job)) {
                    return self.up(seat, now).await;
                }
                if now >= seat.deadline {
                    let here = match live {
                        Some(l) => Some(l.clone()),
                        None => self.world.live(&seat.job_node).await,
                    };
                    match here {
                        // Cannot tell: a start is never given up on a guess.
                        None => return false,
                        Some(l) if !seat.job.is_empty() && l.contains(&seat.job) => return self.up(seat, now).await,
                        Some(_) => {}
                    }
                    tracing::info!("seat {}: its agent {} did not come up", label(&seat), seat.job);
                    self.failed_start(seat, "its agent did not come up within 3 minutes").await;
                    return true;
                }
                false
            }
            State::Occupied => {
                if gone(&seat) {
                    if seat.done_at == 0.0 {
                        self.notice(&format!("the agent of the seat {} ({}@{}) ended before its task was done.", label(&seat), seat.job, seat.job_node)).await;
                    }
                    self.vacate(&mut seat).await;
                    return !pending.is_empty();
                }
                if seat.done_at > 0.0 && now - seat.done_at.max(seat.last_message) >= DONE_S && pending.is_empty() {
                    seat.state = State::Retiring;
                    seat.deadline = now + RETIRE_S;
                    seat.replied = false;
                    self.save(&seat).await;
                    if self.world.push(&seat.job_node, &seat.job, RETIRE_LINE).await == Outcome::Gone {
                        self.vacate(&mut seat).await;
                    }
                    return false;
                }
                if !pending.is_empty() && !self.pump(&mut seat, &pending).await {
                    self.vacate(&mut seat).await;
                    return true;
                }
                false
            }
            State::Retiring => {
                if gone(&seat) {
                    self.vacate(&mut seat).await;
                    return !pending.is_empty();
                }
                if pending.is_empty() {
                    return false;
                }
                if !seat.replied {
                    seat.replied = true;
                    self.save(&seat).await;
                    let first = &pending[0];
                    let thread = if first.thread_id.is_empty() { None } else { Some(first.thread_id.as_str()) };
                    self.world.say(&seat.account, &seat.channel_id, thread, SHIFT_REPLY).await;
                    self.react(&seat, &pending).await;
                }
                if now >= seat.deadline {
                    let previous = Previous { job: seat.job.clone(), node: seat.job_node.clone() };
                    self.cold_start(seat, &pending, Some(previous)).await;
                }
                false
            }
        }
    }

    /// The started agent is up: its brief's lines are delivered.
    async fn up(&self, mut seat: Seat, now: f64) -> bool {
        let briefed = self.store.pending(&seat.account, &seat.channel_id).await;
        if let Some(last) = briefed.iter().rev().find(|q| q.briefed_to == seat.job) {
            self.store.set_line(&seat.job_node, &seat.job, &seat.account, &seat.channel_id, &last.thread_id, &last.message_id, now).await;
        }
        self.store.deliver_briefed(&seat.account, &seat.channel_id, &seat.job, now).await;
        seat.state = State::Occupied;
        seat.since = now;
        if seat.remote_control {
            seat.rc_url = self.world.rc_url(&seat.job_node, &seat.job).await.unwrap_or_default();
        }
        self.save(&seat).await;
        true
    }

    /// The agent is gone: free the seat and keep its lines for the next agent.
    async fn vacate(&self, seat: &mut Seat) {
        if !seat.job.is_empty() {
            seat.previous = Previous { job: seat.job.clone(), node: seat.job_node.clone() };
            self.unbind(seat).await;
        }
        seat.state = State::Empty;
        seat.job.clear();
        seat.rc_url.clear();
        seat.adopted = false;
        seat.done_at = 0.0;
        self.store.unbrief(&seat.account, &seat.channel_id).await;
        self.save(seat).await;
    }

    async fn unbind(&self, seat: &Seat) {
        if let Some(m) = self.db.holder(&seat.account, &seat.channel_id).await {
            if m.job == seat.job && m.node == seat.job_node {
                self.db.unmount(&seat.account, &seat.channel_id).await;
            }
        }
    }

    async fn react(&self, seat: &Seat, pending: &[Queued]) {
        if let Some(last) = pending.iter().rev().find(|q| !q.message_id.is_empty()) {
            self.world.react(&seat.account, &seat.channel_id, &last.message_id, "👀").await;
        }
    }

    async fn failed_start(&self, mut seat: Seat, why: &str) {
        let now = self.world.now();
        self.unbind(&seat).await;
        self.store.unbrief(&seat.account, &seat.channel_id).await;
        seat.state = State::Empty;
        seat.job.clear();
        let tripped = seat.breaker.fail(now);
        self.save(&seat).await;
        if tripped {
            self.notice(&format!(
                "the seat {} could not start an agent 4 times in an hour ({why}). It holds its messages for 6 hours, or until `social seat_reset account={} channel={}`.",
                label(&seat),
                seat.account,
                seat.channel_id
            ))
            .await;
        }
    }

    /// Start the seat's agent with every waiting line in its brief.
    async fn cold_start(&self, mut seat: Seat, pending: &[Queued], previous: Option<Previous>) {
        let now = self.world.now();
        let project = self.cfg.project.clone();
        if self.world.scope_ready(&seat.node, &project, &seat.scope).await == Some(false) {
            if seat.held != "scope" {
                seat.held = "scope".into();
                self.save(&seat).await;
                self.world.ask("blocked", &format!("The seat {} has no scope {project}/{} on {}. Create it, then run `social seat_reset account={} channel={}`.", label(&seat), seat.scope, seat.node, seat.account, seat.channel_id)).await;
            }
            return;
        }
        if seat.state == State::Empty {
            self.react(&seat, pending).await;
        }
        let old = (seat.state == State::Retiring).then(|| seat.clone());
        let previous = previous.or_else(|| (!seat.previous.job.is_empty()).then(|| seat.previous.clone()));
        let transcript = match &previous {
            Some(p) => self.world.transcript(&p.node, &p.job).await,
            None => None,
        };
        let place = Place {
            platform: self.platform(&seat.account),
            server: seat.server.clone(),
            kind: match seat.kind.as_str() {
                "dm" => ChatKind::Dm,
                "group" => ChatKind::Group,
                _ => ChatKind::Channel,
            },
            chat_name: seat.channel_name.clone(),
            person: seat.slug.clone(),
        };
        let lines: Vec<String> = pending.iter().map(|q| q.line.clone()).collect();
        let facts = line::BriefFacts {
            docs: &self.cfg.docs,
            place: &place,
            level: &seat.level,
            project: &project,
            scope: &seat.scope,
            previous: previous.as_ref().map(|p| (p.job.as_str(), p.node.as_str(), transcript.as_deref())),
        };
        let spec = json!({
            "project": project, "scope": seat.scope, "name": seat.name, "prompt": line::brief(&facts, &lines),
            "trust": seat.level, "model": self.cfg.model, "effort": self.cfg.effort, "remote_control": seat.remote_control,
        });
        let ids: Vec<i64> = pending.iter().map(|q| q.id).collect();
        seat.state = State::Starting;
        seat.since = now;
        seat.deadline = now + UP_S;
        seat.job.clear();
        seat.rc_url.clear();
        seat.job_node = seat.node.clone();
        seat.replied = false;
        if seat.held == "scope" {
            seat.held.clear();
        }
        self.save(&seat).await;
        self.store.brief_to(ids.clone(), "starting").await;
        match self.world.start(&seat.node, spec).await {
            Started::Job(job) => {
                seat.job = job.clone();
                if let Some(p) = previous {
                    seat.previous = p;
                }
                self.save(&seat).await;
                self.store.brief_to(ids, &job).await;
                let _ = self
                    .db
                    .mount(Mount { account: seat.account.clone(), channel_id: seat.channel_id.clone(), channel_name: seat.channel_name.clone(), node: seat.node.clone(), job, session_name: seat.name.clone(), mounted_at: String::new() })
                    .await;
                if let Some(old) = old {
                    tracing::info!("seat {}: {} took over from retiring {}", label(&seat), seat.job, old.job);
                }
            }
            Started::Refused(why) => {
                tracing::info!("seat {}: start refused: {why}", label(&seat));
                if let Some(old) = old {
                    // The retiring agent still runs: it keeps the seat until it is gone.
                    let mut back = old;
                    back.replied = true;
                    self.store.unbrief(&back.account, &back.channel_id).await;
                    back.breaker.fail(now);
                    self.save(&back).await;
                    return;
                }
                self.failed_start(seat, &why).await;
            }
        }
    }

    /// Push every waiting line to the seat's agent, in order. False when the agent is lost.
    async fn pump(&self, seat: &mut Seat, pending: &[Queued]) -> bool {
        for q in pending {
            let started = self.world.now();
            let mut delay = 2.0;
            loop {
                match self.world.push(&seat.job_node, &seat.job, &q.line).await {
                    Outcome::Delivered => {
                        let now = self.world.now();
                        self.store.delivered(q.id, now).await;
                        self.store.set_line(&seat.job_node, &seat.job, &seat.account, &seat.channel_id, &q.thread_id, &q.message_id, now).await;
                        break;
                    }
                    Outcome::Gone => return false,
                    Outcome::Retry(why) => {
                        if self.world.now() - started >= PUSH_RETRY_S {
                            tracing::info!("seat {}: {} refused a line past the retry window: {why}", label(seat), seat.job);
                            return false;
                        }
                        self.world.sleep(delay).await;
                        delay = (delay * 2.0).min(30.0);
                    }
                }
            }
        }
        true
    }

    /// Every seat once. Each node's live jobs are read once, when a seat there needs them.
    pub async fn tick(self: &Arc<Self>) {
        let mut cache: HashMap<String, Option<HashSet<String>>> = HashMap::new();
        for s in self.store.all().await {
            let lock = self.lock(&s.key());
            let _held = lock.lock().await;
            for _ in 0..3 {
                let Some(seat) = self.store.get(&s.account, &s.channel_id).await else { break };
                let live = if seat.state != State::Empty && !seat.job_node.is_empty() {
                    if !cache.contains_key(&seat.job_node) {
                        let l = self.world.live(&seat.job_node).await;
                        cache.insert(seat.job_node.clone(), l);
                    }
                    cache[&seat.job_node].clone()
                } else {
                    None
                };
                if !self.step(seat, live.as_ref()).await {
                    break;
                }
            }
        }
    }

    pub async fn tick_forever(self: Arc<Self>) {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(TICK_S)).await;
            self.tick().await;
        }
    }

    /// The agent `(node, job)` made a social call, so it is up.
    pub async fn touch(self: &Arc<Self>, node: &str, job: &str) {
        for s in self.store.of_job(node, job).await {
            if s.state == State::Starting {
                let live = HashSet::from([job.to_string()]);
                self.drive(&s.key(), Some(&live)).await;
            }
        }
    }

    /// `mark_seat_done` from `(node, job)`: the seats it marked.
    pub async fn mark_done(&self, node: &str, job: &str) -> Vec<Seat> {
        let now = self.world.now();
        let mut out = vec![];
        for mut s in self.store.of_job(node, job).await {
            if matches!(s.state, State::Occupied | State::Starting) {
                s.done_at = now;
                self.save(&s).await;
                out.push(s);
            }
        }
        out
    }

    pub async fn reset(self: &Arc<Self>, account: &str, channel: &str) -> Option<Seat> {
        let mut s = self.store.get(account, channel).await?;
        s.breaker = Default::default();
        if s.held == "scope" {
            s.held.clear();
        }
        self.save(&s).await;
        self.drive(&s.key(), None).await;
        self.store.get(account, channel).await
    }

    pub async fn admit(self: &Arc<Self>, account: &str, channel: &str) -> Option<Seat> {
        let mut s = self.store.get(account, channel).await?;
        s.held = "admitted".into();
        self.save(&s).await;
        self.drive(&s.key(), None).await;
        self.store.get(account, channel).await
    }

    /// A seat row for a chat the config declares, without a message, for adopt and forward.
    pub async fn declared(&self, account: &str, channel: &str, place: &Place) -> Option<Seat> {
        let m = Inbound { account: account.into(), channel_id: channel.into(), channel_name: place.chat_name.clone(), server: place.server.clone(), kind: place.kind, platform: place.platform.clone(), sender_name: place.person.clone(), ..Default::default() };
        self.declare(&m).await.map(|(s, _)| s)
    }

    /// Make `(node, job)` the seat's agent, keeping its conversation.
    pub async fn adopt(&self, mut seat: Seat, node: &str, job: &str, session_name: &str) -> Seat {
        let now = self.world.now();
        if !seat.job.is_empty() && (seat.job != job || seat.job_node != node) {
            seat.previous = Previous { job: seat.job.clone(), node: seat.job_node.clone() };
        }
        seat.state = State::Occupied;
        seat.job = job.into();
        seat.job_node = node.into();
        seat.adopted = true;
        seat.since = now;
        seat.done_at = 0.0;
        // Every seat session so far started with Remote Control on, and adoption leaves it so.
        seat.remote_control = true;
        seat.rc_url = self.world.rc_url(node, job).await.unwrap_or_default();
        self.save(&seat).await;
        self.store.deliver_briefed(&seat.account, &seat.channel_id, job, now).await;
        let name = if session_name.is_empty() { seat.name.clone() } else { session_name.to_string() };
        let _ = self.db.mount(Mount { account: seat.account.clone(), channel_id: seat.channel_id.clone(), channel_name: seat.channel_name.clone(), node: node.into(), job: job.into(), session_name: name, mounted_at: String::new() }).await;
        seat
    }

    /// A hand mount let go of a declared seat: the seat is empty again.
    pub async fn released(&self, account: &str, channel: &str, node: &str, job: &str) {
        if let Some(mut s) = self.store.get(account, channel).await {
            if s.job == job && s.job_node == node && s.state != State::Empty {
                s.previous = Previous { job: s.job.clone(), node: s.job_node.clone() };
                s.state = State::Empty;
                s.job.clear();
                s.rc_url.clear();
                s.adopted = false;
                self.save(&s).await;
            }
        }
    }

    /// Queue a forwarded line for another seat, and let it read the source chat for a day.
    pub async fn forward(self: &Arc<Self>, target: Seat, source: (&str, &str), line: &str) {
        let now = self.world.now();
        self.store.enqueue(&target.account, &target.channel_id, "", "", line, now).await;
        self.store.grant_read((&target.account, &target.channel_id), source, now + READ_S).await;
        let mut t = target;
        t.last_message = now;
        t.done_at = 0.0;
        self.save(&t).await;
        let me = self.clone();
        let key = t.key();
        tokio::spawn(async move { me.drive(&key, None).await });
    }

    /// At start: each mount on a declared chat is that seat's agent.
    pub async fn adopt_mounts(&self, mounts: Vec<(Mount, Place)>) -> usize {
        let mut n = 0;
        for (m, place) in mounts {
            let seat = match self.store.get(&m.account, &m.channel_id).await {
                Some(s) => s,
                None => match self.declared(&m.account, &m.channel_id, &place).await {
                    Some(s) => s,
                    None => continue,
                },
            };
            if seat.state == State::Empty || (seat.job == m.job && seat.job_node == m.node && seat.state != State::Occupied) {
                self.adopt(seat, &m.node, &m.job, &m.session_name).await;
                n += 1;
            }
        }
        n
    }

    /// With the seats unpaused, one notice line per seat chat that got messages while paused.
    pub async fn unpause_summary(&self) {
        if self.cfg.paused {
            return;
        }
        let missed: Vec<Seat> = self.store.all().await.into_iter().filter(|s| s.missed > 0).collect();
        if missed.is_empty() {
            return;
        }
        let lines: Vec<String> = missed.iter().map(|s| format!("- {}: {} message{}", label(s), s.missed, if s.missed == 1 { "" } else { "s" })).collect();
        self.notice(&format!("the seats are on again. These chats got messages while they were paused, and nobody answered them:\n{}", lines.join("\n"))).await;
        for mut s in missed {
            s.missed = 0;
            self.save(&s).await;
        }
    }

    /// Set the seat's Remote Control switch, and switch its live session too.
    pub async fn set_remote_control(&self, account: &str, channel: &str, on: bool) -> Option<(Seat, Live)> {
        let key = (account.to_string(), channel.to_string());
        let lock = self.lock(&key);
        let _held = lock.lock().await;
        let mut seat = self.store.get(account, channel).await?;
        seat.remote_control = on;
        if !on {
            seat.rc_url.clear();
        }
        self.save(&seat).await;
        let live_job = !seat.job.is_empty() && matches!(seat.state, State::Starting | State::Occupied);
        if !live_job {
            return Some((seat, Live::None));
        }
        let live = match self.world.remote_control(&seat.job_node, &seat.job, on, &Self::rc_name(&seat)).await {
            Ok(url) => {
                if on {
                    seat.rc_url = match url {
                        Some(u) => u,
                        None => self.world.rc_url(&seat.job_node, &seat.job).await.unwrap_or_default(),
                    };
                    self.save(&seat).await;
                }
                Live::Applied
            }
            Err(why) => {
                tracing::info!("seat {}: Remote Control {} for {} failed: {why}", label(&seat), if on { "on" } else { "off" }, seat.job);
                Live::Failed(why)
            }
        };
        Some((seat, live))
    }

    /// The pending lines of a seat, for `seats`.
    pub async fn queued(&self, account: &str, channel: &str) -> i64 {
        self.store.pending(account, channel).await.len() as i64
    }
}

#[cfg(test)]
mod tests;
