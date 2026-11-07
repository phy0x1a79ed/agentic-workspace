//! The seat transitions against a scripted world, and the no-drop property under faults.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use awm_plugin_sdk::push::Outcome;
use awm_trust::Level;
use serde_json::Value;

use super::*;
use crate::config::{AccountConfig, Notice, SeatChat, SeatRule, SeatsConfig};
use crate::connectors::{ChatKind, Inbound};
use crate::db::Db;

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
    briefs: HashMap<String, Vec<String>>,
    reacts: Vec<String>,
    said: Vec<(String, String)>,
    asked: Vec<(String, String)>,
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
    async fn start(&self, _node: &str, spec: Value) -> Started {
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
        w.briefs.insert(job.clone(), lines.clone());
        if !w.down_on_start {
            w.up.insert(job.clone());
            w.got.entry(job.clone()).or_default().extend(lines);
        }
        Started::Job(job)
    }
    async fn push(&self, _node: &str, job: &str, line: &str) -> Outcome {
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
        if line != RETIRE_LINE {
            w.got.entry(job.into()).or_default().push(text_of(line));
        }
        Outcome::Delivered
    }
    async fn live(&self, _node: &str) -> Option<HashSet<String>> {
        let w = self.w();
        (!w.cannot_tell).then(|| w.up.clone())
    }
    async fn scope_ready(&self, _n: &str, _p: &str, _s: &str) -> Option<bool> {
        Some(!self.w().scope_missing)
    }
    async fn transcript(&self, _node: &str, job: &str) -> Option<String> {
        Some(format!("/transcripts/{job}.jsonl"))
    }
    async fn react(&self, _a: &str, _c: &str, id: &str, _e: &str) {
        self.w().reacts.push(id.into());
    }
    async fn say(&self, _a: &str, channel: &str, _t: Option<&str>, text: &str) -> bool {
        self.w().said.push((channel.into(), text.into()));
        true
    }
    async fn ask(&self, kind: &str, text: &str) {
        self.w().asked.push((kind.into(), text.into()));
    }
    async fn tag(&self, _n: &str, _d: &str) {}
}

fn cfg(paused: bool) -> SeatsConfig {
    SeatsConfig {
        paused,
        notice: Some(Notice { account: "bot".into(), channel: "N0TEST".into() }),
        rules: vec![SeatRule { account: "opal".into(), kind: "dm".into(), node: "altair".into(), level: Level::Cooperative, scope: "seats/{slug}".into(), except: vec!["USLACKBOT".into()] }],
        chats: vec![SeatChat { account: "opal".into(), channel: "C0TEST".into(), node: "altair".into(), level: Level::Cooperative, scope: "seats/test-lab-general".into() }],
        ..SeatsConfig::default()
    }
}

fn accounts() -> Vec<AccountConfig> {
    vec![AccountConfig { name: "opal".into(), platform: "slack".into(), trust: "cooperative".into(), ..Default::default() }]
}

struct T {
    _tmp: tempfile::TempDir,
    db: Db,
    world: Fake,
    seats: Arc<Seats>,
    n: u32,
}

impl T {
    fn new(paused: bool) -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let db = Db::open(&tmp.path().join("social.db")).unwrap();
        let world = Fake::default();
        world.w().now = 1_000_000.0;
        let seats = Seats::new(db.clone(), cfg(paused), accounts(), Arc::new(world.clone()));
        Self { _tmp: tmp, db, world, seats, n: 0 }
    }

    /// social restarted: a new engine over the same database.
    fn restart(&mut self, paused: bool) {
        self.seats = Seats::new(self.db.clone(), cfg(paused), accounts(), Arc::new(self.world.clone()));
    }

    fn dm(&mut self, text: &str) -> Inbound {
        self.n += 1;
        Inbound { account: "opal".into(), platform: "slack".into(), channel_id: "D0TEST".into(), kind: ChatKind::Dm, sender_id: "U0TEST".into(), sender_name: "tester".into(), text: text.into(), message_id: format!("{}.0", self.n), to_holder: true, ..Default::default() }
    }

    async fn send(&mut self, text: &str) -> Routed {
        let m = self.dm(text);
        let r = self.seats.route(&m).await;
        self.settle().await;
        r
    }

    async fn settle(&self) {
        for _ in 0..20 {
            tokio::task::yield_now().await;
        }
        self.seats.tick().await;
    }

    async fn seat(&self) -> Seat {
        self.seats.store.get("opal", "D0TEST").await.unwrap()
    }

    async fn tick(&self) {
        self.seats.tick().await;
    }
}

