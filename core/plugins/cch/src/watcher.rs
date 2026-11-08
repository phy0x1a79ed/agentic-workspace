//! Waits for an injected command to start running, by reading the session's own record and transcript.
//!
//! It uses no transport: the record has the same fields whether the pty belongs to a tmux pane or
//! the daemon, and one watcher cannot drift from itself the way the two per-transport ones did.

use std::future::Future;
use std::time::Duration;

use awm_harness::claude::home::ClaudeHome;
use awm_plugin_sdk::procfs;
use serde_json::Value;

use crate::transcript::{self, Tail};

/// The complete set of statuses Claude Code writes. Listed for the reader: the logic keys off
/// [`SETTLED`] alone, so an unrecognised value keeps the watcher waiting instead of firing.
pub const KNOWN_STATUSES: [&str; 4] = ["busy", "shell", "idle", "waiting"];

/// Statuses that are settled with no corroboration. `waiting` is a blocking dialog, not the end of
/// a turn, and resuming into it pastes a prompt behind a modal that swallows it.
pub const SETTLED: [&str; 1] = ["idle"];

/// How long a `shell` must sit unchanged before the transcript gets a say: long enough to outlast
/// the busy/shell flapping of a live turn, well inside a compaction.
pub const SHELL_SETTLE_S: f64 = 20.0;

pub const POLL_S: f64 = 2.0;
/// The poll interval until the session is seen to react: a fast command could otherwise start and
/// finish between two samples.
pub const FAST_POLL_S: f64 = 0.3;
/// Consecutive settled samples before the resume fires: cheap insurance against a status flap.
pub const SETTLE_POLLS: u32 = 3;
/// Hard cap on one round of waiting. Reaching it is a refusal to deliver, not a licence to deliver
/// anyway: a resume typed before the session reaches its command would run first, on the old
/// context. The caller holds its promise and re-arms the wait.
pub const MAX_WAIT_S: f64 = 900.0;

/// How a wait ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    /// The command is running, so anything typed now lands on the other side of it.
    Started,
    /// The session is between turns.
    Settled,
    /// The process is gone: there is nobody left to resume.
    Vanished,
    /// Neither, after [`MAX_WAIT_S`]: the caller must hold its promise rather than deliver.
    TimedOut,
}

impl Outcome {
    /// The Python outcome string: `started`, `settled`, `vanished` or `timed-out`.
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::Started => "started",
            Outcome::Settled => "settled",
            Outcome::Vanished => "vanished",
            Outcome::TimedOut => "timed-out",
        }
    }
}

/// What the watcher asks of a transcript reader; [`Tail`] is the real one.
///
/// Every answer is tri-state, and only a definite value counts: `None` keeps the watcher waiting.
pub trait TranscriptView: Send {
    fn poll(&mut self) -> impl Future<Output = bool> + Send;
    fn watch(&mut self, text: &str);
    fn tool_call_in_flight(&self) -> Option<bool>;
    fn queued(&self, text: &str) -> Option<bool>;
    fn consumed(&self, text: &str) -> Option<bool>;
    fn started(&self, text: &str, since_ms: Option<i64>) -> Option<bool>;
    fn compacted(&self, since_ms: Option<i64>) -> Option<bool>;
}

impl TranscriptView for Tail {
    fn poll(&mut self) -> impl Future<Output = bool> + Send {
        Tail::poll(self)
    }
    fn watch(&mut self, text: &str) {
        Tail::watch(self, text)
    }
    fn tool_call_in_flight(&self) -> Option<bool> {
        Tail::tool_call_in_flight(self)
    }
    fn queued(&self, text: &str) -> Option<bool> {
        Tail::queued(self, text)
    }
    fn consumed(&self, text: &str) -> Option<bool> {
        Tail::consumed(self, text)
    }
    fn started(&self, text: &str, since_ms: Option<i64>) -> Option<bool> {
        Tail::started(self, text, since_ms)
    }
    fn compacted(&self, since_ms: Option<i64>) -> Option<bool> {
        Tail::compacted(self, since_ms)
    }
}

/// The session facts the watcher reads besides the transcript; [`LiveProbe`] is the real one.
pub trait Probe: Send {
    /// `(status, statusUpdatedAt)` from the session record; `None` when it could not be read this
    /// time, which is not the same as the session being gone.
    fn read_status(&mut self, pid: u32) -> impl Future<Output = Option<(String, i64)>> + Send;
    /// The process's start time, or `None` when no process holds `pid`. Liveness is judged here,
    /// never by the record.
    fn proc_start(&mut self, pid: u32) -> impl Future<Output = Option<String>> + Send;
    /// Wall-clock epoch ms, the scale `statusUpdatedAt` is on.
    fn now_ms(&self) -> i64;
}

