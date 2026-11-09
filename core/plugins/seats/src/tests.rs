//! The seat transitions against a scripted world, and the link through a social that can drop.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use awm_plugin_sdk::push::Outcome;
use serde_json::{json, Value};

use crate::engine::*;
use crate::link::{Alcor, Flushed, Link};
use crate::store::{self, Decl, Seat, Seed, State, Store};

#[derive(Default)]
struct W {
    now: f64,
    jobs: u32,
    /// What the next starts answer, else a job that comes up.
    starts: VecDeque<Started>,
    /// Jobs that run.
    up: HashSet<String>,
    /// Jobs started but never up.
    down_on_start: bool,
    /// What the next pushes answer, else delivered while the job runs.
    pushes: VecDeque<Outcome>,
    cannot_tell: bool,
    scope_missing: bool,
    specs: Vec<Value>,
    /// Every line each job got, from its brief or a push.
    got: HashMap<String, Vec<String>>,
    reacts: Vec<String>,
    said: Vec<(String, String)>,
    asked: Vec<(String, String)>,
    /// `channel -> job` from the mount and unmount rows.
    mounted: HashMap<String, String>,
    latest: HashMap<String, String>,
    hands: Vec<(String, String)>,
    live_reports: Vec<Value>,
    seat_reports: Vec<Value>,
    /// Each `remote_control` call: job, on, name.
    switched: Vec<(String, bool, String)>,
    switch_fails: bool,
}

#[derive(Clone, Default)]
struct Fake(Arc<Mutex<W>>);

impl Fake {
    fn w(&self) -> std::sync::MutexGuard<'_, W> {
        self.0.lock().unwrap()
    }
    fn advance(&self, s: f64) {
        self.w().now += s;
    }
    fn got(&self, job: &str) -> Vec<String> {
        self.w().got.get(job).cloned().unwrap_or_default()
    }
    fn texts(&self) -> Vec<String> {
        let w = self.w();
        let mut all: Vec<String> = w.got.values().flatten().cloned().collect();
        all.sort();
        all
    }
    fn noticed(&self) -> bool {
        self.w().said.iter().any(|(c, _)| c == "NOTICE")
    }
}

fn text_of(line: &str) -> String {
    line.rsplit("] ").next().unwrap_or(line).to_string()
}

#[async_trait]
impl World for Fake {
    fn now(&self) -> f64 {
        self.w().now
    }
    async fn sleep(&self, secs: f64) {
        self.advance(secs);
    }
    async fn start(&self, spec: Value) -> Started {
        let mut w = self.w();
        w.specs.push(spec.clone());
        if let Some(s) = w.starts.pop_front() {
            if let Started::Job(j) = &s {
                w.up.insert(j.clone());
            }
            return s;
        }
        w.jobs += 1;
        let job = format!("job-{}", w.jobs);
        let lines: Vec<String> = spec["prompt"].as_str().unwrap().lines().filter(|l| l.starts_with('[')).map(text_of).collect();
        if !w.down_on_start {
            w.up.insert(job.clone());
            w.got.entry(job.clone()).or_default().extend(lines);
        }
        Started::Job(job)
    }
    async fn push(&self, job: &str, line: &str) -> Outcome {
        let mut w = self.w();
        if let Some(o) = w.pushes.pop_front() {
            if o == Outcome::Delivered {
                w.got.entry(job.into()).or_default().push(text_of(line));
            }
            return o;
        }
        if !w.up.contains(job) {
            return Outcome::Gone;
        }
        w.got.entry(job.into()).or_default().push(text_of(line));
        Outcome::Delivered
    }
    async fn live(&self) -> Option<HashSet<String>> {
        let w = self.w();
        (!w.cannot_tell).then(|| w.up.clone())
    }
    async fn scope_ready(&self, _p: &str, _s: &str) -> Option<bool> {
        Some(!self.w().scope_missing)
    }
    async fn transcript(&self, job: &str) -> Option<String> {
        Some(format!("/transcripts/{job}.jsonl"))
    }
    async fn remote_control(&self, job: &str, on: bool, name: &str) -> Result<Option<String>, String> {
        let mut w = self.w();
        if w.switch_fails {
            return Err("no such session".into());
        }
        w.switched.push((job.into(), on, name.into()));
        Ok(on.then(|| format!("https://claude.ai/code/{job}")))
    }
    async fn rc_url(&self, job: &str) -> Option<String> {
        self.w().up.contains(job).then(|| format!("https://claude.ai/code/{job}"))
    }
    fn hand(&self, job: &str, line: &str) {
        self.w().hands.push((job.into(), line.into()));
    }
    async fn up(&self, kind: &str, b: Value) {
        let text = |k: &str| b[k].as_str().unwrap_or("").to_string();
        let mut w = self.w();
        match kind {
            "react" => w.reacts.push(text("message_id")),
            "send" => w.said.push((text("channel"), text("text"))),
            "notice" => w.said.push(("NOTICE".into(), text("text"))),
            "ask" => w.asked.push((text("kind"), text("text"))),
            "mount" => {
                w.mounted.insert(text("channel_id"), text("job"));
            }
            "unmount" => {
                w.mounted.remove(&text("channel_id"));
            }
            "line" => {
                w.latest.insert(text("job"), text("message_id"));
            }
            "live" => w.live_reports.push(b),
            "seat" => w.seat_reports.push(b),
            _ => {}
        }
    }
}