#[tokio::test]
async fn a_message_to_an_empty_seat_reacts_queues_and_starts_one_agent() {
    let mut t = T::new(false);
    t.world.w().down_on_start = true;
    assert_eq!(t.send("first").await, Routed::Seat);
    let s = t.seat().await;
    assert_eq!((s.state, s.job.as_str(), s.scope.as_str()), (State::Starting, "job-1", "seats/tester"));
    assert_eq!(t.world.w().reacts, ["1.0"], "one 👀, on the cold start");
    let spec = t.world.w().specs[0].clone();
    assert_eq!((spec["project"].as_str(), spec["trust"].as_str(), spec["name"].as_str()), (Some("agentic-commons"), Some("cooperative"), Some("seat:slack/dm/tester")));
    assert!(spec["prompt"].as_str().unwrap().contains("[slack/DM/tester, cooperative] first"));
    assert_eq!(t.seats.queued("opal", "D0TEST").await, 1, "briefed is not delivered");
    assert_eq!(t.send("second").await, Routed::Seat);
    assert_eq!(t.world.w().specs.len(), 1, "a starting seat starts nothing more");
    assert_eq!(t.world.w().reacts.len(), 1);

    t.world.w().up.insert("job-1".into());
    t.seats.touch("altair", "job-1").await;
    assert_eq!(t.seat().await.state, State::Occupied);
    assert_eq!(t.world.got("job-1"), ["second"], "the brief held the first, the push the second");
    assert_eq!(t.seats.queued("opal", "D0TEST").await, 0);
    let mount = t.db.holder("opal", "D0TEST").await.unwrap();
    assert_eq!((mount.node.as_str(), mount.job.as_str()), ("altair", "job-1"));
    assert_eq!(t.seats.store.line("altair", "job-1").await.unwrap().3, "2.0", "send defaults to the latest line");
}

#[tokio::test]
async fn a_start_that_never_comes_up_requeues_and_trips_the_breaker() {
    let mut t = T::new(false);
    t.world.w().down_on_start = true;
    t.send("hello").await;
    for round in 1..=4 {
        t.world.advance(UP_S + 1.0);
        t.tick().await;
        let s = t.seat().await;
        assert_eq!(s.state, State::Empty, "round {round}");
        assert_eq!(t.seats.queued("opal", "D0TEST").await, 1, "the line waits");
        assert!(t.db.holder("opal", "D0TEST").await.is_none());
        if round < 4 {
            t.world.advance(store::BACKOFF_S[round - 1] + 1.0);
            t.tick().await;
            assert_eq!(t.seat().await.state, State::Starting, "retried after the backoff");
        }
    }
    let s = t.seat().await;
    assert!(s.breaker.held(t.world.now()));
    assert!(t.world.w().said.iter().any(|(c, x)| c == "N0TEST" && x.contains("could not start an agent 4 times")));
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
    assert_eq!(t.seat().await.state, State::Occupied);
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
    let s = t.seat().await;
    assert_eq!(s.job, "job-2");
    assert_eq!(t.world.got("job-2"), ["two"]);
}

#[tokio::test]
async fn done_then_quiet_retires_and_a_message_while_retiring_gets_one_reply() {
    let mut t = T::new(false);
    t.send("task").await;
    assert_eq!(t.seats.mark_done("altair", "job-1").await.len(), 1);
    t.world.advance(DONE_S - 10.0);
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Occupied);
    t.world.advance(20.0);
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Retiring);
    t.send("new one").await;
    t.send("and another").await;
    let said: Vec<String> = t.world.w().said.iter().filter(|(c, _)| c == "D0TEST").map(|(_, x)| x.clone()).collect();
    assert_eq!(said, [SHIFT_REPLY], "one reply per retiring");
    assert_eq!(t.seat().await.state, State::Retiring);
    t.world.advance(RETIRE_S + 1.0);
    t.tick().await;
    t.tick().await;
    let s = t.seat().await;
    assert_eq!((s.state, s.job.as_str()), (State::Occupied, "job-2"), "the next agent starts beside the old one");
    assert!(t.world.w().specs[1]["prompt"].as_str().unwrap().contains("/transcripts/job-1.jsonl"));
    assert!(t.world.w().specs[1].get("replaces").is_none());
    assert_eq!(t.world.got("job-2"), ["new one", "and another"]);
    assert_eq!(t.db.holder("opal", "D0TEST").await.unwrap().job, "job-2", "the old agent is unbound");
}