/// The real [`Probe`]: Claude Code's session record and `/proc`.
#[derive(Debug, Clone)]
pub struct LiveProbe {
    pub home: ClaudeHome,
}

impl Probe for LiveProbe {
    async fn read_status(&mut self, pid: u32) -> Option<(String, i64)> {
        read_status(&self.home, pid).await
    }
    async fn proc_start(&mut self, pid: u32) -> Option<String> {
        tokio::task::spawn_blocking(move || procfs::start_ticks(pid)).await.ok().flatten()
    }
    fn now_ms(&self) -> i64 {
        now_ms()
    }
}

/// Wall-clock epoch ms.
pub fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

fn status_number(v: Option<&Value>) -> i64 {
    match v {
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)).unwrap_or(0),
        Some(Value::String(s)) => s.trim().parse().unwrap_or(0),
        Some(Value::Bool(b)) => i64::from(*b),
        _ => 0,
    }
}

/// `(status, statusUpdatedAt)` from `pid`'s session record.
///
/// `None` means the record could not be read this time. The record is rewritten whenever a session
/// is attached or backgrounded, and a read landing inside that is a transient miss, not death.
pub async fn read_status(home: &ClaudeHome, pid: u32) -> Option<(String, i64)> {
    let record = transcript::read_record(&home.session_record(pid)).await?;
    let status = match record.get("status") {
        Some(Value::String(s)) => s.clone(),
        None | Some(Value::Null) | Some(Value::Bool(false)) => String::new(),
        Some(other) => other.to_string(),
    };
    Some((status, status_number(record.get("statusUpdatedAt"))))
}

/// Is `text` a `/compact`? Decides whether a boundary is about it.
pub fn is_compaction(text: &str) -> bool {
    text.split_whitespace().next() == Some("/compact")
}

/// Has the command injected at `injected_at_ms` begun running?
///
/// The start line naming it is the direct answer, and a `compact_boundary` the late one. Both are
/// read as "since we typed", never as "present": an earlier compaction left the same two lines.
pub fn has_started<T: TranscriptView>(tail: &T, text: &str, injected_at_ms: i64) -> bool {
    if text.is_empty() {
        return false;
    }
    if tail.started(text, Some(injected_at_ms)) == Some(true) {
        return true;
    }
    is_compaction(text) && tail.compacted(Some(injected_at_ms)) == Some(true)
}

/// Is this session between turns, and therefore safe to type a resume into?
///
/// `idle` says so outright. `shell` is worn both by a session running a foreground tool and by one
/// whose turn ended with a background task still running, so it needs three things to agree: the
/// status unchanged for [`SHELL_SETTLE_S`], no unanswered tool call at the transcript's tail, and
/// nothing of ours still queued. An unreadable transcript leaves the session unsettled.
pub fn is_settled<T: TranscriptView>(status: &str, updated_ms: i64, now_ms: i64, tail: &T, text: &str) -> bool {
    if SETTLED.contains(&status) {
        return true;
    }
    if status != "shell" {
        return false;
    }
    if ((now_ms - updated_ms) as f64) < SHELL_SETTLE_S * 1000.0 {
        return false;
    }
    if tail.tool_call_in_flight() != Some(false) {
        return false;
    }
    tail.queued(text) != Some(true)
}