fn settings(paused: bool) -> Settings {
    Settings { paused, project: "agentic-commons".into(), model: "opus".into(), effort: "high".into(), docs: "/docs".into() }
}

fn decl() -> Decl {
    Decl {
        account: "opal".into(),
        channel_id: "D0TEST".into(),
        channel_name: "tester".into(),
        kind: "dm".into(),
        slug: "tester".into(),
        node: "altair".into(),
        scope: "seats/tester".into(),
        level: "cooperative".into(),
        platform: "slack".into(),
        server_label: "slack".into(),
        chat_label: "DM".into(),
        brief: "briefs/slack-slack.md".into(),
        name: "seat:slack/dm/tester".into(),
        ..Default::default()
    }
}

struct T {
    _tmp: tempfile::TempDir,
    store: Store,
    world: Fake,
    seats: Arc<Seats>,
    n: i64,
}

impl T {
    fn new(paused: bool) -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open(&tmp.path().join("seats.db")).unwrap();
        let world = Fake::default();
        world.w().now = 1_000_000.0;
        let seats = Seats::new(store.clone(), settings(paused), Arc::new(world.clone()));
        Self { _tmp: tmp, store, world, seats, n: 0 }
    }

    /// The plugin restarted: a new engine over the same database.
    fn restart(&mut self, paused: bool) {
        self.seats = Seats::new(self.store.clone(), settings(paused), Arc::new(self.world.clone()));
    }

    fn dm(&mut self, text: &str) -> Incoming {
        self.n += 1;
        Incoming { id: self.n, seat: decl(), message_id: format!("{}.0", self.n), line: format!("[slack/DM/tester, cooperative] {text}"), ..Default::default() }
    }

    async fn send(&mut self, text: &str) {
        let m = self.dm(text);
        self.seats.take(m).await;
        self.settle().await;
    }

    async fn settle(&self) {
        for _ in 0..20 {
            tokio::task::yield_now().await;
        }
        self.seats.tick().await;
    }

    async fn seat(&self) -> Seat {
        self.store.get("opal", "D0TEST").await.unwrap()
    }

    async fn tick(&self) {
        self.seats.tick().await;
    }

    async fn queued(&self) -> i64 {
        self.seats.queued("opal", "D0TEST").await
    }
}

#[tokio::test]
async fn a_message_to_an_empty_seat_reacts_queues_and_starts_one_agent() {
    let mut t = T::new(false);
    t.world.w().down_on_start = true;
    t.send("first").await;
    let s = t.seat().await;
    assert_eq!((s.state, s.job.as_str()), (State::Starting, "job-1"));
    assert_eq!(t.world.w().reacts, ["1.0"], "one 👀, on the cold start");
    let spec = t.world.w().specs[0].clone();
    assert_eq!((spec["project"].as_str(), spec["trust"].as_str(), spec["name"].as_str()), (Some("agentic-commons"), Some("cooperative"), Some("seat:slack/dm/tester")));
    let prompt = spec["prompt"].as_str().unwrap();
    assert!(prompt.contains("[slack/DM/tester, cooperative] first") && prompt.contains("/docs/briefs/slack-slack.md") && prompt.contains("`seats send text=<text>`"));
    assert_eq!(t.queued().await, 1, "briefed is not delivered");
    t.send("second").await;
    assert_eq!(t.world.w().specs.len(), 1, "a starting seat starts nothing more");
    assert_eq!(t.world.w().reacts.len(), 1);

    t.world.w().up.insert("job-1".into());
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Occupied);
    assert_eq!(t.world.got("job-1"), ["second"], "the brief held the first, the push the second");
    assert_eq!(t.queued().await, 0);
    assert_eq!(t.world.w().mounted["D0TEST"], "job-1");
    assert_eq!(t.store.line("job-1").await.unwrap().message_id, "2.0", "send defaults to the latest line");
    assert_eq!(t.world.w().latest["job-1"], "2.0", "social hears it too");
}