#[tokio::test]
async fn a_retiring_agent_that_leaves_empties_the_seat_and_a_new_message_clears_done() {
    let mut t = T::new(false);
    t.send("task").await;
    t.seats.mark_done("altair", "job-1").await;
    t.send("more").await;
    assert_eq!(t.seat().await.done_at, 0.0, "a new message clears the mark");
    t.seats.mark_done("altair", "job-1").await;
    t.world.advance(DONE_S + 1.0);
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Retiring);
    t.world.w().up.remove("job-1");
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Empty);
    assert!(t.world.w().specs.len() == 1, "nothing waits, so nothing starts");
    assert!(!t.world.w().said.iter().any(|(_, x)| x.contains("ended before")), "a done agent leaving is no outage");
}

#[tokio::test]
async fn an_agent_that_dies_before_done_gets_a_notice_and_no_restart() {
    let mut t = T::new(false);
    t.send("task").await;
    t.world.w().up.remove("job-1");
    t.tick().await;
    assert_eq!(t.seat().await.state, State::Empty);
    assert!(t.world.w().said.iter().any(|(c, x)| c == "N0TEST" && x.contains("ended before its task was done")));
    assert_eq!(t.world.w().specs.len(), 1);
    t.send("back?").await;
    assert_eq!(t.seat().await.job, "job-2");
}

#[tokio::test]
async fn after_a_restart_waiting_lines_go_to_the_live_agent() {
    let mut t = T::new(false);
    t.send("one").await;
    let m = t.dm("two");
    t.seats.store.enqueue("opal", "D0TEST", &m.message_id, "", &line::push_line(&m, "cooperative"), t.world.now()).await;
    t.restart(false);
    t.tick().await;
    assert_eq!(t.world.got("job-1"), ["one", "two"]);
    assert_eq!(t.world.w().specs.len(), 1);
}

#[tokio::test]
async fn a_message_routes_once() {
    let mut t = T::new(false);
    let m = t.dm("once");
    assert_eq!(t.seats.route(&m).await, Routed::Seat);
    assert_eq!(t.seats.route(&m).await, Routed::Duplicate);
    t.settle().await;
    assert_eq!(t.world.texts(), ["once"]);
}

#[tokio::test]
async fn paused_seats_record_and_start_nothing_and_unpause_lists_the_chats() {
    let mut t = T::new(true);
    t.send("while paused").await;
    t.send("again").await;
    assert_eq!(t.seat().await.state, State::Empty);
    assert!(t.world.w().specs.is_empty() && t.world.w().reacts.is_empty());
    assert_eq!(t.seats.queued("opal", "D0TEST").await, 0, "nothing to replay");
    t.restart(false);
    t.seats.unpause_summary().await;
    let notice = t.world.w().said.last().unwrap().1.clone();
    assert!(notice.contains("opal/DM tester: 2 messages") && !notice.contains("while paused"));
    t.tick().await;
    assert!(t.world.w().specs.is_empty(), "unpause starts nothing by itself");
    assert_eq!(t.seat().await.missed, 0);
}

#[tokio::test]
async fn a_paused_occupied_seat_still_delivers() {
    let mut t = T::new(true);
    let seat = t.seats.declared("opal", "D0TEST", &line::Place { platform: "slack".into(), kind: ChatKind::Dm, person: "tester".into(), ..Default::default() }).await.unwrap();
    t.world.w().up.insert("kalen".into());
    t.seats.adopt(seat, "altair", "kalen", "").await;
    t.send("to the adopted seat").await;
    assert_eq!(t.world.got("kalen"), ["to the adopted seat"]);
}

#[tokio::test]
async fn an_outside_sender_is_held_until_admitted() {
    let mut t = T::new(false);
    let mut m = t.dm("from outside");
    m.outside = true;
    t.seats.route(&m).await;
    t.settle().await;
    assert!(t.world.w().specs.is_empty());
    assert!(t.world.w().asked.iter().any(|(k, x)| k == "blocked" && x.contains("seat_admit")));
    t.seats.admit("opal", "D0TEST").await;
    assert_eq!(t.world.texts(), ["from outside"]);
}