/// Wait until the command `text` injected at `injected_at_ms` is safe to resume past.
///
/// [`has_started`] is the signal that fires first. The settled path survives for a transcript that
/// will not read and a non-compacting command that leaves no boundary: the session must have
/// reacted (its record moved after the injection, or the transcript shows the command consumed)
/// and then be settled for [`SETTLE_POLLS`] samples. A `waiting` status suppresses both, because a
/// blocking dialog swallows a paste. Open `tail` before injecting so nothing is missed.
pub async fn await_completion<T: TranscriptView, P: Probe>(
    repl_pid: u32,
    injected_at_ms: i64,
    label: &str,
    text: &str,
    tail: &mut T,
    probe: &mut P,
) -> Outcome {
    let who = if label.is_empty() { format!("pid {repl_pid}") } else { label.to_string() };
    if !text.is_empty() {
        tail.watch(text);
    }
    let deadline = tokio::time::Instant::now() + Duration::from_secs_f64(MAX_WAIT_S);
    let mut reacted = false;
    let mut settled_streak = 0;
    let mut unreadable = 0u32;
    let mut waiting_logged = false;
    let pause = |reacted: bool| Duration::from_secs_f64(if reacted { POLL_S } else { FAST_POLL_S });

    while tokio::time::Instant::now() < deadline {
        tail.poll().await;
        let Some((status, updated_ms)) = probe.read_status(repl_pid).await else {
            if probe.proc_start(repl_pid).await.is_none() {
                tracing::warn!("reflection: session {who} is gone; no resume injected");
                return Outcome::Vanished;
            }
            // Alive, but its record could not be read: most likely being rewritten right now.
            unreadable += 1;
            if unreadable == 1 || unreadable == 10 {
                tracing::info!(
                    "reflection: session {who} is alive but its record is briefly unreadable; still waiting"
                );
            }
            tokio::time::sleep(pause(reacted)).await;
            continue;
        };
        unreadable = 0;
        if status != "waiting" && has_started(tail, text, injected_at_ms) {
            tracing::info!(
                "reflection: session {who} has begun running the command it was sent; its resume goes in \
                 now, to be taken up on the other side of it"
            );
            return Outcome::Started;
        }
        if updated_ms > injected_at_ms || (!text.is_empty() && tail.consumed(text) == Some(true)) {
            reacted = true;
        }
        if reacted && is_settled(&status, updated_ms, probe.now_ms(), tail, text) {
            settled_streak += 1;
            if settled_streak >= SETTLE_POLLS {
                return Outcome::Settled;
            }
        } else {
            settled_streak = 0;
            if status == "waiting" && !waiting_logged {
                // A session blocked on a dialog stays blocked until a human answers it.
                tracing::info!(
                    "reflection: session {who} is waiting on a dialog; holding its resume until that clears"
                );
                waiting_logged = true;
            }
        }
        tokio::time::sleep(pause(reacted)).await;
    }

    tracing::warn!("{}", timed_out_message(&who, reacted));
    Outcome::TimedOut
}