#[tokio::test]
async fn a_seat_with_its_own_model_and_effort_starts_with_them() {
    let mut t = T::new(false);
    t.send("first").await;
    let spec = t.world.w().specs[0].clone();
    assert_eq!((spec["model"].as_str(), spec["effort"].as_str()), (Some("opus"), Some("high")), "no override takes [seats]");

    let mut t = T::new(false);
    let mut m = t.dm("first");
    (m.seat.model, m.seat.effort) = ("sonnet".into(), "medium".into());
    t.seats.take(m).await;
    t.settle().await;
    let spec = t.world.w().specs[0].clone();
    assert_eq!((spec["model"].as_str(), spec["effort"].as_str()), (Some("sonnet"), Some("medium")));
    assert_eq!(t.seat().await.decl.model, "sonnet", "the override is stored");
}

#[test]
fn a_database_from_before_the_seat_override_opens_with_it_empty() {
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("seats.db");
    drop(Store::open(&path).unwrap());
    let c = rusqlite::Connection::open(&path).unwrap();
    c.execute_batch("ALTER TABLE seat_registry DROP COLUMN model; ALTER TABLE seat_registry DROP COLUMN effort; INSERT INTO seat_registry (account, channel_id) VALUES ('opal', 'D1')").unwrap();
    drop(c);
    drop(Store::open(&path).unwrap());
    let c = rusqlite::Connection::open(&path).unwrap();
    let model: String = c.query_row("SELECT model FROM seat_registry WHERE channel_id = 'D1'", [], |r| r.get(0)).unwrap();
    assert_eq!(model, "");
}

#[tokio::test]
async fn a_start_that_never_comes_up_requeues_and_trips_the_breaker() {
    let mut t = T::new(false);
    t.world.w().down_on_start = true;
    t.send("hello").await;
    for round in 1..=4 {
        t.world.advance(UP_S + 1.0);
        t.tick().await;
        assert_eq!(t.seat().await.state, State::Empty, "round {round}");
        assert_eq!(t.queued().await, 1, "the line waits");
        assert!(!t.world.w().mounted.contains_key("D0TEST"));
        if round < 4 {
            t.world.advance(store::BACKOFF_S[round - 1] + 1.0);
            t.tick().await;
            assert_eq!(t.seat().await.state, State::Starting, "retried after the backoff");
        }
    }
    assert!(t.seat().await.breaker.held(t.world.now()));
    assert!(t.world.w().said.iter().any(|(c, x)| c == "NOTICE" && x.contains("could not start an agent 4 times") && x.contains("seats reset")));
    t.world.advance(3600.0);
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Empty, "held");
    t.world.w().down_on_start = false;
    t.seats.reset("opal", "D0TEST").await;
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Occupied);
    assert_eq!(t.world.texts(), ["hello"]);
}

#[tokio::test]
async fn cannot_tell_never_gives_up_a_start_or_empties_a_seat() {
    let mut t = T::new(false);
    t.world.w().down_on_start = true;
    t.send("hello").await;
    t.world.w().cannot_tell = true;
    t.world.advance(UP_S * 10.0);
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Starting);
    t.world.w().up.insert("job-1".into());
    t.world.w().cannot_tell = false;
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Occupied);
    t.world.w().cannot_tell = true;
    t.world.w().up.clear();
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Occupied);
}

#[tokio::test]
async fn a_gone_push_returns_its_lines_and_starts_the_next_agent_with_them() {
    let mut t = T::new(false);
    t.send("one").await;
    t.world.w().up.remove("job-1");
    t.send("two").await;
    t.send("three").await;
    let s = t.seat().await;
    assert_eq!((s.state, s.job.as_str()), (State::Occupied, "job-2"));
    assert_eq!(t.world.got("job-2"), ["two", "three"]);
    assert!(t.world.w().specs[1]["prompt"].as_str().unwrap().contains("job job-1 on altair"));
    assert_eq!(t.world.texts(), ["one", "three", "two"]);
}

