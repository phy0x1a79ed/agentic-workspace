//! Reports each fork session's start, stop and rename to reflection `harness_event`, by diffing
//! `session.list` against what dsh last saw whenever the fork's `events.mux` stream or a verb's
//! own listing shows the set may have changed. Nothing polls.

use std::collections::BTreeMap;
use std::future::Future;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use awm_plugin_sdk::{CallOpts, Hub};
use futures_util::StreamExt;
use serde_json::{json, Value};
use tokio::sync::{mpsc, Notify};
use tokio_tungstenite::tungstenite::Message;

use crate::fork::Fork;
use crate::sessions::{list_all, row};
use crate::supervisor::listening;

/// The `harness_event` param that marks a reflection taking these events. Python reflection's
/// `harness_event` has none and writes any kind but `clear` into its ledger, so nothing is sent
/// until reflection's describe lists it.
pub const GATE_PARAM: &str = "session_kind";
/// How long one `harness_event` or `describe` call may take.
pub const SEND_TIMEOUT: Duration = Duration::from_secs(5);
/// What `session_kind` says of every fork session.
pub const SESSION_KIND: &str = "web";

/// The `session/<what>` stream frames that can add, drop or retitle a session.
const SET_CHANGES: [&str; 12] =
    ["subscribed", "created", "deleted", "removed", "renamed", "title", "archived", "unarchived", "forked", "list", "changed", "closed"];

/// reflection as the session events reach it: the hub in dsh, a fake in a test.
pub trait Reflection: Send + Sync + 'static {
    /// reflection's describe of `harness_event`.
    fn describe(&self) -> impl Future<Output = Result<Value, String>> + Send;
    fn harness_event(&self, event: Value) -> impl Future<Output = Result<Value, String>> + Send;
}

/// reflection through the gateway.
pub struct HubReflection(pub Hub);

impl Reflection for HubReflection {
    async fn describe(&self) -> Result<Value, String> {
        let opts = CallOpts { timeout: SEND_TIMEOUT, ..CallOpts::default() };
        self.0.call_with("reflection", "describe", json!({"verbs": ["harness_event"]}), &opts).await.map_err(|e| e.to_string())
    }

    async fn harness_event(&self, event: Value) -> Result<Value, String> {
        let opts = CallOpts { timeout: SEND_TIMEOUT, ..CallOpts::default() };
        self.0.call_with("reflection", "harness_event", event, &opts).await.map_err(|e| e.to_string())
    }
}

/// Whether a reflection `describe` answer lists the verb `harness_event` with a param named
/// [`GATE_PARAM`].
pub fn takes_session_events(describe: &Value) -> bool {
    let verbs = describe.get("verbs").and_then(Value::as_array).into_iter().flatten();
    verbs.filter(|v| v.get("verb").and_then(Value::as_str) == Some("harness_event")).any(|v| {
        let params = v.get("params").and_then(Value::as_array).into_iter().flatten();
        params.into_iter().any(|p| p.get("name").and_then(Value::as_str) == Some(GATE_PARAM))
    })
}

async fn gate<R: Reflection>(r: &R) -> bool {
    match r.describe().await {
        Ok(answer) => takes_session_events(&answer),
        Err(e) => {
            tracing::debug!("dsh: session events: reflection describe failed: {e}");
            false
        }
    }
}

/// Sends each event in order while the gate is open. A failed send is dropped, never spooled or
/// retried: a lifecycle kind is not a ticket.
pub async fn report<R: Reflection>(r: R, mut events: mpsc::UnboundedReceiver<Value>) {
    let mut open = gate(&r).await;
    while let Some(ev) = events.recv().await {
        // A closed gate is read again before each event, else a reflection that gains the param
        // after dsh booted would never be sent to.
        if !open {
            open = gate(&r).await;
            if !open {
                continue;
            }
        }
        let kind = ev.get("kind").and_then(Value::as_str).unwrap_or_default().to_string();
        let failed = match r.harness_event(ev).await {
            Ok(answer) if answer.get("ok") == Some(&json!(false)) => Some(answer.to_string()),
            Ok(_) => None,
            Err(e) => Some(e),
        };
        if let Some(why) = failed {
            tracing::debug!("dsh: session event {kind} dropped: {why}");
            open = gate(&r).await;
        }
    }
}