fn timed_out_message(who: &str, reacted: bool) -> String {
    format!(
        "reflection: session {who} has not reached the command it was sent after {MAX_WAIT_S}s \
         (reacted={reacted}); it is still owed its resume, which is being held rather than typed into a \
         session that would only queue it"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A transcript that answers whatever the test says; the default is the tri-state "don't
    /// know" a missing transcript gives, which keeps a `shell` unsettled.
    #[derive(Default)]
    struct FakeTail {
        in_flight: Option<bool>,
        queued: Option<bool>,
        consumed: Option<bool>,
        started: Option<bool>,
        compacted: Option<bool>,
        // When set, the start line is dated, and one stamped before the injection must not count.
        started_at: Option<i64>,
        polls: u32,
    }

    impl TranscriptView for FakeTail {
        async fn poll(&mut self) -> bool {
            self.polls += 1;
            self.in_flight.is_some()
        }
        fn watch(&mut self, _text: &str) {}
        fn tool_call_in_flight(&self) -> Option<bool> {
            self.in_flight
        }
        fn queued(&self, _text: &str) -> Option<bool> {
            self.queued
        }
        fn consumed(&self, _text: &str) -> Option<bool> {
            self.consumed
        }
        fn started(&self, _text: &str, since_ms: Option<i64>) -> Option<bool> {
            match self.started_at {
                Some(at) => Some(at > since_ms.unwrap_or(0)),
                None => self.started,
            }
        }
        fn compacted(&self, _since_ms: Option<i64>) -> Option<bool> {
            self.compacted
        }
    }

    /// The record as a script of samples; the last one repeats forever.
    struct FakeProbe {
        served: Vec<Option<(String, i64)>>,
        alive: bool,
        now: Option<i64>,
    }

    impl Probe for FakeProbe {
        async fn read_status(&mut self, _pid: u32) -> Option<(String, i64)> {
            if self.served.len() > 1 { self.served.remove(0) } else { self.served[0].clone() }
        }
        async fn proc_start(&mut self, _pid: u32) -> Option<String> {
            self.alive.then(|| "123".to_string())
        }
        fn now_ms(&self) -> i64 {
            self.now.unwrap_or_else(now_ms)
        }
    }

    struct Run {
        injected_at_ms: i64,
        alive: bool,
        tail: FakeTail,
        text: &'static str,
        now: Option<i64>,
    }

    impl Default for Run {
        fn default() -> Self {
            Self { injected_at_ms: 1000, alive: true, tail: FakeTail::default(), text: "", now: None }
        }
    }

    fn s(status: &str, updated_ms: i64) -> Option<(String, i64)> {
        Some((status.to_string(), updated_ms))
    }

    /// Drive the watcher over a scripted list of samples. Paused time makes the cap reachable.
    async fn run(samples: &[Option<(String, i64)>], opts: Run) -> Outcome {
        let mut tail = opts.tail;
        let mut probe = FakeProbe { served: samples.to_vec(), alive: opts.alive, now: opts.now };
        await_completion(4242, opts.injected_at_ms, "", opts.text, &mut tail, &mut probe).await
    }

    fn tail(f: impl FnOnce(&mut FakeTail)) -> FakeTail {
        let mut t = FakeTail::default();
        f(&mut t);
        t
    }

    /// Wall clock far enough past the samples that a `shell` counts as stale.
    fn frozen_clock() -> Option<i64> {
        Some(2000 + (SHELL_SETTLE_S * 1000.0) as i64)
    }

    // -- the signal that matters: the command has begun running

    #[tokio::test(start_paused = true)]
    async fn test_a_command_seen_to_start_fires_at_once_however_busy_the_session_is() {
        // A compacting session is `busy` straight through, and everything typed then is drained
        // into the turn that begins when it ends: this is the moment to deliver.
        let t = tail(|t| t.started = Some(true));
        assert_eq!(run(&[s("busy", 2000)], Run { text: "/compact", tail: t, ..Run::default() }).await, Outcome::Started);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_start_line_from_an_earlier_compaction_does_not_fire() {
        // Firing on an earlier compaction's identical start line would queue the resume ahead of
        // the command it is meant to follow.
        let t = tail(|t| t.started_at = Some(500));
        let opts = Run { injected_at_ms: 1_000, text: "/compact", tail: t, ..Run::default() };
        assert_eq!(run(&[s("busy", 2000)], opts).await, Outcome::TimedOut);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_finished_compaction_fires_even_if_the_start_line_was_missed() {
        let t = tail(|t| t.compacted = Some(true));
        assert_eq!(run(&[s("busy", 2000)], Run { text: "/compact", tail: t, ..Run::default() }).await, Outcome::Started);
    }

    // -- a command that finished before the first sample

    // -- settling

    #[tokio::test(start_paused = true)]
    async fn test_a_full_turn_then_idle_settles() {
        let samples = [s("busy", 1100), s("busy", 1200), s("idle", 1300), s("idle", 1300), s("idle", 1300)];
        assert_eq!(run(&samples, Run::default()).await, Outcome::Settled);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_busy_or_blocked_session_never_settles() {
        // `waiting` is a blocking dialog, and resuming into one pastes behind a modal.
        for status in ["busy", "waiting"] {
            let t = tail(|t| t.in_flight = Some(false));
            assert_eq!(run(&[s(status, 2000)], Run { tail: t, ..Run::default() }).await, Outcome::TimedOut, "{status}");
        }
    }

    // -- a session that finished its turn but still holds a background task

    #[tokio::test(start_paused = true)]
    async fn test_a_stale_shell_with_a_foreground_tool_call_does_not_settle() {
        let t = tail(|t| {
            t.in_flight = Some(true);
            t.queued = Some(false);
        });
        let opts = Run { text: "/compact", tail: t, now: frozen_clock(), ..Run::default() };
        assert_eq!(run(&[s("shell", 2000)], opts).await, Outcome::TimedOut);
    }

    #[tokio::test(start_paused = true)]
    async fn test_our_own_command_still_queued_holds_the_resume_back() {
        // Resuming now co-queues the resume with the /compact it is supposed to follow.
        let t = tail(|t| {
            t.in_flight = Some(false);
            t.queued = Some(true);
        });
        let opts = Run { text: "/compact", tail: t, now: frozen_clock(), ..Run::default() };
        assert_eq!(run(&[s("shell", 2000)], opts).await, Outcome::TimedOut);
    }

    // -- robustness while the session is being moved around

    #[tokio::test(start_paused = true)]
    async fn test_an_unreadable_record_on_a_dead_process_is_death() {
        assert_eq!(run(&[None], Run { alive: false, ..Run::default() }).await, Outcome::Vanished);
    }

}