#[tokio::test]
async fn a_refused_push_past_the_window_counts_the_agent_lost() {
    let mut t = T::new(false);
    t.send("one").await;
    t.world.w().pushes.extend(std::iter::repeat_n(Outcome::Retry("busy".into()), 100));
    t.send("two").await;
    t.world.w().pushes.clear();
    t.tick().await;
    assert_eq!(t.seat().await.job, "job-2");
    assert_eq!(t.world.got("job-2"), ["two"]);
}

#[tokio::test]
async fn a_seat_keeps_its_agent_however_long_the_chat_is_quiet() {
    let mut t = T::new(false);
    t.send("task").await;
    assert_eq!(t.world.w().specs[0]["pin"], true, "a seat's agent is a persistent session");
    assert!(t.world.w().specs[0].get("replaces").is_none());
    t.seats.mark_done("job-1").await;
    t.send("more").await;
    assert_eq!(t.seat().await.done_at, 0.0, "a new message clears the mark");
    t.seats.mark_done("job-1").await;
    let lines = t.world.got("job-1");
    t.world.advance(30.0 * 86400.0);
    t.tick().await;
    let s = t.seat().await;
    assert_eq!((s.state, s.job.as_str()), (State::Occupied, "job-1"));
    assert_eq!(t.world.got("job-1"), lines, "nothing tells the agent to leave");

    t.world.w().up.remove("job-1");
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Empty);
    assert_eq!(t.world.w().specs.len(), 1, "nothing waits, so nothing starts");
    assert!(!t.world.w().said.iter().any(|(_, x)| x.contains("ended before")), "a done agent leaving is no outage");
    t.send("back").await;
    t.tick().await;
    assert_eq!(t.world.w().specs[1]["replaces"], "job-1", "an agent a reboot brought back gives way");

    t.world.w().up.insert("hand-started".into());
    t.seats.adopt(&decl(), "hand-started", "").await;
    t.world.w().up.remove("hand-started");
    t.tick().await;
    t.send("again").await;
    t.tick().await;
    assert!(t.world.w().specs[2].get("replaces").is_none(), "a session the seat only adopted is never stopped");
}

#[tokio::test]
async fn a_session_event_wakes_the_loop_with_no_timer_behind_it() {
    let mut t = T::new(false);
    t.send("task").await;
    let looping = tokio::spawn(t.seats.clone().tick_forever());
    t.world.w().up.remove("job-1");
    t.seats.wake();
    for _ in 0..200 {
        if t.seat().await.state == State::Empty {
            break;
        }
        tokio::task::yield_now().await;
    }
    assert_eq!(t.seat().await.state, State::Empty, "the stop was seen on the event");
    looping.abort();
}

#[tokio::test]
async fn an_agent_that_dies_before_done_gets_a_notice_and_no_restart() {
    let mut t = T::new(false);
    t.send("task").await;
    t.world.w().up.remove("job-1");
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Empty);
    assert!(t.world.w().said.iter().any(|(c, x)| c == "NOTICE" && x.contains("ended before its task was done")));
    assert_eq!(t.world.w().specs.len(), 1);
    t.send("back?").await;
    assert_eq!(t.seat().await.job, "job-2");
}

#[tokio::test]
async fn after_a_restart_waiting_lines_go_to_the_live_agent() {
    let mut t = T::new(false);
    t.send("one").await;
    let m = t.dm("two");
    t.store.enqueue(Some(m.id), "opal", "D0TEST", &m.message_id, "", &m.line, t.world.now()).await;
    t.restart(false);
    t.tick().await;
    assert_eq!(t.world.got("job-1"), ["one", "two"]);
    assert_eq!(t.world.w().specs.len(), 1);
}

#[tokio::test]
async fn paused_seats_record_and_start_nothing_and_going_on_lists_the_chats() {
    let mut t = T::new(true);
    t.send("while paused").await;
    t.send("again").await;
    assert_eq!(t.seat().await.state, State::Empty);
    assert!(t.world.w().specs.is_empty() && t.world.w().reacts.is_empty());
    assert_eq!(t.queued().await, 0, "nothing to replay");
    t.seats.configure(settings(false)).await;
    let notice = t.world.w().said.last().unwrap().1.clone();
    assert!(notice.contains("opal/DM tester: 2 messages") && !notice.contains("while paused"));
    t.tick().await;
    assert!(t.world.w().specs.is_empty(), "going on starts nothing by itself");
    assert_eq!(t.seat().await.missed, 0);
    t.seats.configure(settings(false)).await;
    assert_eq!(t.world.w().said.len(), 1, "the same settings post nothing");
}