/// The event names a stream frame carries, at its top level, in `payload` and in `value`.
fn frame_names(frame: &Value) -> impl Iterator<Item = (&str, Option<&str>)> {
    [Some(frame), frame.get("payload"), frame.get("value")].into_iter().flatten().flat_map(|level| {
        let key = level.get("key").and_then(Value::as_str);
        ["type", "method", "event"].into_iter().filter_map(move |k| level.get(k).and_then(Value::as_str).map(|n| (n, key)))
    })
}

/// Whether a stream frame can add, drop or retitle a session. A turn's own traffic cannot, and
/// diffing on each of its chunks would list the sessions once per token.
pub fn can_change_the_set(frame: &Value) -> bool {
    frame_names(frame).any(|(name, key)| {
        if name == "session/projection" {
            return key == Some("title");
        }
        if name.starts_with("alpha-session/") {
            return true;
        }
        name.strip_prefix("session/").or_else(|| name.strip_prefix("session.")).is_some_and(|what| SET_CHANGES.contains(&what))
    })
}

#[derive(Clone)]
struct Seen {
    name: Value,
    cwd: Value,
}

#[derive(Default)]
struct State {
    /// None until the first listing, which is read silently.
    seen: Option<BTreeMap<String, Seen>>,
    /// The ticket of the newest listing applied: an older one finishing late is stale.
    applied: u64,
}

/// What dsh last saw of the fork's sessions, and the wakeups that make it look again.
pub struct Lifecycle {
    state: Mutex<State>,
    tickets: AtomicU64,
    out: mpsc::UnboundedSender<Value>,
    kick: Notify,
    dirty: Notify,
}

fn event(kind: &str, session_id: &str, s: &Seen) -> Value {
    json!({"kind": kind, "session_id": session_id, "name": s.name, "cwd": s.cwd, "session_kind": SESSION_KIND})
}

impl Lifecycle {
    /// A lifecycle and the events it emits, for [`report`] to send.
    pub fn new() -> (Arc<Self>, mpsc::UnboundedReceiver<Value>) {
        let (out, rx) = mpsc::unbounded_channel();
        let lc = Self { state: Mutex::default(), tickets: AtomicU64::new(0), out, kick: Notify::new(), dirty: Notify::new() };
        (Arc::new(lc), rx)
    }

    /// Taken before a listing starts, so listings apply in the order they began.
    pub fn ticket(&self) -> u64 {
        self.tickets.fetch_add(1, Ordering::SeqCst) + 1
    }

    /// A verb's full `session.list`: diff it, and since the fork answered, reopen the stream if
    /// it is closed.
    pub fn listed(&self, ticket: u64, items: &[Value]) {
        self.observe(ticket, items);
        self.kick();
    }

    /// The fork is up, or may be: reopen the stream if it is closed.
    pub fn kick(&self) {
        self.kick.notify_one();
    }

    fn emit(&self, kind: &str, session_id: &str, s: &Seen) {
        let _ = self.out.send(event(kind, session_id, s));
    }

    fn observe(&self, ticket: u64, items: &[Value]) {
        let now: BTreeMap<String, Seen> = items
            .iter()
            .filter_map(row)
            .filter_map(|r| {
                let sid = r["session_id"].as_str()?.to_string();
                Some((sid, Seen { name: r["name"].clone(), cwd: r["cwd"].clone() }))
            })
            .collect();
        let mut st = self.state.lock().unwrap();
        if ticket < st.applied {
            return;
        }
        st.applied = ticket;
        let Some(before) = st.seen.replace(now.clone()) else { return };
        for (sid, s) in &now {
            match before.get(sid) {
                None => self.emit("started", sid, s),
                Some(b) if b.name != s.name => self.emit("renamed", sid, s),
                Some(_) => {}
            }
        }
        for (sid, b) in before.iter().filter(|(sid, _)| !now.contains_key(*sid)) {
            self.emit("stopped", sid, b);
        }
    }

    /// The fork is down: each session it last showed stops, once.
    fn down(&self, ticket: u64) {
        let mut st = self.state.lock().unwrap();
        if ticket < st.applied {
            return;
        }
        st.applied = ticket;
        if let Some(seen) = st.seen.as_mut() {
            for (sid, s) in std::mem::take(seen) {
                self.emit("stopped", &sid, &s);
            }
        }
    }

    /// List and diff. A listing that fails while nothing listens on the fork's port is the fork
    /// going down.
    async fn refresh(&self, fork: &Fork) {
        let ticket = self.ticket();
        match list_all(fork).await {
            Ok(items) => self.observe(ticket, &items),
            Err(e) if !listening(fork.port).await => {
                tracing::debug!("dsh: session events: the fork is down: {e}");
                self.down(ticket);
            }
            Err(e) => tracing::debug!("dsh: session events: the fork did not list its sessions: {e}"),
        }
    }
}

