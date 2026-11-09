//! The seats of this node: every message in a seated chat goes to that chat's one agent, which
//! this plugin starts here and keeps for as long as it runs.
//!
//! A seat is `empty`, `starting` or `occupied`. Every line goes into the durable
//! `seat_queue` first, and leaves it only when reflection took it, so no line is lost to a dead
//! agent, a failed push or a restart. Each seat's work runs under its own lock, and each
//! transition writes the database before it acts. [`Seats::drive`] reconciles one seat with its
//! queue and the clock, and every event and the tick only call it.
//!
//! Nothing here depends on the link to social. What social should hear goes in the outbox.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, RwLock};

use async_trait::async_trait;
use awm_plugin_sdk::push::Outcome;
use awm_plugin_sdk::Backoff;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::brief;
use crate::store::{Decl, Latest, Previous, Queued, Seat, Seed, State, Store};

/// Transitions one look at a seat may take before it waits for the next tick.
const STEPS_PER_LOOK: usize = 3;

/// How long a started agent has to come up.
pub const UP_S: f64 = 180.0;
/// How long one line is retried before the seat counts its agent lost.
pub const PUSH_RETRY_S: f64 = 120.0;
/// The shortest wait before a seat's overdue clock is looked at again.
pub const TICK_S: u64 = 15;


/// What `start_other` answered.
#[derive(Debug, Clone, PartialEq)]
pub enum Started {
    Job(String),
    Refused(String),
}

/// Everything a seat does outside its own database.
#[async_trait]
pub trait World: Send + Sync {
    fn now(&self) -> f64;
    async fn sleep(&self, secs: f64);
    /// `reflection start_other` here.
    async fn start(&self, spec: Value) -> Started;
    /// `reflection message`: one line typed into `job`.
    async fn push(&self, job: &str, line: &str) -> Outcome;
    /// The live jobs here, or None when it cannot tell.
    async fn live(&self) -> Option<HashSet<String>>;
    /// Whether the seat's scope exists here, or None when it cannot tell.
    async fn scope_ready(&self, project: &str, scope: &str) -> Option<bool>;
    /// The transcript of a past job, for the next agent's brief.
    async fn transcript(&self, job: &str) -> Option<String>;
    /// `reflection remote_control` on `job`: its claude.ai link when the answer carries one, or why it failed.
    async fn remote_control(&self, job: &str, on: bool, name: &str) -> Result<Option<String>, String>;
    /// The live job's claude.ai link, from its `list_other` row.
    async fn rc_url(&self, job: &str) -> Option<String>;
    /// Type one line into a hand-mounted chat's holder.
    fn hand(&self, job: &str, line: &str);
    /// Queue one row for social.
    async fn up(&self, kind: &str, body: Value);
}

/// What social sets for every seat. It arrives on each pull and is kept across a restart.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Settings {
    pub paused: bool,
    pub project: String,
    pub model: String,
    pub effort: String,
    /// The seat docs on this node: `seat-sop.md` and `briefs/`.
    pub docs: String,
}

impl Default for Settings {
    /// Paused until social has said otherwise.
    fn default() -> Self {
        Self { paused: true, project: String::new(), model: String::new(), effort: String::new(), docs: String::new() }
    }
}

/// One line social sent for a seat.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct Incoming {
    pub id: i64,
    pub seat: Decl,
    pub message_id: String,
    pub thread_id: String,
    pub line: String,
    /// The sender's name when the sender is outside the lab.
    pub outside: String,
    /// The first message of a new seat.
    pub new: bool,
    /// A line that waited in social before seats moved here, which a pause does not discard.
    pub kept: bool,
}

/// What a switch did to the seat's live session.
#[derive(Debug, Clone, PartialEq)]
pub enum Live {
    /// The seat has no live session to switch.
    None,
    Applied,
    Failed(String),
}

type Key = (String, String);

pub struct Seats {
    pub store: Store,
    settings: RwLock<Settings>,
    world: Arc<dyn World>,
    locks: Mutex<HashMap<Key, Arc<tokio::sync::Mutex<()>>>>,
    reported_live: Mutex<Option<HashSet<String>>>,
    wake: tokio::sync::Notify,
}

fn label(s: &Seat) -> String {
    let d = &s.decl;
    let server = if d.kind == "dm" || d.server.is_empty() { d.account.clone() } else { d.server.clone() };
    let chat = match d.kind.as_str() {
        "dm" => format!("DM {}", d.slug),
        "group" => d.channel_name.clone(),
        _ => format!("#{}", d.channel_name.trim_start_matches('#')),
    };
    format!("{server}/{chat}")
}