#[tokio::test]
async fn an_outside_sender_is_held_until_admitted() {
    let mut t = T::new(false);
    let mut m = t.dm("from outside");
    m.outside = "stranger".into();
    t.seats.take(m).await;
    t.settle().await;
    assert!(t.world.w().specs.is_empty());
    assert!(t.world.w().asked.iter().any(|(k, x)| k == "blocked" && x.contains("seats admit")));
    t.seats.admit("opal", "D0TEST").await;
    assert_eq!(t.world.texts(), ["from outside"]);
}

#[tokio::test]
async fn a_missing_scope_holds_the_lines_and_asks() {
    let mut t = T::new(false);
    t.world.w().scope_missing = true;
    t.send("hi").await;
    assert_eq!(t.seat().await.held, "scope");
    assert!(t.world.w().asked.iter().any(|(k, x)| k == "blocked" && x.contains("no scope")));
    t.tick().await;
    assert!(t.world.w().specs.is_empty());
    t.world.w().scope_missing = false;
    t.seats.reset("opal", "D0TEST").await;
    assert_eq!(t.world.texts(), ["hi"]);
}

/// A replay of the lost DM of 2026-11-07: the seat's agent had gone, the push was refused as
/// gone, and the Python dropped the line. Here the line starts the next agent.
#[tokio::test]
async fn the_undelivered_dm_of_2026_11_07_reaches_an_agent() {
    let mut t = T::new(false);
    t.seats.adopt(&decl(), "hand-started", "").await;
    t.send("are you there?").await;
    assert_eq!(t.world.got("job-1"), ["are you there?"]);
    assert_eq!(t.world.w().reacts.len(), 1);
}

/// No line is lost or given twice, through refused starts, refused and lost pushes, agents
/// that die, "cannot tell", and restarts of the plugin.
#[tokio::test]
async fn no_line_is_lost_or_doubled_under_faults() {
    let mut rng_state: u64 = 0x9e37_79b9_7f4a_7c15;
    let mut rnd = move |n: u64| {
        rng_state ^= rng_state << 13;
        rng_state ^= rng_state >> 7;
        rng_state ^= rng_state << 17;
        rng_state % n
    };
    for _round in 0..3 {
        let mut t = T::new(false);
        t.send("m00").await;
        let mut sent = vec!["m00".to_string()];
        for i in 1..40 {
            match rnd(10) {
                0 => t.world.w().starts.push_back(Started::Refused("daemon down".into())),
                1 => t.world.w().down_on_start = rnd(2) == 0,
                2 => {
                    let s = t.seat().await;
                    if s.state == State::Occupied {
                        t.world.w().up.remove(&s.job);
                    }
                }
                3 => t.world.w().pushes.push_back(Outcome::Retry("busy".into())),
                4 => t.world.w().cannot_tell = rnd(2) == 0,
                5 => t.restart(false),
                6 => t.world.advance(rnd(400) as f64),
                _ => {}
            }
            let text = format!("m{i:02}");
            t.send(&text).await;
            sent.push(text);
            if rnd(3) == 0 {
                t.tick().await;
            }
        }
        {
            let mut w = t.world.0.lock().unwrap();
            w.starts.clear();
            w.pushes.clear();
            w.down_on_start = false;
            w.cannot_tell = false;
        }
        for _ in 0..40 {
            t.world.advance(store::HOLD_S / 4.0);
            t.tick().await;
            if t.queued().await == 0 && t.seat().await.state == State::Occupied {
                break;
            }
        }
        let mut got = t.world.texts();
        got.sort();
        assert_eq!(got, sent, "every line once");
    }
}

#[tokio::test]
async fn a_seat_starts_off_and_its_agent_follows_the_switch() {
    let mut t = T::new(false);
    t.send("first").await;
    assert_eq!(t.world.w().specs[0]["remote_control"], false, "a seat starts with Remote Control off");
    let (s, live) = t.seats.set_remote_control("opal", "D0TEST", true).await.unwrap();
    assert_eq!(live, Live::Applied);
    assert_eq!(t.world.w().switched, [("job-1".to_string(), true, "seat-slack-dm-tester".to_string())], "the session name as a plain label");
    assert_eq!(s.rc_url, "https://claude.ai/code/job-1");
    t.seats.set_remote_control("opal", "D0TEST", false).await.unwrap();
    assert_eq!(t.seat().await.rc_url, "", "off clears the link");
    t.seats.set_remote_control("opal", "D0TEST", true).await.unwrap();
    t.world.w().up.clear();
    t.tick().await;
    let s = t.seat().await;
    assert_eq!((s.state, s.remote_control, s.rc_url.as_str()), (State::Empty, true, ""), "the switch holds across an empty spell");
    t.send("again").await;
    assert_eq!(t.world.w().specs.last().unwrap()["remote_control"], true, "the next agent starts with it");
    assert_eq!(t.seat().await.rc_url, "https://claude.ai/code/job-2", "an agent that comes up on reports its link");
}

