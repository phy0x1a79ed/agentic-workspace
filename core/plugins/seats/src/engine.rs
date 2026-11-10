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

use std::collections::HashMap;
use std::sync::{Arc, Mutex, RwLock};

use async_trait::async_trait;
use awm_plugin_sdk::push::Outcome;
use awm_plugin_sdk::Backoff;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::brief;
use crate::store::{Decl, Failure, Latest, Queued, Seat, Seed, State, Store};

/// Transitions one look at a seat may take before it waits for the next tick.
const STEPS_PER_LOOK: usize = 3;

/// How long a started agent has to come up.
pub const UP_S: f64 = 180.0;
/// How long one line is retried before the seat counts its agent lost.
pub const PUSH_RETRY_S: f64 = 120.0;
/// The shortest wait before a seat's overdue clock is looked at again.
pub const TICK_S: u64 = 15;
/// How long a seat's agent may go with no message and no turn before the seat stops it.
pub const IDLE_S: f64 = 4.0 * 3600.0;


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
    /// `reflection resume_other` on a job the seat stopped, typing `prompt`, then the switch
    /// `remote_control` applied to the session it comes back as.
    async fn resume(&self, job: &str, prompt: &str, remote_control: bool, name: &str) -> Started;
    /// `reflection stop_other`, or why it failed.
    async fn stop(&self, job: &str) -> Result<(), String>;
    /// `reflection message`: one line typed into `job`.
    async fn push(&self, job: &str, line: &str) -> Outcome;
    /// The live jobs here, or None when it cannot tell.
    async fn live(&self) -> Option<Agents>;
    /// Whether the seat's scope exists here, or None when it cannot tell.
    async fn scope_ready(&self, project: &str, scope: &str) -> Option<bool>;
    /// The transcript of a past job, for the next agent's brief.
    async fn transcript(&self, job: &str) -> Option<String>;
    /// `reflection remote_control` on `job`, or why it failed.
    async fn remote_control(&self, job: &str, on: bool, name: &str) -> Result<(), String>;
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

impl Live {
    /// How the seat event tells Ops the answer: `failed: <why>` when it failed.
    fn as_text(&self) -> String {
        match self {
            Self::None => "none".into(),
            Self::Applied => "applied".into(),
            Self::Failed(why) => format!("failed: {why}"),
        }
    }
}

type Key = (String, String);

/// What this node's session list says of one live job.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Agent {
    /// Its claude.ai link, empty while its Remote Control is off.
    pub link: String,
    pub busy: bool,
    /// When its session last changed state, in epoch seconds, or 0 when unknown. Once it waits,
    /// this is the end of its last turn.
    pub active_at: f64,
}

/// Each live job here.
pub type Agents = HashMap<String, Agent>;

/// Take the seat's Remote Control state from its live session. True when that changed the seat.
fn sync_rc(seat: &mut Seat, live: Option<&Agents>) -> bool {
    let Some(link) = live.and_then(|l| l.get(&seat.job)).map(|a| &a.link) else { return false };
    if seat.rc_url == *link && seat.remote_control != link.is_empty() {
        return false;
    }
    seat.remote_control = !link.is_empty();
    seat.rc_url = link.clone();
    true
}