impl Seats {
    pub fn new(store: Store, settings: Settings, world: Arc<dyn World>) -> Arc<Self> {
        Arc::new(Self { store, settings: RwLock::new(settings), world, locks: Mutex::new(HashMap::new()), reported_live: Mutex::new(None), wake: tokio::sync::Notify::new() })
    }

    pub fn settings(&self) -> Settings {
        self.settings.read().unwrap_or_else(|p| p.into_inner()).clone()
    }

    fn paused(&self) -> bool {
        self.settings.read().unwrap_or_else(|p| p.into_inner()).paused
    }

    /// Take what social set. Going from paused to on posts the summary of what was missed.
    pub async fn configure(self: &Arc<Self>, settings: Settings) {
        {
            let mut held = self.settings.write().unwrap_or_else(|p| p.into_inner());
            if *held == settings {
                return;
            }
            *held = settings.clone();
        }
        self.store.set_meta("settings", &serde_json::to_string(&settings).unwrap_or_default()).await;
        if !settings.paused {
            self.unpause_summary().await;
            self.wake();
        }
    }

    /// Write the row, and tell social when what it shows of the seat changed.
    async fn save(&self, s: &Seat) {
        if self.store.save(s).await {
            let queued = self.queued(s.account(), s.channel()).await;
            let ts = crate::now_iso();
            self.world
                .up("seat", json!({"account": s.account(), "channel_id": s.channel(), "state": s.state.as_str(), "job": s.job, "remote_control": s.remote_control, "rc_url": s.rc_url, "public": s.public(queued), "event": s.event(&ts)}))
                .await;
        }
    }

    /// The session name a seat's agent gets: Remote Control writes each `/` as `-` (R146).
    fn rc_name(s: &Seat) -> String {
        s.decl.name.replace('/', "-")
    }

    fn lock(&self, key: &Key) -> Arc<tokio::sync::Mutex<()>> {
        self.locks.lock().unwrap().entry(key.clone()).or_default().clone()
    }

    async fn notice(&self, text: &str) {
        self.world.up("notice", json!({"text": text})).await;
    }

    async fn say(&self, s: &Seat, thread: Option<&str>, text: &str) {
        self.world.up("send", json!({"account": s.account(), "channel": s.channel(), "thread": thread, "text": text})).await;
    }

    async fn ask(&self, kind: &str, text: &str) {
        self.world.up("ask", json!({"kind": kind, "text": text})).await;
    }

    async fn mounted(&self, s: &Seat, job: &str, session_name: &str) {
        self.world.up("mount", json!({"account": s.account(), "channel_id": s.channel(), "channel_name": s.decl.channel_name, "job": job, "session_name": session_name})).await;
    }

    async fn unbind(&self, seat: &Seat) {
        self.world.up("unmount", json!({"account": seat.account(), "channel_id": seat.channel(), "job": seat.job})).await;
    }

    async fn set_line(&self, job: &str, latest: Latest, now: f64) {
        self.world.up("line", json!({"job": job, "account": latest.account, "channel_id": latest.channel, "thread_id": latest.thread, "message_id": latest.message_id})).await;
        self.store.set_line(job, &latest, now).await;
    }

    /// The seat this row names, created or brought up to date from social's declaration.
    async fn upsert(&self, decl: &Decl) -> Seat {
        match self.store.get(&decl.account, &decl.channel_id).await {
            Some(mut s) => {
                if s.decl != *decl {
                    s.decl = decl.clone();
                    self.save(&s).await;
                }
                s
            }
            None => {
                let s = Seat { decl: decl.clone(), ..Default::default() };
                self.save(&s).await;
                s
            }
        }
    }

    /// A seat's state from social's old database. It never overwrites a seat already here.
    pub async fn seed(&self, decl: Decl, seed: Seed) {
        if self.store.get(&decl.account, &decl.channel_id).await.is_none() {
            self.save(&Seat::seeded(decl, seed)).await;
        }
    }