#[tokio::test]
async fn an_empty_seat_switches_without_a_call_and_a_failed_live_switch_keeps_the_setting() {
    let mut t = T::new(true);
    t.send("while paused").await;
    let (_, live) = t.seats.set_remote_control("opal", "D0TEST", true).await.unwrap();
    assert_eq!(live, Live::None);
    assert!(t.world.w().switched.is_empty(), "no session to switch");
    assert!(t.seats.set_remote_control("opal", "D0NONE", true).await.is_none(), "no such seat");
    let mut t = T::new(false);
    t.send("first").await;
    t.world.w().switch_fails = true;
    let (s, live) = t.seats.set_remote_control("opal", "D0TEST", true).await.unwrap();
    assert_eq!(live, Live::Failed("no such session".into()));
    assert!(s.remote_control && t.seat().await.remote_control);
}

#[tokio::test]
async fn adoption_switches_remote_control_off_and_a_paused_occupied_seat_still_delivers() {
    let mut t = T::new(true);
    t.world.w().up.insert("agent-a".into());
    t.seats.adopt(&decl(), "agent-a", "s").await;
    let s = t.seat().await;
    assert_eq!((s.state, s.adopted, s.remote_control, s.rc_url.as_str()), (State::Occupied, true, false, ""));
    assert_eq!(t.world.w().switched, [("agent-a".to_string(), false, "seat-slack-dm-tester".to_string())], "the live session follows the switch");
    t.send("to the adopted seat").await;
    assert_eq!(t.world.got("agent-a"), ["to the adopted seat"]);
}

#[tokio::test]
async fn a_new_session_reports_the_live_jobs_once_per_change() {
    let mut t = T::new(false);
    t.send("first").await;
    t.tick().await;
    t.tick().await;
    assert_eq!(t.world.w().live_reports.last().unwrap()["jobs"], json!(["job-1"]));
    let n = t.world.w().live_reports.len();
    t.tick().await;
    assert_eq!(t.world.w().live_reports.len(), n, "no change, no row");
}

// -- moving state from social ----------------------------------------------------------

fn seed(job: &str, job_node: &str) -> Seed {
    Seed { state: "occupied".into(), job: job.into(), job_node: job_node.into(), adopted: 1, remote_control: 1, rc_url: "u".into(), previous: r#"{"job":"old","node":"altair"}"#.into(), breaker: r#"{"hold_until":5.0}"#.into(), missed: 2, ..Default::default() }
}

#[tokio::test]
async fn a_seed_keeps_a_live_agent_in_its_seat_and_never_overwrites_one() {
    let t = T::new(true);
    t.world.w().up.insert("j1".into());
    t.seats.seed(decl(), seed("j1", "altair")).await;
    let s = t.seat().await;
    assert_eq!((s.state, s.job.as_str(), s.adopted, s.remote_control, s.missed, s.previous.job.as_str(), s.breaker.hold_until), (State::Occupied, "j1", true, true, 2, "old", 5.0));
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Occupied, "the agent is live, so nothing changes");
    assert!(!t.world.noticed() && t.world.w().specs.is_empty());
    t.seats.seed(decl(), seed("other", "altair")).await;
    assert_eq!(t.seat().await.job, "j1", "a replayed seed changes nothing");
}

#[tokio::test]
async fn a_line_that_waited_through_the_move_survives_the_pause() {
    let mut t = T::new(true);
    let mut m = t.dm("waited");
    m.kept = true;
    t.seats.take(m).await;
    t.settle().await;
    assert_eq!((t.seat().await.missed, t.queued().await), (0, 1), "it waits instead of counting as missed");
    assert!(t.world.w().specs.is_empty(), "and starts nothing while paused");
    t.seats.configure(settings(false)).await;
    t.tick().await;
    assert_eq!(t.world.texts(), ["waited"]);
}