pub struct Seats {
    pub store: Store,
    settings: RwLock<Settings>,
    world: Arc<dyn World>,
    locks: Mutex<HashMap<Key, Arc<tokio::sync::Mutex<()>>>>,
    reported_live: Mutex<Option<Vec<String>>>,
    /// When each occupied seat's agent goes idle, as last seen, for the tick's timer.
    idle_at: Mutex<HashMap<Key, f64>>,
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
        Arc::new(Self { store, settings: RwLock::new(settings), world, locks: Mutex::new(HashMap::new()), reported_live: Mutex::new(None), idle_at: Mutex::new(HashMap::new()), wake: tokio::sync::Notify::new() })
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
            self.report(s, None).await;
        }
    }

    /// Tell social what it shows of the seat. `switch` answers a Remote Control flip, which social
    /// passes on to Ops even when nothing changed.
    async fn report(&self, s: &Seat, switch: Option<&Live>) {
        let queued = self.queued(s.account(), s.channel()).await;
        let mut event = s.event(&crate::now_iso());
        if let Some(live) = switch {
            event["rc_live"] = json!(live.as_text());
        }
        self.world
            .up("seat", json!({"account": s.account(), "channel_id": s.channel(), "state": s.state.as_str(), "job": s.job, "remote_control": s.remote_control, "rc_url": s.rc_url, "public": s.public(queued), "event": event}))
            .await;
    }

    /// The session name a seat's agent gets: Remote Control writes each `/` as `-` (R146).
    /// The session name as a plain label, which `/rc` takes: each run of other characters as `-`.
    fn rc_name(s: &Seat) -> String {
        let mut out = String::new();
        for c in s.decl.name.chars() {
            if c.is_ascii_alphanumeric() || "._-".contains(c) {
                out.push(c);
            } else if !out.ends_with('-') {
                out.push('-');
            }
        }
        out.trim_matches(|c| "-._".contains(c)).chars().take(64).collect()
    }

    fn lock(&self, key: &Key) -> Arc<tokio::sync::Mutex<()>> {
        self.locks.lock().unwrap().entry(key.clone()).or_default().clone()
    }

    async fn notice(&self, text: &str) {
        self.world.up("notice", json!({"text": text})).await;
    }

    async fn ask(&self, kind: &str, text: &str) {
        self.world.up("ask", json!({"kind": kind, "text": text})).await;
    }

    async fn mounted(&self, s: &Seat, job: &str) {
        self.world.up("mount", json!({"account": s.account(), "channel_id": s.channel(), "channel_name": s.decl.channel_name, "job": job, "session_name": s.decl.name})).await;
    }

    async fn unbind(&self, seat: &Seat) {
        self.world.up("unmount", json!({"account": seat.account(), "channel_id": seat.channel(), "job": seat.job})).await;
    }

    async fn set_line(&self, job: &str, latest: Latest, now: f64) {
        self.world.up("line", json!({"job": job, "account": latest.account, "channel_id": latest.channel, "thread_id": latest.thread, "message_id": latest.message_id})).await;
        self.store.set_line(job, &latest, now).await;
    }

    /// The seat this row names, created or brought up to date from social's declaration.
    pub async fn upsert(&self, decl: &Decl) -> Seat {
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
    pub async fn drive(self: &Arc<Self>, key: &Key, live: Option<&Agents>) {
        let lock = self.lock(key);
        let _held = lock.lock().await;
        // A job started or adopted since `live` was read is not in it, and is not gone.
        let newer = match live {
            Some(l) => self.store.get(&key.0, &key.1).await.is_some_and(|s| !s.job.is_empty() && !l.contains_key(&s.job)),
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
    async fn step(self: &Arc<Self>, mut seat: Seat, live: Option<&Agents>) -> bool {
        let now = self.world.now();
        let pending = self.store.pending(seat.account(), seat.channel()).await;
        let gone = |s: &Seat| live.is_some_and(|l| !l.contains_key(&s.job));
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
                if !seat.job.is_empty() && live.is_some_and(|l| l.contains_key(&seat.job)) {
                    return self.up(seat, now, live).await;
                }
                if now >= seat.deadline {
                    let here = match live {
                        Some(l) => Some(l.clone()),
                        None => self.world.live().await,
                    };
                    match here {
                        // Cannot tell: a start is never given up on a guess.
                        None => return false,
                        Some(l) if !seat.job.is_empty() && l.contains_key(&seat.job) => return self.up(seat, now, Some(&l)).await,
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
                    self.lost(&mut seat, "ended").await;
                    return !pending.is_empty();
                }
                if sync_rc(&mut seat, live) {
                    self.save(&seat).await;
                }
                if !pending.is_empty() && !self.pump(&mut seat, &pending).await {
                    self.lost(&mut seat, "stopped taking messages").await;
                    return true;
                }
                self.stop_if_idle(seat, live, now).await
            }
        }
    }

    /// The started agent is up: its brief's lines are delivered.
    async fn up(&self, mut seat: Seat, now: f64, live: Option<&Agents>) -> bool {
        let briefed = self.store.pending(seat.account(), seat.channel()).await;
        if let Some(last) = briefed.iter().rev().find(|q| q.briefed_to == seat.job) {
            let latest = Latest { account: seat.account().into(), channel: seat.channel().into(), thread: last.thread_id.clone(), message_id: last.message_id.clone() };
            self.set_line(&seat.job, latest, now).await;
        }
        self.store.deliver_briefed(seat.account(), seat.channel(), &seat.job, now).await;
        seat.state = State::Occupied;
        seat.breaker.attempts.clear();
        sync_rc(&mut seat, live);
        self.save(&seat).await;
        true
    }

    /// Stop the seat's agent once it went `IDLE_S` with no message and no turn, else note when
    /// it would. Only an agent the seat started, and never mid-turn. True when it stopped.
    async fn stop_if_idle(&self, mut seat: Seat, live: Option<&Agents>, now: f64) -> bool {
        let Some(live) = live else { return false };
        let at = match live.get(&seat.job) {
            _ if seat.adopted => None,
            Some(a) if a.busy => Some(now + IDLE_S),
            Some(a) if a.active_at > 0.0 => Some(seat.last_message.max(a.active_at) + IDLE_S),
            _ => None,
        };
        {
            let mut idle_at = self.idle_at.lock().unwrap();
            match at {
                Some(at) => idle_at.insert(seat.key(), at),
                None => idle_at.remove(&seat.key()),
            };
        }
        if !at.is_some_and(|at| at <= now) {
            return false;
        }
        // A failed stop leaves the deadline past, so the next tick tries again.
        match self.world.stop(&seat.job).await {
            Ok(()) => {
                tracing::info!("seat {}: stopped its idle agent {}", label(&seat), seat.job);
                self.vacate(&mut seat, true).await;
                true
            }
            Err(why) => {
                tracing::info!("seat {}: stopping its idle agent {} failed: {why}", label(&seat), seat.job);
                false
            }
        }
    }

    /// The agent went without the seat stopping it: free the seat, with a notice unless the
    /// session was only adopted.
    async fn lost(&self, seat: &mut Seat, how: &str) {
        if !seat.adopted {
            self.notice(&format!("the agent of the seat {} ({}@{}) {how}. The next message starts a new one.", label(seat), seat.job, seat.decl.node)).await;
        }
        self.vacate(seat, false).await;
    }

    /// The agent is gone: free the seat and keep its lines for the next agent. `stopped` says
    /// the seat stopped it for idleness.
    async fn vacate(&self, seat: &mut Seat, stopped: bool) {
        if !seat.job.is_empty() {
            seat.previous = seat.as_previous(stopped);
            self.unbind(seat).await;
        }
        seat.state = State::Empty;
        seat.job.clear();
        seat.rc_url.clear();
        seat.adopted = false;
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
        // A resume that did not come up is not tried again.
        seat.previous.stopped = false;
        let failure = seat.breaker.fail(now);
        self.save(&seat).await;
        let reset = format!("`seats reset account={} channel={}` on {}", seat.account(), seat.channel(), seat.decl.node);
        match failure {
            Failure::Tripped => self.notice(&format!("the seat {} could not start an agent 4 times in an hour ({why}). It holds its messages for 6 hours, or until {reset}.", label(&seat))).await,
            Failure::First => self.notice(&format!("the seat {} could not start an agent ({why}). It tries again on its own, or now with {reset}.", label(&seat))).await,
            Failure::Again => {}
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
            missed: seat.missed,
        };
        let (model, effort) = if seat.decl.model.is_empty() { (&settings.model, &settings.effort) } else { (&seat.decl.model, &seat.decl.effort) };
        let mut spec = json!({
            "project": settings.project, "scope": seat.decl.scope, "name": seat.decl.name, "prompt": brief::brief(&facts, &lines),
            "trust": seat.decl.level, "model": model, "effort": effort, "remote_control": seat.remote_control,
            "pin": true,
        });
        let here = previous.as_ref().filter(|p| !p.adopted && p.node == seat.decl.node);
        // A host reboot can bring the old agent back after its seat emptied: the new one takes its place.
        if let Some(p) = here.filter(|p| !p.stopped) {
            spec["replaces"] = json!(p.job);
        }
        let resume = here.filter(|p| p.stopped).map(|p| p.job.clone());
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
        let started = match resume {
            Some(job) => match self.world.resume(&job, &format!("{}{}", brief::missed(seat.missed), lines.join("\n")), seat.remote_control, &Self::rc_name(&seat)).await {
                Started::Refused(why) => {
                    tracing::info!("seat {}: resuming {job} refused, starting afresh: {why}", label(&seat));
                    self.world.start(spec).await
                }
                started => started,
            },
            None => self.world.start(spec).await,
        };
        match started {
            Started::Job(job) => {
                seat.job = job.clone();
                seat.missed = 0;
                self.save(&seat).await;
                self.store.brief_to(ids, &job).await;
                self.mounted(&seat, &job).await;
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
            let mut jobs: Vec<String> = l.keys().cloned().collect();
            jobs.sort();
            let changed = {
                let mut reported = self.reported_live.lock().unwrap();
                let changed = reported.as_ref() != Some(&jobs);
                if changed {
                    *reported = Some(jobs.clone());
                }
                changed
            };
            if changed {
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
                State::Occupied => match self.idle_at.lock().unwrap().get(&s.key()) {
                    Some(at) => *at,
                    None => continue,
                },
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
    pub async fn adopt(&self, decl: &Decl, job: &str) -> Seat {
        let mut seat = self.upsert(decl).await;
        let now = self.world.now();
        if !seat.job.is_empty() && seat.job != job {
            seat.previous = seat.as_previous(false);
        }
        seat.state = State::Occupied;
        seat.job = job.into();
        seat.adopted = true;
        seat.rc_url.clear();
        if seat.remote_control {
            sync_rc(&mut seat, self.world.live().await.as_ref());
        }
        self.save(&seat).await;
        if !seat.remote_control {
            if let Err(why) = self.world.remote_control(job, false, &Self::rc_name(&seat)).await {
                tracing::info!("seat {}: Remote Control off for {job} failed: {why}", label(&seat));
            }
        }
        self.store.deliver_briefed(seat.account(), seat.channel(), job, now).await;
        self.mounted(&seat, job).await;
        seat
    }

    /// Social no longer declares the seat. A live agent keeps running, without the seat.
    pub async fn dropped(&self, account: &str, channel: &str) {
        let lock = self.lock(&(account.to_string(), channel.to_string()));
        let _held = lock.lock().await;
        self.store.delete(account, channel).await;
    }

    /// A hand mount let go of a declared seat: the seat is empty again.
    pub async fn released(&self, account: &str, channel: &str, job: &str) {
        if let Some(mut s) = self.store.get(account, channel).await {
            if s.job == job && s.state != State::Empty {
                s.previous = s.as_previous(false);
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
    }

    /// Set the seat's Remote Control switch, and switch its live session too. The row then holds
    /// what the session really is, and social hears the answer even when the row did not change.
    pub async fn set_remote_control(&self, account: &str, channel: &str, on: bool) -> Option<(Seat, Live)> {
        let key = (account.to_string(), channel.to_string());
        let lock = self.lock(&key);
        let _held = lock.lock().await;
        let mut seat = self.store.get(account, channel).await?;
        // The switch is what the seat's next start uses, whether or not a live session took it.
        seat.remote_control = on;
        seat.rc_url.clear();
        let live = if seat.job.is_empty() || !matches!(seat.state, State::Starting | State::Occupied) {
            Live::None
        } else {
            match self.world.remote_control(&seat.job, on, &Self::rc_name(&seat)).await {
                Ok(()) => {
                    sync_rc(&mut seat, self.world.live().await.as_ref());
                    Live::Applied
                }
                Err(why) => {
                    tracing::info!("seat {}: Remote Control {} for {} failed: {why}", label(&seat), if on { "on" } else { "off" }, seat.job);
                    Live::Failed(why)
                }
            }
        };
        self.store.save(&seat).await;
        self.report(&seat, Some(&live)).await;
        Some((seat, live))
    }

    /// The pending lines of a seat.
    pub async fn queued(&self, account: &str, channel: &str) -> i64 {
        self.store.pending(account, channel).await.len() as i64
    }
}