    /// Take one line from social.
    pub async fn take(self: &Arc<Self>, m: Incoming) {
        let mut seat = self.upsert(&m.seat).await;
        let now = self.world.now();
        if self.paused() && seat.state == State::Empty && !m.kept {
            seat.missed += 1;
            self.save(&seat).await;
            return;
        }
        self.store.enqueue(Some(m.id), seat.account(), seat.channel(), &m.message_id, &m.thread_id, &m.line, now).await;
        seat.last_message = now;
        seat.done_at = 0.0;
        if !m.outside.is_empty() && seat.held != "admitted" && !seat.held.starts_with("outside") {
            seat.held = format!("outside:{}", m.outside);
            self.save(&seat).await;
            self.ask("blocked", &format!("A sender outside the lab wrote in the seat {}. Its messages wait until `seats admit account={} channel={}` on {}.", label(&seat), seat.account(), seat.channel(), seat.decl.node)).await;
            return;
        }
        self.save(&seat).await;
        if m.new && seat.decl.kind == "dm" {
            self.world.up("tag", json!({"name": format!("user:{}", seat.decl.slug), "description": format!("journals of the seat agent for {}", seat.decl.slug)})).await;
            self.ask("review", &format!("A new DM seat opened: {} on {}. Its agent starts now.", label(&seat), seat.decl.node)).await;
        }
        let me = self.clone();
        let key = seat.key();
        tokio::spawn(async move { me.drive(&key, None).await });
    }

    /// Bring one seat in line with its queue and the clock. `live` is this node's live jobs
    /// when the caller knows them.
    pub async fn drive(self: &Arc<Self>, key: &Key, live: Option<&HashSet<String>>) {
        let lock = self.lock(key);
        let _held = lock.lock().await;
        // A job started or adopted since `live` was read is not in it, and is not gone.
        let newer = match live {
            Some(l) => self.store.get(&key.0, &key.1).await.is_some_and(|s| !s.job.is_empty() && !l.contains(&s.job)),
            None => false,
        };
        let reread = if newer { self.world.live().await } else { None };
        let live = if newer { reread.as_ref() } else { live };
        for _ in 0..STEPS_PER_LOOK {
            let Some(seat) = self.store.get(&key.0, &key.1).await else { return };
            if !self.step(seat, live).await {
                return;
            }
        }
    }

    /// One transition. True when the seat should be looked at again at once.
    async fn step(self: &Arc<Self>, mut seat: Seat, live: Option<&HashSet<String>>) -> bool {
        let now = self.world.now();
        let pending = self.store.pending(seat.account(), seat.channel()).await;
        let gone = |s: &Seat| live.is_some_and(|l| !l.contains(&s.job));
        match seat.state {
            State::Empty => {
                let held = seat.held.starts_with("outside") || seat.held == "scope";
                if pending.is_empty() || self.paused() || held || !seat.breaker.allows(now) {
                    return false;
                }
                self.cold_start(seat, &pending).await;
                false
            }
            State::Starting => {
                if !seat.job.is_empty() && live.is_some_and(|l| l.contains(&seat.job)) {
                    return self.up(seat, now).await;
                }
                if now >= seat.deadline {
                    let here = match live {
                        Some(l) => Some(l.clone()),
                        None => self.world.live().await,
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
                        self.notice(&format!("the agent of the seat {} ({}@{}) ended before its task was done.", label(&seat), seat.job, seat.decl.node)).await;
                    }
                    self.vacate(&mut seat).await;
                    return !pending.is_empty();
                }
                if !pending.is_empty() && !self.pump(&mut seat, &pending).await {
                    self.vacate(&mut seat).await;
                    return true;
                }
                false
            }
        }
    }

    /// The started agent is up: its brief's lines are delivered.
    async fn up(&self, mut seat: Seat, now: f64) -> bool {
        let briefed = self.store.pending(seat.account(), seat.channel()).await;
        if let Some(last) = briefed.iter().rev().find(|q| q.briefed_to == seat.job) {
            let latest = Latest { account: seat.account().into(), channel: seat.channel().into(), thread: last.thread_id.clone(), message_id: last.message_id.clone() };
            self.set_line(&seat.job, latest, now).await;
        }
        self.store.deliver_briefed(seat.account(), seat.channel(), &seat.job, now).await;
        seat.state = State::Occupied;
        if seat.remote_control {
            seat.rc_url = self.world.rc_url(&seat.job).await.unwrap_or_default();
        }
        self.save(&seat).await;
        true
    }

    /// The agent is gone: free the seat and keep its lines for the next agent.
    async fn vacate(&self, seat: &mut Seat) {
        if !seat.job.is_empty() {
            seat.previous = Previous { job: seat.job.clone(), node: seat.decl.node.clone(), adopted: seat.adopted };
            self.unbind(seat).await;
        }
        seat.state = State::Empty;
        seat.job.clear();
        seat.rc_url.clear();
        seat.adopted = false;
        seat.done_at = 0.0;
        self.store.unbrief(seat.account(), seat.channel()).await;
        self.save(seat).await;
    }