#[tokio::test]
async fn a_seed_with_a_job_on_another_node_starts_empty_with_it_as_predecessor() {
    let t = T::new(true);
    t.seats.seed(decl(), seed("512feb43", "deneb")).await;
    let s = t.seat().await;
    assert_eq!((s.state, s.job.as_str(), s.previous.job.as_str(), s.previous.node.as_str(), s.rc_url.as_str()), (State::Empty, "", "512feb43", "deneb", ""));
}

// -- the link ----------------------------------------------------------------------------

/// social as the host sees it, with a line that can drop.
#[derive(Default)]
struct S {
    down: bool,
    /// Answer the next pull with an error after social has handled it.
    lose_pull: bool,
    lose_report: bool,
    retry_reports: bool,
    rows: Vec<Value>,
    next: i64,
    acked: i64,
    /// `(epoch, id, kind)` of what was applied, in order.
    applied: Vec<(String, i64, String)>,
    mark: HashMap<String, i64>,
    paused: bool,
}

#[derive(Clone, Default)]
struct Social(Arc<Mutex<S>>);

impl Social {
    fn s(&self) -> std::sync::MutexGuard<'_, S> {
        self.0.lock().unwrap()
    }

    fn line(&self, text: &str) {
        let mut s = self.s();
        s.next += 1;
        let id = s.next;
        s.rows.push(json!({"id": id, "kind": "line", "seat": {"account": "opal", "channel_id": "D0TEST", "kind": "dm", "slug": "tester", "node": "altair", "scope": "seats/tester", "level": "cooperative", "name": "seat:slack/dm/tester"},
            "message_id": format!("{id}.0"), "thread_id": "", "line": format!("[x] {text}"), "outside": "", "new": false}));
    }
}

#[async_trait]
impl Alcor for Social {
    async fn pull(&self, ack_through: i64, _wait_s: u64) -> Result<Value, String> {
        let mut s = self.s();
        if s.down {
            return Err("no route to host".into());
        }
        s.acked = s.acked.max(ack_through);
        let rows: Vec<Value> = s.rows.iter().filter(|r| r["id"].as_i64().unwrap() > ack_through).cloned().collect();
        if std::mem::take(&mut s.lose_pull) {
            return Err("the answer was lost".into());
        }
        Ok(json!({"rows": rows, "settings": {"paused": s.paused, "project": "agentic-commons", "model": "opus", "effort": "high", "docs": "/docs"}}))
    }

    async fn report(&self, row: Value) -> Result<Value, String> {
        let mut s = self.s();
        if s.down {
            return Err("no route to host".into());
        }
        if s.retry_reports {
            return Ok(json!({"ok": false, "error": "slack is down", "retry": true}));
        }
        let (epoch, id) = (row["epoch"].as_str().unwrap().to_string(), row["id"].as_i64().unwrap());
        if id > s.mark.get(&epoch).copied().unwrap_or(0) {
            s.applied.push((epoch.clone(), id, row["kind"].as_str().unwrap().to_string()));
            s.mark.insert(epoch, id);
        }
        if std::mem::take(&mut s.lose_report) {
            return Err("the answer was lost".into());
        }
        Ok(json!({"ok": true, "ts": "9.9"}))
    }
}

struct L {
    t: T,
    social: Social,
    link: Arc<Link>,
}

fn wall() -> f64 {
    1_000_000.0
}

fn attached(t: &T, social: &Social, epoch: &str) -> Arc<Link> {
    let link = Link::new(Arc::new(social.clone()), t.store.clone(), epoch.into());
    link
}

impl L {
    /// A seat with a live agent, through the link's first pull.
    async fn occupied() -> Self {
        let t = T::new(false);
        let social = Social::default();
        let link = attached(&t, &social, "e1");
        social.line("hello");
        link.pull_once(&t.seats).await.unwrap();
        t.settle().await;
        assert_eq!(t.world.got("job-1"), ["hello"]);
        assert_eq!(t.seat().await.state, State::Occupied);
        Self { t, social, link }
    }

    async fn settle_pull(&self) {
        let _ = self.link.pull_once(&self.t.seats).await;
        self.t.settle().await;
    }

    async fn drain(&self) {
        while self.link.flush_once().await == Flushed::Sent {}
    }
}