/// Follow the fork's `events.mux` stream for as long as dsh runs, diffing on each open, on each
/// frame that can change the set, and on each close. A closed stream opens again on a kick.
pub async fn follow(lc: Arc<Lifecycle>, fork: Fork) {
    let (differ, at) = (lc.clone(), fork.clone());
    tokio::spawn(async move {
        loop {
            differ.dirty.notified().await;
            differ.refresh(&at).await;
        }
    });
    let url = format!("ws://127.0.0.1:{}/api/events.mux", fork.port);
    loop {
        match tokio_tungstenite::connect_async(url.as_str()).await {
            Ok((mut ws, _)) => {
                lc.refresh(&fork).await;
                while let Some(Ok(msg)) = ws.next().await {
                    match msg {
                        Message::Text(text) => {
                            if serde_json::from_str::<Value>(text.as_str()).is_ok_and(|v| can_change_the_set(&v)) {
                                lc.dirty.notify_one();
                            }
                        }
                        Message::Close(_) => break,
                        _ => {}
                    }
                }
                lc.refresh(&fork).await;
            }
            Err(e) => {
                tracing::debug!("dsh: session events: events.mux did not open: {e}");
                lc.refresh(&fork).await;
            }
        }
        lc.kick.notified().await;
    }
}

/// Start sending session events to `reflection` and following the fork's stream.
pub fn spawn<R: Reflection>(lc: Arc<Lifecycle>, events: mpsc::UnboundedReceiver<Value>, fork: Fork, reflection: R) {
    tokio::spawn(report(reflection, events));
    tokio::spawn(follow(lc, fork));
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fork::stub::{self, Down, Stub};
    use crate::sessions::tests::{default_items, recording, FakeProcs, FORK, R9};
    use crate::sessions::{Sessions, COMPACT_WAIT, POLL};
    use std::time::Instant;

    const OTHER_FORK: &str = "session-11111111-1111-4111-8111-111111111111";
    const OTHER_R9: &str = "ds11111111111141118111111111111111";

    #[derive(Default)]
    struct Inner {
        open: bool,
        failing: bool,
        describes: usize,
        sent: Vec<Value>,
    }

    #[derive(Clone, Default)]
    struct FakeReflection(Arc<Mutex<Inner>>);

    impl FakeReflection {
        fn open() -> Self {
            let r = Self::default();
            r.0.lock().unwrap().open = true;
            r
        }
        fn set(&self, f: impl FnOnce(&mut Inner)) {
            f(&mut self.0.lock().unwrap());
        }
        fn sent(&self) -> Vec<Value> {
            self.0.lock().unwrap().sent.clone()
        }
        fn describes(&self) -> usize {
            self.0.lock().unwrap().describes
        }
    }

    impl Reflection for FakeReflection {
        async fn describe(&self) -> Result<Value, String> {
            let mut g = self.0.lock().unwrap();
            g.describes += 1;
            let mut params = vec![json!({"name": "session_id"}), json!({"name": "kind"})];
            if g.open {
                params.push(json!({"name": GATE_PARAM}));
            }
            Ok(json!({"verbs": [{"verb": "harness_event", "params": params}]}))
        }

        async fn harness_event(&self, event: Value) -> Result<Value, String> {
            let mut g = self.0.lock().unwrap();
            g.sent.push(event);
            if g.failing {
                Err("reflection is down".into())
            } else {
                Ok(json!({"ok": true}))
            }
        }
    }

    struct World {
        stub: Stub,
        down: Down,
        port: u16,
        lc: Arc<Lifecycle>,
        reflection: FakeReflection,
        _dir: tempfile::TempDir,
    }

    async fn until(what: &str, f: impl Fn() -> bool) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !f() {
            assert!(Instant::now() < deadline, "timed out waiting for {what}");
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }

    impl World {
        async fn new(reflection: FakeReflection) -> Self {
            let stub = Stub::default();
            stub.with(|g| g.items = default_items());
            let (port, down) = stub::serve_until(stub.clone()).await;
            let (lc, rx) = Lifecycle::new();
            spawn(lc.clone(), rx, Fork::new(port), reflection.clone());
            let w = World { stub, down, port, lc, reflection, _dir: tempfile::tempdir().unwrap() };
            w.until_open().await;
            w
        }

        /// Wait until the stream is open and its opening diff has listed.
        async fn until_open(&self) {
            until("the stream to open and list", || self.stub.sockets() == 1 && self.lc.state.lock().unwrap().applied >= 1).await;
        }

        /// Push `frame` and wait for the diff it causes.
        async fn push(&self, frame: Value) {
            let lists = self.stub.count("session.list");
            self.stub.push(frame);
            until("the diff", || self.stub.count("session.list") > lists).await;
            let ticket = self.lc.tickets.load(Ordering::SeqCst);
            until("the diff to apply", || self.lc.state.lock().unwrap().applied >= ticket).await;
        }

        async fn until_sent(&self, n: usize) {
            until(&format!("{n} sent"), || self.reflection.sent().len() >= n).await;
        }

        fn sessions(&self) -> Sessions {
            let (reporter, _) = recording(self._dir.path().join("unsent.jsonl"), true);
            Sessions {
                fork: Fork::new(self.port),
                procs: Box::new(FakeProcs::default()),
                reporter,
                lifecycle: self.lc.clone(),
                compact_wait: COMPACT_WAIT,
                poll: POLL,
            }
        }
    }

    fn kinds(events: &[Value]) -> Vec<&str> {
        events.iter().map(|e| e["kind"].as_str().unwrap()).collect()
    }

    fn created() -> Value {
        json!({"type": "server-request", "method": "session/subscribed", "payload": {"type": "session/subscribed", "sessionId": OTHER_FORK, "lastSeq": 0}})
    }

    #[tokio::test]
    async fn the_set_at_boot_sends_nothing() {
        let w = World::new(FakeReflection::open()).await;
        w.push(created()).await;
        w.sessions().rows().await.unwrap();
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(w.reflection.sent().is_empty(), "{:?}", w.reflection.sent());
    }

    #[tokio::test]
    async fn a_created_session_is_started_with_the_event_body() {
        let w = World::new(FakeReflection::open()).await;
        w.stub.with(|g| g.items.push(json!({"sessionId": OTHER_FORK, "title": "fresh", "cwd": "/w/projects/p/s"})));
        w.push(created()).await;
        w.until_sent(1).await;
        assert_eq!(
            w.reflection.sent(),
            [json!({"kind": "started", "session_id": OTHER_R9, "name": "fresh", "cwd": "/w/projects/p/s", "session_kind": "web"})]
        );
    }

    #[tokio::test]
    async fn a_new_title_is_renamed() {
        let w = World::new(FakeReflection::open()).await;
        w.stub.with(|g| g.items[0]["title"] = json!("retitled"));
        w.push(json!({"method": "session/projection", "payload": {"type": "session/projection", "sessionId": FORK, "key": "title", "value": "retitled"}})).await;
        w.until_sent(1).await;
        let ev = &w.reflection.sent()[0];
        assert_eq!((ev["kind"].as_str(), ev["session_id"].as_str(), ev["name"].as_str()), (Some("renamed"), Some(R9), Some("retitled")));
        assert!(ev.get("rc_url").is_none() && ev.get("task_id").is_none());
    }

    #[tokio::test]
    async fn a_deleted_session_is_stopped() {
        let w = World::new(FakeReflection::open()).await;
        w.stub.with(|g| g.items.retain(|i| i["sessionId"] != FORK));
        w.push(json!({"type": "session/deleted", "sessionId": FORK})).await;
        w.until_sent(1).await;
        let ev = &w.reflection.sent()[0];
        assert_eq!((ev["kind"].as_str(), ev["session_id"].as_str()), (Some("stopped"), Some(R9)));
    }

    #[tokio::test]
    async fn a_verbs_own_listing_is_diffed() {
        let w = World::new(FakeReflection::open()).await;
        w.stub.with(|g| g.items.push(json!({"sessionId": OTHER_FORK})));
        w.sessions().rows().await.unwrap();
        w.until_sent(1).await;
        assert_eq!(kinds(&w.reflection.sent()), ["started"]);
    }

    #[tokio::test]
    async fn the_fork_going_down_stops_every_session_once() {
        let w = World::new(FakeReflection::open()).await;
        w.stub.with(|g| g.items.push(json!({"sessionId": OTHER_FORK})));
        w.sessions().rows().await.unwrap();
        w.until_sent(1).await;
        w.down.down();
        w.until_sent(3).await;
        let sent = w.reflection.sent();
        let mut stopped: Vec<&str> = sent[1..].iter().map(|e| e["session_id"].as_str().unwrap()).collect();
        stopped.sort();
        assert_eq!(kinds(&sent[1..]), ["stopped", "stopped"]);
        assert_eq!(stopped, [R9, OTHER_R9]);
        assert!(w.sessions().rows().await.is_err());
        w.lc.kick();
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert_eq!(w.reflection.sent().len(), 3, "a second look at a down fork stops nothing again");
    }

    #[tokio::test]
    async fn a_frame_of_a_turn_lists_nothing() {
        let w = World::new(FakeReflection::open()).await;
        let lists = w.stub.count("session.list");
        w.stub.push(json!({"method": "session/event", "payload": {"type": "session/event", "sessionId": FORK, "event": {"type": "assistant/chunk"}}}));
        w.stub.push(json!({"method": "session/projection", "payload": {"type": "session/projection", "key": "stats"}}));
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert_eq!(w.stub.count("session.list"), lists);
    }

    #[tokio::test]
    async fn a_closed_gate_sends_nothing_and_is_read_before_each_event() {
        let w = World::new(FakeReflection::default()).await;
        w.stub.with(|g| g.items.push(json!({"sessionId": OTHER_FORK})));
        w.push(created()).await;
        until("the gate read again", || w.reflection.describes() >= 2).await;
        assert!(w.reflection.sent().is_empty());
        w.reflection.set(|g| g.open = true);
        w.stub.with(|g| g.items.retain(|i| i["sessionId"] != OTHER_FORK));
        w.push(json!({"type": "session/removed"})).await;
        w.until_sent(1).await;
        assert_eq!(kinds(&w.reflection.sent()), ["stopped"]);
        assert_eq!(w.reflection.describes(), 3);
    }

    #[tokio::test]
    async fn a_failed_send_is_dropped_not_retried_or_spooled() {
        let w = World::new(FakeReflection::open()).await;
        w.reflection.set(|g| g.failing = true);
        w.stub.with(|g| g.items.push(json!({"sessionId": OTHER_FORK})));
        w.push(created()).await;
        w.until_sent(1).await;
        until("the gate read after the failure", || w.reflection.describes() >= 2).await;
        w.reflection.set(|g| g.failing = false);
        w.stub.with(|g| g.items[0]["title"] = json!("again"));
        w.push(json!({"type": "session/renamed"})).await;
        w.until_sent(2).await;
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert_eq!(kinds(&w.reflection.sent()), ["started", "renamed"]);
        assert!(!w._dir.path().join("unsent.jsonl").exists());
    }

    #[tokio::test]
    async fn a_stream_closed_while_the_fork_lives_opens_again_on_a_kick() {
        let w = World::new(FakeReflection::open()).await;
        w.stub.hang_up();
        until("the stream to close", || w.stub.sockets() == 0).await;
        w.stub.with(|g| g.items.push(json!({"sessionId": OTHER_FORK})));
        w.lc.kick();
        w.until_sent(1).await;
        assert_eq!(kinds(&w.reflection.sent()), ["started"]);
        until("the stream to reopen", || w.stub.sockets() == 1).await;
    }

    #[test]
    fn a_stale_listing_is_not_applied() {
        let (lc, mut rx) = Lifecycle::new();
        let (old, new) = (lc.ticket(), lc.ticket());
        lc.observe(new, &default_items());
        lc.observe(old, &[]);
        lc.observe(lc.ticket(), &default_items());
        assert!(rx.try_recv().is_err());
    }

    #[test]
    fn the_gate_reads_harness_events_session_kind_param() {
        let with = |params: Value| json!({"verbs": [{"verb": "harness_event", "params": params}]});
        assert!(takes_session_events(&with(json!([{"name": "kind"}, {"name": GATE_PARAM}]))));
        assert!(!takes_session_events(&with(json!([{"name": "kind"}]))));
        assert!(!takes_session_events(&json!({"verbs": [{"verb": "other", "params": [{"name": GATE_PARAM}]}]})));
        assert!(!takes_session_events(&json!({})));
    }

    #[test]
    fn set_changing_frames_are_told_from_a_turns_traffic() {
        assert!(can_change_the_set(&created()));
        assert!(can_change_the_set(&json!({"type": "item", "value": {"type": "emit", "event": "alpha-session/created"}})));
        assert!(can_change_the_set(&json!({"type": "session/projection", "key": "title"})));
        assert!(!can_change_the_set(&json!({"type": "session/projection", "key": "plan"})));
        assert!(!can_change_the_set(&json!({"type": "session/event", "event": {"type": "assistant/chunk"}})));
        assert!(!can_change_the_set(&json!({"type": "approval/requested", "sessionId": FORK})));
    }
}