    async fn react(&self, seat: &Seat, pending: &[Queued]) {
        if let Some(last) = pending.iter().rev().find(|q| !q.message_id.is_empty()) {
            self.world.up("react", json!({"account": seat.account(), "channel": seat.channel(), "message_id": last.message_id, "emoji": "👀"})).await;
        }
    }

    async fn failed_start(&self, mut seat: Seat, why: &str) {
        let now = self.world.now();
        self.unbind(&seat).await;
        self.store.unbrief(seat.account(), seat.channel()).await;
        seat.state = State::Empty;
        seat.job.clear();
        let tripped = seat.breaker.fail(now);
        self.save(&seat).await;
        if tripped {
            self.notice(&format!(
                "the seat {} could not start an agent 4 times in an hour ({why}). It holds its messages for 6 hours, or until `seats reset account={} channel={}` on {}.",
                label(&seat),
                seat.account(),
                seat.channel(),
                seat.decl.node
            ))
            .await;
        }
    }

    /// Start the seat's agent with every waiting line in its brief.
    async fn cold_start(&self, mut seat: Seat, pending: &[Queued]) {
        let now = self.world.now();
        let settings = self.settings();
        if self.world.scope_ready(&settings.project, &seat.decl.scope).await == Some(false) {
            if seat.held != "scope" {
                seat.held = "scope".into();
                self.save(&seat).await;
                self.ask("blocked", &format!("The seat {} has no scope {}/{} on {}. Create it, then run `seats reset account={} channel={}` there.", label(&seat), settings.project, seat.decl.scope, seat.decl.node, seat.account(), seat.channel())).await;
            }
            return;
        }
        if seat.state == State::Empty {
            self.react(&seat, pending).await;
        }
        let previous = (!seat.previous.job.is_empty()).then(|| seat.previous.clone());
        let transcript = match &previous {
            Some(p) if p.node == seat.decl.node => self.world.transcript(&p.job).await,
            _ => None,
        };
        let lines: Vec<String> = pending.iter().map(|q| q.line.clone()).collect();
        let facts = brief::Facts {
            docs: &settings.docs,
            decl: &seat.decl,
            project: &settings.project,
            previous: previous.as_ref().map(|p| (p.job.as_str(), p.node.as_str(), transcript.as_deref())),
        };
        let mut spec = json!({
            "project": settings.project, "scope": seat.decl.scope, "name": seat.decl.name, "prompt": brief::brief(&facts, &lines),
            "trust": seat.decl.level, "model": settings.model, "effort": settings.effort, "remote_control": seat.remote_control,
            "pin": true,
        });
        // A host reboot can bring the old agent back after its seat emptied: the new one takes its place.
        if let Some(p) = previous.as_ref().filter(|p| !p.adopted && p.node == seat.decl.node) {
            spec["replaces"] = json!(p.job);
        }
        let ids: Vec<i64> = pending.iter().map(|q| q.id).collect();
        seat.state = State::Starting;
        seat.deadline = now + UP_S;
        seat.job.clear();
        seat.rc_url.clear();
        if seat.held == "scope" {
            seat.held.clear();
        }
        self.save(&seat).await;
        self.store.brief_to(ids.clone(), "starting").await;
        match self.world.start(spec).await {
            Started::Job(job) => {
                seat.job = job.clone();
                self.save(&seat).await;
                self.store.brief_to(ids, &job).await;
                self.mounted(&seat, &job, &seat.decl.name).await;
                // The session's `started` event can come before this start answers.
                self.wake();
            }
            Started::Refused(why) => {
                tracing::info!("seat {}: start refused: {why}", label(&seat));
                self.failed_start(seat, &why).await;
            }
        }
    }

    /// Push every waiting line to the seat's agent, in order. False when the agent is lost.
    async fn pump(&self, seat: &mut Seat, pending: &[Queued]) -> bool {
        for q in pending {
            let started = self.world.now();
            let mut delay = Backoff::doubling(2.0, 30.0);
            loop {
                match self.world.push(&seat.job, &q.line).await {
                    Outcome::Delivered => {
                        let now = self.world.now();
                        self.store.delivered(q.id, now).await;
                        let latest = Latest { account: seat.account().into(), channel: seat.channel().into(), thread: q.thread_id.clone(), message_id: q.message_id.clone() };
                        self.set_line(&seat.job, latest, now).await;
                        break;
                    }
                    Outcome::Gone => return false,
                    Outcome::Retry(why) => {
                        if self.world.now() - started >= PUSH_RETRY_S {
                            tracing::info!("seat {}: {} refused a line past the retry window: {why}", label(seat), seat.job);
                            return false;
                        }
                        self.world.sleep(delay.wait().as_secs_f64()).await;
                    }
                }
            }
        }
        true
    }