#[tokio::test]
async fn a_link_loss_loses_no_agent_no_line_and_no_reply_and_says_nothing() {
    let l = L::occupied().await;
    l.link.enqueue("send", &json!({"text": "before"})).await;
    l.drain().await;

    l.social.s().down = true;
    assert!(l.link.pull_once(&l.t.seats).await.is_err());
    assert_eq!(l.link.status().await["connected"], false);
    l.t.world.advance(420.0);
    l.t.tick().await;
    let s = l.t.seat().await;
    assert_eq!((s.state, s.job.as_str(), s.breaker.attempts.len()), (State::Occupied, "job-1", 0), "the agent's life is read on this node");
    assert!(!l.t.world.noticed(), "a reset says nothing");
    assert_eq!(l.t.world.w().specs.len(), 1, "and starts nothing");

    l.link.enqueue("send", &json!({"text": "during"})).await;
    let id = l.t.store.outbox_first().await.unwrap().id;
    assert!(matches!(l.link.flush_once().await, Flushed::Retry(_)));
    assert_eq!(l.link.status().await["outbox"], 1, "the reply waits");
    l.social.line("while down");
    l.social.line("and another");

    l.social.s().down = false;
    l.settle_pull().await;
    assert_eq!(l.t.world.got("job-1"), ["hello", "while down", "and another"], "in order");
    l.drain().await;
    let applied = l.social.s().applied.clone();
    assert_eq!(applied.iter().filter(|(_, i, _)| *i == id).count(), 1, "the reply goes out once");
    assert_eq!(l.link.status().await["connected"], true);
}

#[tokio::test]
async fn a_lost_pull_answer_resends_and_a_row_stored_twice_is_taken_once() {
    let l = L::occupied().await;
    l.social.line("one");
    l.social.s().lose_pull = true;
    assert!(l.link.pull_once(&l.t.seats).await.is_err());
    assert_eq!(l.t.world.got("job-1"), ["hello"], "it never arrived");
    l.settle_pull().await;
    assert_eq!(l.t.world.got("job-1"), ["hello", "one"]);
    l.settle_pull().await;
    assert_eq!(l.t.world.got("job-1"), ["hello", "one"], "the next pull acked it");
    assert_eq!(l.social.s().acked, 2);
}

#[tokio::test]
async fn a_lost_report_answer_is_resent_and_applied_once() {
    let l = L::occupied().await;
    l.drain().await;
    let before = l.social.s().applied.len();
    l.link.enqueue("send", &json!({"text": "x"})).await;
    l.social.s().lose_report = true;
    assert!(matches!(l.link.flush_once().await, Flushed::Retry(_)));
    assert_eq!(l.link.flush_once().await, Flushed::Sent, "the row stayed and is sent again");
    assert_eq!(l.social.s().applied.len(), before + 1);
}

#[tokio::test]
async fn a_host_restart_takes_an_unacked_row_once() {
    let mut l = L::occupied().await;
    l.social.line("one");
    l.link.pull_once(&l.t.seats).await.unwrap();
    l.t.settle().await;
    l.t.restart(false);
    l.link = attached(&l.t, &l.social, "e1");
    l.link.pull_once(&l.t.seats).await.unwrap();
    l.t.settle().await;
    assert_eq!(l.t.world.got("job-1"), ["hello", "one"]);
    let rows = l.social.s().rows.len();
    assert_eq!(rows, 2);
}

#[tokio::test]
async fn a_row_social_keeps_refusing_stops_blocking_the_queue_after_an_hour() {
    let l = L::occupied().await;
    l.drain().await;
    l.social.s().retry_reports = true;
    l.link.enqueue("send", &json!({"text": "new"})).await;
    assert!(matches!(l.link.flush_once().await, Flushed::Retry(_)), "a new row waits");
    l.t.store.outbox_delete(l.t.store.outbox_first().await.unwrap().id).await;
    l.t.store.outbox_push("send", &json!({"text": "old"}), wall() - 7200.0).await;
    assert_eq!(l.link.flush_once().await, Flushed::Sent, "an old row gives up");
    assert_eq!(l.t.store.outbox_len().await, 0);
}

#[tokio::test]
async fn a_hand_row_is_typed_and_paused_arrives_with_the_pull() {
    let l = L::occupied().await;
    {
        let mut s = l.social.s();
        s.next += 1;
        let id = s.next;
        s.rows.push(json!({"id": id, "kind": "hand", "job": "h1", "line": "[hand] hi"}));
        s.paused = true;
    }
    l.link.pull_once(&l.t.seats).await.unwrap();
    assert_eq!(l.t.world.w().hands, [("h1".to_string(), "[hand] hi".to_string())]);
    assert!(l.t.seats.settings().paused);
}