#[tokio::test]
async fn a_new_dm_files_a_review_card_and_slackbot_is_no_seat() {
    let mut t = T::new(false);
    t.send("hi").await;
    assert!(t.world.w().asked.iter().any(|(k, _)| k == "review"));
    let mut bot = t.dm("reminder");
    bot.channel_id = "D0BOT".into();
    bot.sender_id = "USLACKBOT".into();
    assert_eq!(t.seats.route(&bot).await, Routed::NotSeat);
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

#[tokio::test]
async fn live_mounts_on_declared_chats_become_adopted_seats() {
    let t = T::new(true);
    let dm = |ch: &str, job: &str| crate::db::Mount { account: "opal".into(), channel_id: ch.into(), channel_name: "".into(), node: "altair".into(), job: job.into(), session_name: "s".into(), mounted_at: "".into() };
    let place = |kind| line::Place { platform: "slack".into(), kind, person: "tester".into(), chat_name: "general".into(), ..Default::default() };
    t.db.mount(dm("D0TEST", "patrik")).await.unwrap();
    t.db.mount(dm("C0OTHER", "hand")).await.unwrap();
    let n = t.seats.adopt_mounts(vec![(dm("D0TEST", "patrik"), place(ChatKind::Dm)), (dm("C0OTHER", "hand"), place(ChatKind::Channel))]).await;
    assert_eq!(n, 1, "only the declared chat is a seat");
    let s = t.seat().await;
    assert_eq!((s.state, s.job.as_str(), s.adopted), (State::Occupied, "patrik", true));
    assert!(t.seats.store.get("opal", "C0OTHER").await.is_none(), "an undeclared mount stays a hand mount");
    assert_eq!(t.seats.adopt_mounts(vec![(dm("D0TEST", "patrik"), place(ChatKind::Dm))]).await, 0, "once");
}

#[tokio::test]
async fn a_forward_queues_a_line_and_grants_a_day_of_reads() {
    let mut t = T::new(false);
    let target = t.seats.declared("opal", "C0TEST", &line::Place { platform: "slack".into(), server: "Test Lab".into(), kind: ChatKind::Channel, chat_name: "general".into(), ..Default::default() }).await.unwrap();
    t.seats.forward(target, ("opal", "D0TEST"), "[forwarded] look at this").await;
    for _ in 0..20 {
        tokio::task::yield_now().await;
    }
    t.tick().await;
    assert_eq!(t.seats.store.get("opal", "C0TEST").await.unwrap().state, State::Occupied);
    let now = t.world.now();
    assert_eq!(t.seats.store.reads(vec![("opal".into(), "C0TEST".into())], "opal", now).await, ["D0TEST"]);
    assert!(t.seats.store.reads(vec![("opal".into(), "C0TEST".into())], "opal", now + READ_S + 1.0).await.is_empty());
    let _ = t.dm("x");
}

/// A replay of the lost DM of 2026-11-07: the seat's agent had gone, the push was refused as
/// gone, and the Python dropped the line. Here the line starts the next agent.
#[tokio::test]
async fn the_undelivered_dm_of_2026_11_07_reaches_an_agent() {
    let mut t = T::new(false);
    let seat = t.seats.declared("opal", "D0TEST", &line::Place { platform: "slack".into(), kind: ChatKind::Dm, person: "tester".into(), ..Default::default() }).await.unwrap();
    t.seats.adopt(seat, "altair", "hand-started", "").await;
    t.send("are you there?").await;
    assert_eq!(t.world.got("job-1"), ["are you there?"]);
    assert_eq!(t.world.w().reacts.len(), 1);
}

/// No line is lost or given twice, through refused starts, refused and lost pushes, agents
/// that die, "cannot tell", and restarts of social.
#[tokio::test]
async fn no_line_is_lost_or_doubled_under_faults() {
    let mut rng_state: u64 = 0x9e37_79b9_7f4a_7c15;
    let mut rnd = move |n: u64| {
        rng_state ^= rng_state << 13;
        rng_state ^= rng_state >> 7;
        rng_state ^= rng_state << 17;
        rng_state % n
    };
    for _round in 0..12 {
        let mut t = T::new(false);
        let mut sent = vec![];
        for i in 0..40 {
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
            if t.seats.queued("opal", "D0TEST").await == 0 && t.seat().await.state == State::Occupied {
                break;
            }
        }
        let mut got = t.world.texts();
        got.sort();
        assert_eq!(got, sent, "every line once");
    }
}