    /// Every seat once, and social told when the set of live jobs changed.
    pub async fn tick(self: &Arc<Self>) {
        let live = self.world.live().await;
        if let Some(l) = &live {
            let changed = {
                let mut reported = self.reported_live.lock().unwrap();
                let changed = reported.as_ref() != Some(l);
                *reported = Some(l.clone());
                changed
            };
            if changed {
                let mut jobs: Vec<&String> = l.iter().collect();
                jobs.sort();
                self.world.up("live", json!({"jobs": jobs})).await;
            }
        }
        let keys: Vec<Key> = self.store.all().await.iter().map(Seat::key).collect();
        futures_util::future::join_all(keys.iter().map(|key| self.drive(key, live.as_ref()))).await;
    }

    /// One line for a hand-mounted chat's holder here.
    pub fn hand(&self, job: &str, line: &str) {
        self.world.hand(job, line);
    }

    /// A session started or stopped here, or a clock changed: look at every seat now.
    pub fn wake(&self) {
        self.wake.notify_one();
    }

    /// Seconds until the next moment a seat's own clock needs a look. A moment already past that
    /// `tick` did not clear is looked at again after `TICK_S`.
    async fn next_due(&self) -> Option<f64> {
        let now = self.world.now();
        let mut due: Option<f64> = None;
        for s in self.store.all().await {
            let at = match s.state {
                State::Starting => s.deadline,
                State::Empty => s.breaker.next_try.max(s.breaker.hold_until),
                State::Occupied => continue,
            };
            due = Some(due.map_or(at, |d| d.min(at)));
        }
        due.map(|at| (at - now).max(TICK_S as f64))
    }

    pub async fn tick_forever(self: Arc<Self>) {
        loop {
            self.tick().await;
            let wait = self.next_due().await.map(std::time::Duration::from_secs_f64);
            tokio::select! {
                _ = self.wake.notified() => {}
                _ = async { match wait { Some(w) => tokio::time::sleep(w).await, None => std::future::pending().await } } => {}
            }
        }
    }

    /// `mark_done` from `job`: the seats it marked.
    pub async fn mark_done(&self, job: &str) -> Vec<Seat> {
        let now = self.world.now();
        let mut out = vec![];
        for mut s in self.store.of_job(job).await {
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

    /// Make `job` the seat's agent, keeping its conversation.
    pub async fn adopt(&self, decl: &Decl, job: &str, session_name: &str) -> Seat {
        let mut seat = self.upsert(decl).await;
        let now = self.world.now();
        if !seat.job.is_empty() && seat.job != job {
            seat.previous = Previous { job: seat.job.clone(), node: seat.decl.node.clone(), adopted: seat.adopted };
        }
        seat.state = State::Occupied;
        seat.job = job.into();
        seat.adopted = true;
        seat.done_at = 0.0;
        // Every seat session so far started with Remote Control on, and adoption leaves it so.
        seat.remote_control = true;
        seat.rc_url = self.world.rc_url(job).await.unwrap_or_default();
        self.save(&seat).await;
        self.store.deliver_briefed(seat.account(), seat.channel(), job, now).await;
        let name = if session_name.is_empty() { seat.decl.name.clone() } else { session_name.to_string() };
        self.mounted(&seat, job, &name).await;
        seat
    }

    /// A hand mount let go of a declared seat: the seat is empty again.
    pub async fn released(&self, account: &str, channel: &str, job: &str) {
        if let Some(mut s) = self.store.get(account, channel).await {
            if s.job == job && s.state != State::Empty {
                s.previous = Previous { job: s.job.clone(), node: s.decl.node.clone(), adopted: s.adopted };
                s.state = State::Empty;
                s.job.clear();
                s.rc_url.clear();
                s.adopted = false;
                self.save(&s).await;
            }
        }
    }

    /// With the seats unpaused, one notice line per seat chat that got messages while paused.
    pub async fn unpause_summary(&self) {
        if self.paused() {
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
        let live = match self.world.remote_control(&seat.job, on, &Self::rc_name(&seat)).await {
            Ok(url) => {
                if on {
                    seat.rc_url = match url {
                        Some(u) => u,
                        None => self.world.rc_url(&seat.job).await.unwrap_or_default(),
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

    /// The pending lines of a seat.
    pub async fn queued(&self, account: &str, channel: &str) -> i64 {
        self.store.pending(account, channel).await.len() as i64
    }
}
