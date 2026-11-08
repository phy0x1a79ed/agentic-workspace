//! The sender: find the caller's lane, write into it, and prove from the session's own record that
//! the write landed, retrying only where that record proves nothing was consumed.
//!
//! The screen is never the arbiter. A rendered screen shows what the TUI chose to draw, and Claude
//! Code paints no composer while it compacts, which is the window every deferred resume aims at.
//! The record (`status`/`statusUpdatedAt`) and the transcript say the same thing on every lane.
//! This establishes that the session consumed the keystrokes, never that the command ran.

use std::future::Future;
use std::time::Duration;

use serde_json::{json, Value};
use tokio::time::{sleep, Instant};

use crate::guards::{self, EmptyText};
use crate::lane::{Lane, LaneError, Opener};
use crate::promises::{render_followup, Change, Pending, Promises};
use crate::target::{Resolve, ResolveError, Target};
use crate::transcript::Tail;
use crate::watcher::{self, Probe, TranscriptView};

/// Three tries: enough to cross one re-host plus one unlucky beat, few enough that an unreachable
/// session is not ground against while the caller blocks on the verb.
pub const ATTEMPTS: u32 = 3;

/// The pause between attempts. Run flat out, three attempts were spent inside 2.4 s and the retry
/// budget bought nothing one attempt would not have.
pub const RETRY_BACKOFF: Duration = Duration::from_millis(500);

/// How long the session's record gets to show it took our Enter. `statusUpdatedAt` was measured
/// moving 102 ms after a submit; three seconds is thirty times that.
pub const CONFIRM_WAIT: Duration = Duration::from_secs(3);
/// How often the record is read while confirming.
pub const CONFIRM_POLL: Duration = Duration::from_millis(50);

/// The longest `send` waits before typing, whatever `delay_ms` asks.
pub const MAX_DELAY_MS: u64 = 25_000;

const UNIDENTIFIED: &str = "could not tell which session is calling, so there is nothing safe to inject into. \
                            Reflection types into the caller's own prompt and will not guess at a target — this \
                            usually means the call did not come through a session's own `awm mcp` proxy (a plain \
                            shell, for instance)";

/// How a submit came to be believed. Reported to the caller, because "we could not tell" and "it
/// went in" ask different things of it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Confirmed {
    /// The session's record moved: it took the line.
    Record,
    /// Mid-turn, and its transcript shows our line queued.
    Enqueued,
    /// Mid-turn at write time, with nothing to observe: an inference, not evidence.
    Queued,
    /// A modal had the keyboard, so the line was probably swallowed.
    Blocked,
    /// The record could not be read throughout: no claim either way.
    Unreadable,
    /// `enter` was false, so nothing was submitted.
    NotSubmitted,
}

impl Confirmed {
    /// The wire word: `record`, `enqueued`, `queued`, `blocked`, `unreadable` or `none`.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Record => "record",
            Self::Enqueued => "enqueued",
            Self::Queued => "queued",
            Self::Blocked => "blocked",
            Self::Unreadable => "unreadable",
            Self::NotSubmitted => "none",
        }
    }
}

/// What a delivery achieved: whether it submitted, the lane it went to, and on what evidence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Delivery {
    pub submitted: bool,
    pub lane: Target,
    pub confirmed: Confirmed,
}

/// Every attempt to put the text into the session failed, or the commit broke.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum DeliveryError {
    #[error("{0}")]
    GaveUp(String),
    #[error(transparent)]
    CommitFailed(#[from] CommitFailed),
}

/// Enter was sent, and something went wrong at or after it. Never retried: once Enter is on the
/// wire the command may already be running, and running arbitrary text twice is worse than not at all.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct CommitFailed(pub String);

/// A modal holds the session's keyboard, so nothing was typed. Retryable, and refused before the
/// write: a paste into a dialog is consumed by it, and a retry would concatenate onto that.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct SessionBlocked(pub String);

/// Enter was pressed and the record proves nothing was consumed: settled throughout, timestamp
/// unmoved, so no turn started. The one failure past the commit that is safe to retry.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct NotSubmitted(pub String);

/// Why `send` did not type anything, or did not finish.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SendError {
    /// No caller pid: there is no session that is safe to type into.
    #[error(transparent)]
    Unidentified(#[from] ResolveError),
    /// The text was empty: a caller bug, not a refusal.
    #[error(transparent)]
    Empty(#[from] EmptyText),
    #[error(transparent)]
    Delivery(#[from] DeliveryError),
}

// The failures one attempt may end in. All but a broken commit are retried.
#[derive(Debug, thiserror::Error)]
enum AttemptError {
    #[error(transparent)]
    Lane(#[from] LaneError),
    #[error(transparent)]
    Blocked(#[from] SessionBlocked),
    #[error(transparent)]
    NotSubmitted(#[from] NotSubmitted),
    #[error(transparent)]
    Commit(#[from] CommitFailed),
}

/// What the sender asks of a transcript reader beyond the watcher's questions; [`Tail`] is the real one.
pub trait SendTail: TranscriptView {
    /// `text` is being typed now: only it may claim a content-less enqueue from here on.
    fn expect(&mut self, text: &str);
    /// Did `text` reach the session, consumed or queued? `None` if unknown.
    fn landed(&self, text: &str) -> Option<bool>;
}

impl SendTail for Tail {
    fn expect(&mut self, text: &str) {
        Tail::expect(self, text, None)
    }
    fn landed(&self, text: &str) -> Option<bool> {
        Tail::landed(self, text, None)
    }
}

// What a missing tail answers when the settle test asks: nothing, which leaves `shell` unsettled.
struct NoTail;

impl TranscriptView for NoTail {
    async fn poll(&mut self) -> bool {
        false
    }
    fn watch(&mut self, _text: &str) {}
    fn tool_call_in_flight(&self) -> Option<bool> {
        None
    }
    fn queued(&self, _text: &str) -> Option<bool> {
        None
    }
    fn consumed(&self, _text: &str) -> Option<bool> {
        None
    }
    fn started(&self, _text: &str, _since_ms: Option<i64>) -> Option<bool> {
        None
    }
    fn compacted(&self, _since_ms: Option<i64>) -> Option<bool> {
        None
    }
}

/// Arms the resume watcher for a promise `send` has just written down. Python's `spawn` seam: the
/// live one starts a detached task and returns at once, a test one records or does nothing.
pub trait ResumeWatch<T>: Send {
    /// Start waiting to type `promise`'s resume, reading the session through `tail`, the reader
    /// that was open while the command was typed.
    fn arm(&mut self, promise: Pending, tail: T) -> impl Future<Output = ()> + Send;
}

/// How one delivery runs.
#[derive(Debug, Clone, Copy)]
pub struct DeliverOpts<'a> {
    /// Press Enter after the write. False leaves the text in the prompt.
    pub enter: bool,
    pub attempts: u32,
    /// How logs and errors name what was sent; never the text itself.
    pub what: &'a str,
}

impl Default for DeliverOpts<'_> {
    fn default() -> Self {
        Self {
            enter: true,
            attempts: ATTEMPTS,
            what: "text",
        }
    }
}

/// One `send` call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SendRequest {
    pub text: String,
    /// The calling REPL as the gateway observed it, never as the model claims it.
    pub caller_pid: Option<u32>,
    pub enter: bool,
    /// How long to wait before typing, capped at [`MAX_DELAY_MS`].
    pub delay_ms: u64,
    /// Lets a destructive command through.
    pub confirm: bool,
    /// The caller's closing prompt for the resume; None keeps an earlier one, else the default.
    pub followup: Option<String>,
    /// For a verb that drives the modal its command opens; a free-text caller never sets it.
    pub allow_modal: bool,
    /// The settings command `text` is, so the resume can say what became of it.
    pub change: Option<Change>,
}

impl SendRequest {
    /// Type `text` into `caller_pid` and submit it, with no delay, confirmation or change.
    pub fn new(text: &str, caller_pid: Option<u32>) -> Self {
        Self {
            text: text.into(),
            caller_pid,
            enter: true,
            delay_ms: 0,
            confirm: false,
            followup: None,
            allow_modal: false,
            change: None,
        }
    }
}

fn who(lane: &Target) -> &str {
    lane.name()
        .filter(|n| !n.is_empty())
        .unwrap_or(lane.session_id())
}

/// The sender over its effects: `detect` finds the lane, `opener` opens it, `probe` reads the
/// session record and the clock.
#[derive(Debug, Clone)]
pub struct Sender<D, O, P> {
    pub detect: D,
    pub opener: O,
    pub probe: P,
}

impl<D: Resolve, O: Opener, P: Probe> Sender<D, O, P> {
    /// The caller's injection target: a tmux pane or a background session's PTY.
    pub async fn resolve_lane(&self, caller_pid: u32) -> Result<Target, LaneError> {
        self.detect.resolve(caller_pid, None).await
    }

    async fn resolve(&self, caller_pid: Option<u32>) -> Result<Target, LaneError> {
        match caller_pid.filter(|p| *p != 0) {
            Some(pid) => self.resolve_lane(pid).await,
            None => Err(ResolveError(UNIDENTIFIED.into()).into()),
        }
    }

    /// Did the session take the Enter just pressed? Its own record says, on every lane alike.
    ///
    /// The record is written on transitions. A session settled when we wrote (as
    /// [`watcher::is_settled`] means it, so `shell` held by a background task counts) has to move
    /// to run our line, and a timestamp that moved stays moved. A session mid-turn queues the line
    /// and moves nothing, so only its transcript's enqueue can say so, else the answer is the
    /// inference [`Confirmed::Queued`]. [`Confirmed::NotSubmitted`] is only the proven negative.
    pub async fn confirm_submit<T: SendTail>(
        &mut self,
        repl_pid: u32,
        before: Option<&(String, i64)>,
        text: &str,
        tail: Option<&mut T>,
    ) -> Confirmed {
        let Some((status, at)) = before else {
            // No reference, so "did it move?" has no answer: neither busy nor a failure.
            return Confirmed::Unreadable;
        };
        if status == "waiting" {
            // A dialog owns the keyboard, so the paste was most likely eaten by it. Reachable when
            // a dialog opened between the sample and the Enter.
            return Confirmed::Blocked;
        }
        let now = self.probe.now_ms();
        let settled = match &tail {
            Some(t) => watcher::is_settled(status, *at, now, &**t, text),
            None => watcher::is_settled(status, *at, now, &NoTail, text),
        };
        if !settled {
            if let Some(t) = tail.filter(|_| !text.is_empty()) {
                let deadline = Instant::now() + CONFIRM_WAIT;
                while Instant::now() < deadline {
                    t.poll().await;
                    if t.landed(text) == Some(true) {
                        return Confirmed::Enqueued;
                    }
                    sleep(CONFIRM_POLL).await;
                }
            }
            return Confirmed::Queued;
        }
        let deadline = Instant::now() + CONFIRM_WAIT;
        let mut ever_read = false;
        while Instant::now() < deadline {
            if let Some((_, now_at)) = self.probe.read_status(repl_pid).await {
                ever_read = true;
                if now_at > *at {
                    return Confirmed::Record;
                }
            }
            sleep(CONFIRM_POLL).await;
        }
        // An unreadable record is not a negative: it is rewritten on every attach and background.
        if ever_read {
            Confirmed::NotSubmitted
        } else {
            Confirmed::Unreadable
        }
    }

    // One detect → guard → write → commit → confirm transaction. Nothing rendered is consulted:
    // before the write the record is asked one question, whether a modal holds the keyboard.
    async fn attempt<T: SendTail>(
        &mut self,
        repl_pid: u32,
        text: &str,
        enter: bool,
        clear_first: bool,
        mut tail: Option<&mut T>,
    ) -> Result<Delivery, AttemptError> {
        let lane = self.resolve_lane(repl_pid).await?;
        // Sampled before anything is typed: it is the modal guard and the confirmation's reference.
        let was = self.probe.read_status(repl_pid).await;
        if was.as_ref().is_some_and(|(s, _)| s == "waiting") {
            return Err(SessionBlocked(format!(
                "session {} is at a prompt waiting for an answer, so a paste would be eaten by the dialog rather \
                 than queued; nothing was typed",
                who(&lane)
            ))
            .into());
        }
        let mut writer = self.opener.open_writer(&lane).await?;
        let typed = async {
            if clear_first {
                writer.clear().await?;
            }
            if let Some(t) = tail.as_deref_mut() {
                t.expect(text);
            }
            writer.write(text).await?;
            // All a lane can say of a write: not that it arrived, only that the host did not
            // announce discarding it.
            writer.check_not_rejected().await
        }
        .await;
        if let Err(e) = typed {
            writer.close().await;
            return Err(e.into());
        }
        if !enter {
            writer.close().await;
            return Ok(Delivery {
                submitted: false,
                lane,
                confirmed: Confirmed::NotSubmitted,
            });
        }
        if let Err(e) = writer.commit().await {
            let label = writer.label();
            writer.close().await;
            return Err(CommitFailed(format!(
                "the text reached {label} and Enter was sent, but the submit did not complete cleanly ({e}); not \
                 retrying, because it may already be running"
            ))
            .into());
        }
        writer.close().await;
        let confirmed = self
            .confirm_submit(repl_pid, was.as_ref(), text, tail)
            .await;
        if confirmed == Confirmed::NotSubmitted {
            return Err(NotSubmitted(format!(
                "Enter was sent to {} session {} but it stayed settled with its status unchanged for {:.1}s, so it \
                 consumed nothing — the paste did not reach its prompt",
                lane.kind(),
                who(&lane),
                CONFIRM_WAIT.as_secs_f64()
            ))
            .into());
        }
        Ok(Delivery {
            submitted: true,
            lane,
            confirmed,
        })
    }

    // Best-effort: leave nothing of a failed send in the prompt. Only attempts two and three clear
    // on the way in, so a give-up left the last paste for the next write or a stray Enter. By here
    // the only thing in the box is ours.
    async fn wipe_prompt(&self, repl_pid: u32) {
        let wiped: Result<(), LaneError> = async {
            let lane = self.resolve_lane(repl_pid).await?;
            let mut writer = self.opener.open_writer(&lane).await?;
            let cleared = writer.clear().await;
            writer.close().await;
            cleared
        }
        .await;
        if let Err(e) = wiped {
            tracing::warn!(
                "cch: could not clear the prompt of pid {repl_pid} after a failed send; an unsubmitted paste may be \
                 left in it: {e}"
            );
        }
    }

    /// Put `text` into session `repl_pid`, or fail with [`DeliveryError`].
    ///
    /// The lane is detected afresh inside every attempt, right before the write, because that gap
    /// is where a session being attached or backgrounded re-homes its pty. Attempt one never
    /// clears, so a working send never destroys what a human was typing; attempts two and three
    /// clear first so a retry is idempotent; a give-up clears on the way out, except against a
    /// modal, where Ctrl-U would answer the dialog instead. Every attempt and failure is logged.
    pub async fn deliver<T: SendTail>(
        &mut self,
        repl_pid: u32,
        text: &str,
        opts: DeliverOpts<'_>,
        mut tail: Option<&mut T>,
    ) -> Result<Delivery, DeliveryError> {
        let (what, attempts) = (opts.what, opts.attempts);
        let mut failures: Vec<String> = Vec::new();
        let mut blocked = false;
        for attempt in 1..=attempts {
            if attempt > 1 {
                sleep(RETRY_BACKOFF).await;
            }
            match self
                .attempt(repl_pid, text, opts.enter, attempt > 1, tail.as_deref_mut())
                .await
            {
                Err(AttemptError::Commit(e)) => {
                    tracing::error!(
                        "cch: {what} reached pid {repl_pid} on attempt {attempt}/{attempts} but the submit did not \
                         complete: {e}"
                    );
                    return Err(e.into());
                }
                Err(e) => {
                    blocked = matches!(e, AttemptError::Blocked(_));
                    tracing::warn!("cch: attempt {attempt}/{attempts} to deliver {what} to pid {repl_pid} failed: {e}");
                    failures.push(format!("attempt {attempt}: {e}"));
                }
                Ok(result) => {
                    if attempt > 1 {
                        tracing::warn!(
                            "cch: delivered {what} to pid {repl_pid} on attempt {attempt}/{attempts} via the {} lane, \
                             confirmed by {} (earlier attempts failed — {})",
                            result.lane.kind(),
                            result.confirmed.as_str(),
                            failures.join("; ")
                        );
                    } else {
                        tracing::info!(
                            "cch: delivered {what} to pid {repl_pid} via the {} lane, confirmed by {}",
                            result.lane.kind(),
                            result.confirmed.as_str()
                        );
                    }
                    return Ok(result);
                }
            }
        }
        tracing::error!(
            "cch: giving up on delivering {what} to pid {repl_pid} after {attempts} attempts — {}",
            failures.join("; ")
        );
        if !blocked {
            self.wipe_prompt(repl_pid).await;
        }
        Err(DeliveryError::GaveUp(format!(
            "could not deliver {what} to the calling session after {attempts} attempts: {}",
            failures.join("; ")
        )))
    }

    /// Type `req.text` into the calling session, whatever hosts it, and answer the verb's result.
    ///
    /// A refused text answers the guard's refusal and types nothing. `tail` is a reader on the
    /// caller's transcript, watched here before the first keystroke. A submitted slash command
    /// owes the session a resume: the promise is written to `promises` before `watch` arms its
    /// watcher, and a session keeps one promise, so a command sent while an earlier one is owed
    /// takes that promise over with its changes. A failed delivery leaves no promise behind.
    pub async fn send<T: SendTail, W: ResumeWatch<T>>(
        &mut self,
        req: &SendRequest,
        promises: &Promises,
        mut tail: T,
        watch: &mut W,
    ) -> Result<Value, SendError> {
        let Some(caller_pid) = req.caller_pid.filter(|p| *p != 0) else {
            return Err(ResolveError(UNIDENTIFIED.into()).into());
        };
        let text = req.text.as_str();
        // Guards before anything is logged or detected, so a log never claims a refused command went in.
        if let Some(refused) = guards::refusal(text, req.confirm, req.allow_modal)? {
            return Ok(refused);
        }
        if req.delay_ms > 0 {
            sleep(Duration::from_millis(req.delay_ms.min(MAX_DELAY_MS))).await;
        }
        // Open before a keystroke goes out, so whatever the session does with the text is in view.
        tail.watch(text);
        tail.poll().await;
        let injected_at_ms = self.probe.now_ms();
        let opts = DeliverOpts {
            enter: req.enter,
            what: "the requested command",
            ..DeliverOpts::default()
        };
        let result = self
            .deliver(caller_pid, text, opts, Some(&mut tail))
            .await?;
        let lane = &result.lane;

        let mut followup_sent = None;
        let mut ticket = None;
        if result.submitted && guards::is_slash(text) {
            let pid = lane.repl_pid();
            let proc_start = self.probe.proc_start(pid).await.unwrap_or_default();
            let earlier = promises
                .read(pid)
                .await
                .filter(|e| e.proc_start == proc_start);
            let mut changes = earlier
                .as_ref()
                .map(|e| e.changes.clone())
                .unwrap_or_default();
            changes.extend(req.change.clone());
            let own = req.followup.as_deref().unwrap_or("").trim();
            let closing = if own.is_empty() {
                earlier
                    .as_ref()
                    .map(|e| e.closing.clone())
                    .unwrap_or_default()
            } else {
                own.into()
            };
            let followup = render_followup(&changes, None, &closing);
            let promise = Pending {
                name: lane.name().map(str::to_string),
                hosting: lane.hosting().into(),
                changes,
                closing,
                ticket: earlier
                    .map(|e| e.ticket)
                    .filter(|t| !t.is_empty())
                    .unwrap_or_else(|| uuid::Uuid::new_v4().simple().to_string()),
                ..Pending::new(
                    pid,
                    &proc_start,
                    lane.session_id(),
                    text,
                    &followup,
                    injected_at_ms,
                )
            };
            // Written down BEFORE the watcher starts: the gateway restarts this process under it.
            promises.record(&promise).await;
            ticket = Some(promise.ticket.clone());
            followup_sent = Some(followup);
            watch.arm(promise, tail).await;
        }

        let mut out = json!({
            "ok": true,
            "session": who(lane),
            "hosting": lane.hosting(),
            "text": text,
            "submitted": result.submitted,
            // What the submit rests on. A session compacting itself is mid-turn by definition, so
            // `enqueued` is that case observed in its transcript and `queued` the same unobserved.
            "confirmed": result.confirmed.as_str(),
            "followup": followup_sent,
            "followup_deferred": ticket.is_some(),
        });
        if let Some(ticket) = ticket {
            out["ticket"] = json!(ticket);
        }
        if let Target::Tmux(t) = lane {
            out["pane"] = json!(t.pane);
        }
        Ok(out)
    }

    /// What cch takes the caller to be, for when a refusal is confusing.
    pub async fn describe_caller(&self, caller_pid: Option<u32>) -> Result<Value, LaneError> {
        let lane = self.resolve(caller_pid).await?;
        let mut out = json!({
            "session": who(&lane),
            "session_id": lane.session_id(),
            "pid": lane.repl_pid(),
            "hosting": lane.hosting(),
        });
        if let Target::Tmux(t) = &lane {
            out["pane"] = json!(t.pane);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::fmt::Debug;
    use std::sync::{Arc, Mutex};

    use awm_harness::claude::ClaudeHome;

    use super::*;
    use crate::lane::{Host, Key};
    use crate::promises::tests::{FakeLiveness, SID};
    
    use crate::target::testing::FakeProcs;
    use crate::target::TmuxLane;
    use crate::tmux::testing::{FakePane, PaneState};
    use crate::tmux::{Tmux, TmuxError};

    const CONTINUE: &str = guards::DEFAULT_FOLLOWUP;

    fn lane() -> Target {
        Target::Tmux(TmuxLane {
            pane: "%7".into(),
            session_id: SID.into(),
            repl_pid: 4242,
            name: Some("test".into()),
        })
    }

    type Log = Arc<Mutex<Vec<String>>>;

    fn log() -> Log {
        Log::default()
    }

    fn seen(log: &Log) -> Vec<String> {
        log.lock().unwrap().clone()
    }

    fn count(log: &Log, what: &str) -> usize {
        seen(log).iter().filter(|e| *e == what).count()
    }

    /// The session's own record, written on transitions: `at` moves when it starts doing something.
    #[derive(Clone)]
    struct FakeRecord {
        state: Arc<Mutex<Option<(String, i64)>>>,
        now_ms: i64,
    }

    impl FakeRecord {
        fn new(status: &str) -> Self {
            Self {
                state: Arc::new(Mutex::new(Some((status.into(), 1000)))),
                now_ms: 1000,
            }
        }

        fn unreadable() -> Self {
            Self {
                state: Arc::new(Mutex::new(None)),
                now_ms: 1000,
            }
        }

        /// The session started a turn on what we submitted.
        fn consumed(&self) {
            if let Some((status, at)) = self.state.lock().unwrap().as_mut() {
                *status = "busy".into();
                *at += 1;
            }
        }
    }

    impl Probe for FakeRecord {
        async fn read_status(&mut self, _pid: u32) -> Option<(String, i64)> {
            self.state.lock().unwrap().clone()
        }
        async fn proc_start(&mut self, _pid: u32) -> Option<String> {
            Some("999".into())
        }
        fn now_ms(&self) -> i64 {
            self.now_ms
        }
    }

    /// A prompt box with no opinion about any screen. `deaf` takes the keystrokes and starts no
    /// turn; `fail_on` names the verbs that blow up, `check` standing for the daemon's `auth-required`.
    #[derive(Clone)]
    struct FakeWriter {
        events: Log,
        fail_on: Vec<&'static str>,
        record: Option<FakeRecord>,
        deaf: bool,
    }

    impl FakeWriter {
        fn new(events: &Log, record: Option<&FakeRecord>) -> Self {
            Self {
                events: events.clone(),
                fail_on: vec![],
                record: record.cloned(),
                deaf: false,
            }
        }

        fn failing(mut self, verb: &'static str) -> Self {
            self.fail_on.push(verb);
            self
        }

        fn deaf(mut self) -> Self {
            self.deaf = true;
            self
        }

        fn note(&self, event: String, verb: &str) -> Result<(), LaneError> {
            self.events.lock().unwrap().push(event);
            if self.fail_on.contains(&verb) {
                return Err(TmuxError(format!("{verb} blew up")).into());
            }
            Ok(())
        }
    }

    impl Lane for FakeWriter {
        fn label(&self) -> String {
            "the fake lane".into()
        }
        async fn write(&mut self, text: &str) -> Result<(), LaneError> {
            self.note(format!("write:{text}"), "write")
        }
        async fn clear(&mut self) -> Result<(), LaneError> {
            self.note("clear".into(), "clear")
        }
        async fn check_not_rejected(&mut self) -> Result<(), LaneError> {
            self.note("check".into(), "check")
        }
        async fn commit(&mut self) -> Result<(), LaneError> {
            self.note("commit".into(), "commit")?;
            if let Some(record) = self.record.as_ref().filter(|_| !self.deaf) {
                record.consumed();
            }
            Ok(())
        }
        // Nothing on a screen is worth a veto, so asking for one is a failure of the sender.
        async fn read(&mut self) -> Result<String, LaneError> {
            panic!("the sender asked the lane for read")
        }
        async fn glance(&mut self) -> Result<String, LaneError> {
            panic!("the sender asked the lane for glance")
        }
        async fn repaint(&mut self) -> Result<bool, LaneError> {
            panic!("the sender asked the lane for repaint")
        }
        fn forget(&mut self) {
            panic!("the sender asked the lane for forget")
        }
        async fn press(&mut self, _key: Key) -> Result<(), LaneError> {
            panic!("the sender asked the lane for press")
        }
        async fn close(self) {}
    }

    /// Serves its writers in order, the last one for good.
    struct Writers(Mutex<VecDeque<FakeWriter>>);

    impl Writers {
        fn of(writers: Vec<FakeWriter>) -> Self {
            Self(Mutex::new(writers.into()))
        }
        fn next(&self) -> FakeWriter {
            let mut q = self.0.lock().unwrap();
            if q.len() > 1 {
                q.pop_front().unwrap()
            } else {
                q[0].clone()
            }
        }
    }

    impl Opener for Writers {
        type Lane = FakeWriter;
        async fn open_writer(&self, _target: &Target) -> Result<FakeWriter, LaneError> {
            Ok(self.next())
        }
        async fn open_session(&self, _target: &Target) -> Result<FakeWriter, LaneError> {
            Ok(self.next())
        }
    }

    /// Fails every open, as a pane that has gone.
    struct Gone;

    impl Opener for Gone {
        type Lane = FakeWriter;
        async fn open_writer(&self, _target: &Target) -> Result<FakeWriter, LaneError> {
            Err(TmuxError("pane is gone".into()).into())
        }
        async fn open_session(&self, _target: &Target) -> Result<FakeWriter, LaneError> {
            Err(TmuxError("pane is gone".into()).into())
        }
    }

    struct Detect {
        target: Target,
        calls: Arc<Mutex<Vec<u32>>>,
        fail: Option<&'static str>,
    }

    impl Detect {
        fn of(target: Target) -> Self {
            Self {
                target,
                calls: Arc::default(),
                fail: None,
            }
        }
    }

    impl Resolve for Detect {
        async fn resolve(&self, repl_pid: u32, _expect: Option<&str>) -> Result<Target, LaneError> {
            self.calls.lock().unwrap().push(repl_pid);
            match self.fail {
                Some(why) => Err(ResolveError(why.into()).into()),
                None => Ok(self.target.clone()),
            }
        }
    }

    /// A transcript that answers what the test says.
    #[derive(Default)]
    struct FakeTail {
        landed: Option<bool>,
        in_flight: Option<bool>,
        queued: Option<bool>,
    }

    impl TranscriptView for FakeTail {
        async fn poll(&mut self) -> bool {
            true
        }
        fn watch(&mut self, _text: &str) {}
        fn tool_call_in_flight(&self) -> Option<bool> {
            self.in_flight
        }
        fn queued(&self, _text: &str) -> Option<bool> {
            self.queued
        }
        fn consumed(&self, _text: &str) -> Option<bool> {
            self.landed
        }
        fn started(&self, _text: &str, _since_ms: Option<i64>) -> Option<bool> {
            None
        }
        fn compacted(&self, _since_ms: Option<i64>) -> Option<bool> {
            None
        }
    }

    impl SendTail for FakeTail {
        fn expect(&mut self, _text: &str) {}
        fn landed(&self, _text: &str) -> Option<bool> {
            self.landed
        }
    }

    const NO_TAIL: Option<&mut FakeTail> = None;

    fn sender(
        writers: Vec<FakeWriter>,
        record: &FakeRecord,
    ) -> Sender<Detect, Writers, FakeRecord> {
        Sender {
            detect: Detect::of(lane()),
            opener: Writers::of(writers),
            probe: record.clone(),
        }
    }

    async fn deliver(
        writers: Vec<FakeWriter>,
        record: &FakeRecord,
    ) -> Result<Delivery, DeliveryError> {
        sender(writers, record)
            .deliver(4242, "/compact", DeliverOpts::default(), NO_TAIL)
            .await
    }

    // A subscriber that keeps each event as "LEVEL message", for the tests that a failure is said out loud.
    struct Capture(Log);

    struct Message(String);

    impl tracing::field::Visit for Message {
        fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn Debug) {
            if field.name() == "message" {
                self.0 = format!("{value:?}");
            }
        }
    }

    impl tracing::Subscriber for Capture {
        fn enabled(&self, _meta: &tracing::Metadata<'_>) -> bool {
            true
        }
        fn new_span(&self, _span: &tracing::span::Attributes<'_>) -> tracing::span::Id {
            tracing::span::Id::from_u64(1)
        }
        fn record(&self, _span: &tracing::span::Id, _values: &tracing::span::Record<'_>) {}
        fn record_follows_from(&self, _span: &tracing::span::Id, _follows: &tracing::span::Id) {}
        fn event(&self, event: &tracing::Event<'_>) {
            let mut message = Message(String::new());
            event.record(&mut message);
            self.0
                .lock()
                .unwrap()
                .push(format!("{} {}", event.metadata().level(), message.0));
        }
        fn enter(&self, _span: &tracing::span::Id) {}
        fn exit(&self, _span: &tracing::span::Id) {}
    }

    fn capture() -> (tracing::subscriber::DefaultGuard, Log) {
        let lines = log();
        (
            tracing::subscriber::set_default(Capture(lines.clone())),
            lines,
        )
    }

    struct NoWatch;

    impl<T: Send> ResumeWatch<T> for NoWatch {
        async fn arm(&mut self, _promise: Pending, _tail: T) {}
    }

    // --- the happy path ---

    #[tokio::test(start_paused = true)]
    async fn test_a_clean_send_writes_checks_then_commits() {
        // Three verbs, in this order, and no fourth: nothing read off the lane before Enter.
        let (events, rec) = (log(), FakeRecord::new("idle"));
        let result = deliver(vec![FakeWriter::new(&events, Some(&rec))], &rec)
            .await
            .unwrap();
        assert!(result.submitted);
        assert_eq!(result.lane, lane());
        assert_eq!(result.confirmed, Confirmed::Record);
        assert_eq!(seen(&events), ["write:/compact", "check", "commit"]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_enter_false_writes_but_does_not_commit_or_claim_a_submit() {
        let (events, rec) = (log(), FakeRecord::new("idle"));
        let opts = DeliverOpts {
            enter: false,
            ..DeliverOpts::default()
        };
        let result = sender(vec![FakeWriter::new(&events, None)], &rec)
            .deliver(4242, "/compact", opts, NO_TAIL)
            .await
            .unwrap();
        assert!(!result.submitted);
        assert_eq!(result.confirmed, Confirmed::NotSubmitted);
        assert_eq!(count(&events, "commit"), 0);
    }

    // --- the screen is not consulted, on any lane ---

    // --- what a lane may still say: that it refused the write ---

    #[tokio::test(start_paused = true)]
    async fn test_a_lane_that_reports_the_write_discarded_never_commits() {
        let (events, rec) = (log(), FakeRecord::new("idle"));
        let failed = deliver(
            vec![FakeWriter::new(&events, Some(&rec)).failing("check")],
            &rec,
        )
        .await;
        assert!(matches!(failed, Err(DeliveryError::GaveUp(_))));
        assert_eq!(
            count(&events, "commit"),
            0,
            "a refused write must not be submitted"
        );
        assert_eq!(count(&events, "write:/compact"), 3, "and it is retried");
    }

    // --- confirming the submit from the session's own record ---

    #[tokio::test(start_paused = true)]
    async fn test_a_settled_session_that_never_moves_did_not_take_the_line() {
        // Idle throughout, timestamp unmoved: it consumed nothing, so a retry may cross the commit.
        let (events, rec) = (log(), FakeRecord::new("idle"));
        let err = deliver(vec![FakeWriter::new(&events, Some(&rec)).deaf()], &rec)
            .await
            .unwrap_err();
        assert_eq!(
            count(&events, "commit"),
            3,
            "a proven non-submit is retried"
        );
        assert!(err.to_string().contains("consumed nothing"), "{err}");
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_busy_session_is_reported_queued_rather_than_confirmed() {
        let (events, rec) = (log(), FakeRecord::new("busy"));
        let result = deliver(vec![FakeWriter::new(&events, Some(&rec)).deaf()], &rec)
            .await
            .unwrap();
        assert!(result.submitted);
        assert_eq!(result.confirmed, Confirmed::Queued);
        assert_eq!(count(&events, "commit"), 1, "and it is not retried");
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_session_behind_a_modal_is_refused_before_anything_is_typed() {
        let (events, rec) = (log(), FakeRecord::new("waiting"));
        let err = deliver(vec![FakeWriter::new(&events, Some(&rec))], &rec)
            .await
            .unwrap_err();
        assert!(
            seen(&events).is_empty(),
            "nothing typed, nothing cleared, nothing submitted"
        );
        assert!(err.to_string().contains("waiting for an answer"), "{err}");
    }

    // --- retrying ---

    #[tokio::test(start_paused = true)]
    async fn test_three_attempts_then_a_failure_naming_all_three() {
        let (events, rec) = (log(), FakeRecord::new("idle"));
        let (_guard, lines) = capture();
        let err = deliver(
            vec![FakeWriter::new(&events, Some(&rec)).failing("write")],
            &rec,
        )
        .await
        .unwrap_err();
        assert_eq!(count(&events, "write:/compact"), 3);
        for n in 1..=3 {
            assert!(err.to_string().contains(&format!("attempt {n}")), "{err}");
        }
        assert!(
            seen(&lines)
                .iter()
                .filter(|l| l.contains("attempt"))
                .count()
                >= 3
        );
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_give_up_leaves_nothing_in_the_prompt() {
        let (events, rec) = (log(), FakeRecord::new("idle"));
        assert!(deliver(
            vec![FakeWriter::new(&events, Some(&rec)).failing("write")],
            &rec
        )
        .await
        .is_err());
        assert_eq!(seen(&events).last().map(String::as_str), Some("clear"));
    }

    // --- the commit boundary ---

    #[tokio::test(start_paused = true)]
    async fn test_a_broken_commit_is_never_retried() {
        // A commit that blew up may already be running; running arbitrary text twice is worse.
        let events = log();
        let (_guard, lines) = capture();
        let failed = deliver(
            vec![FakeWriter::new(&events, None).failing("commit")],
            &FakeRecord::new("idle"),
        )
        .await;
        assert!(matches!(failed, Err(DeliveryError::CommitFailed(_))));
        assert_eq!(
            count(&events, "commit"),
            1,
            "a commit failure must not be retried"
        );
        assert!(seen(&lines)
            .iter()
            .any(|l| l.starts_with("ERROR") && l.contains("did not complete")));
    }

    // --- send (test_cch.py) ---

    fn store() -> (tempfile::TempDir, Promises) {
        let dir = tempfile::tempdir().unwrap();
        let promises = Promises::new(dir.path().join("pending"));
        (dir, promises)
    }

    async fn send_as(promises: &Promises, req: SendRequest) -> Result<Value, SendError> {
        let rec = FakeRecord::unreadable();
        sender(vec![FakeWriter::new(&log(), None)], &rec)
            .send(&req, promises, FakeTail::default(), &mut NoWatch)
            .await
    }

    async fn send_change(promises: &Promises, text: &str, change: Option<Change>) {
        send_as(
            promises,
            SendRequest {
                change,
                ..SendRequest::new(text, Some(4242))
            },
        )
        .await
        .unwrap();
    }

    async fn load(promises: &Promises) -> Vec<Pending> {
        promises
            .load_all(watcher::now_ms(), &FakeLiveness::new(Some("999")))
            .await
    }

    #[tokio::test(start_paused = true)]
    async fn send_answers_the_result_shape_and_describe_names_the_pane() {
        let (_d, promises) = store();
        let out = send_as(&promises, SendRequest::new("/compact", Some(4242)))
            .await
            .unwrap();
        let keys: Vec<&str> = out
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            [
                "ok",
                "session",
                "hosting",
                "text",
                "submitted",
                "confirmed",
                "followup",
                "followup_deferred",
                "ticket",
                "pane"
            ]
        );
        assert_eq!(out["confirmed"], "unreadable");
        assert_eq!(out["ticket"].as_str().unwrap().len(), 32);
        let rec = FakeRecord::new("idle");
        let described = sender(vec![FakeWriter::new(&log(), None)], &rec)
            .describe_caller(Some(4242))
            .await
            .unwrap();
        assert_eq!(
            described,
            json!({"session": "test", "session_id": SID, "pid": 4242, "hosting": "tmux", "pane": "%7"})
        );
        let refused = send_as(&promises, SendRequest::new("/clear", Some(4242)))
            .await
            .unwrap();
        assert_eq!(refused["refused"], true);
        assert!(matches!(
            send_as(&promises, SendRequest::new("  ", Some(4242))).await,
            Err(SendError::Empty(_))
        ));
    }

    // --- send records its promise (test_cch_pending.py) ---

    #[tokio::test(start_paused = true)]
    async fn test_no_promise_survives_a_command_that_never_went_in() {
        let (_d, promises) = store();
        let mut s = Sender {
            detect: Detect::of(lane()),
            opener: Gone,
            probe: FakeRecord::unreadable(),
        };
        let failed = s
            .send(
                &SendRequest::new("/compact", Some(4242)),
                &promises,
                FakeTail::default(),
                &mut NoWatch,
            )
            .await;
        assert!(matches!(failed, Err(SendError::Delivery(_))));
        assert_eq!(load(&promises).await, []);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_settings_command_promises_a_resume_that_names_it() {
        let (_d, promises) = store();
        let effort = Change::new("effort", "low", "k-effort");
        send_change(&promises, "/effort low", Some(effort.clone())).await;
        let items = load(&promises).await;
        assert_eq!(items.len(), 1);
        assert_eq!(
            items[0].followup,
            format!("The effort change to low succeeded. {CONTINUE}")
        );
        assert_eq!(
            items[0].changes,
            [effort],
            "the change survives the replay's round trip"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_second_self_command_takes_over_the_promise_and_carries_the_first() {
        let (_d, promises) = store();
        send_change(
            &promises,
            "/rename scout",
            Some(Change::new("rename", "scout", "k-rename")),
        )
        .await;
        let first = promises.read(4242).await.unwrap();
        send_change(
            &promises,
            "/effort low",
            Some(Change::new("effort", "low", "k-effort")),
        )
        .await;
        let item = promises.read(4242).await.unwrap();
        assert_eq!(item.text, "/effort low");
        assert_eq!(
            item.changes
                .iter()
                .map(|c| c.key.as_str())
                .collect::<Vec<_>>(),
            ["k-rename", "k-effort"]
        );
        assert_eq!(
            item.followup,
            format!("The rename to scout and the effort change to low succeeded. {CONTINUE}")
        );
        assert_eq!(
            item.ticket, first.ticket,
            "one promise per session, one ticket"
        );
    }

    // --- the tmux lane end to end, under the sender (tmux.rs stops at the lane) ---

    fn tmux_sender(
        pane: &FakePane,
        procs: FakeProcs,
    ) -> Sender<Detect, Host<FakePane, FakeProcs>, FakeRecord> {
        let home = ClaudeHome::at("/nonexistent/claude", "/nonexistent/claude.json");
        let host = Host {
            home,
            tmux: Tmux::with_runner(pane.clone()),
            procs,
        };
        // Busy, so the submit is believed queued at once: the lane, not the record, is under test.
        Sender {
            detect: Detect::of(lane()),
            opener: host,
            probe: FakeRecord::new("busy"),
        }
    }

    fn call_with(pane: &FakePane, verb: &str) -> Vec<String> {
        pane.state()
            .calls
            .into_iter()
            .find(|a| a.iter().any(|t| t == verb))
            .unwrap()
    }

    #[tokio::test(start_paused = true)]
    async fn a_tmux_send_pastes_bracketed_on_stdin_and_submits_once_unseen() {
        let pane = FakePane::new(PaneState {
            paints: false,
            ..PaneState::default()
        });
        let result = tmux_sender(&pane, FakeProcs::new())
            .deliver(4242, "/model opus", DeliverOpts::default(), NO_TAIL)
            .await
            .unwrap();
        assert!(result.submitted);
        assert_eq!(pane.state().submitted, ["/model opus"]);
        let paste = call_with(&pane, "paste-buffer");
        assert!(paste.contains(&"-p".into()) && paste.contains(&"-d".into()));
        assert_eq!(
            paste[paste.iter().position(|a| a == "-t").unwrap() + 1],
            "%7"
        );
        let load = call_with(&pane, "load-buffer");
        assert_eq!(load.last().unwrap(), "-");
        assert!(
            !load.join(" ").contains("/model opus"),
            "the text goes in on stdin"
        );
        assert!(
            !pane.flat().contains(&"Escape".to_string()),
            "Escape would cancel the agent's turn"
        );
        assert!(
            !pane.verbs().contains(&"capture-pane".to_string()),
            "the pane is never captured"
        );
        assert_eq!(
            pane.verbs().iter().filter(|v| *v == "paste-buffer").count(),
            1
        );
    }

    #[tokio::test(start_paused = true)]
    async fn a_tmux_pane_with_no_agent_or_no_life_is_refused() {
        let pane = FakePane::new(PaneState::default());
        let err = tmux_sender(
            &pane,
            FakeProcs {
                agent: false,
                ..FakeProcs::new()
            },
        )
        .deliver(4242, "/compact", DeliverOpts::default(), NO_TAIL)
        .await
        .unwrap_err();
        assert!(
            err.to_string().contains("is not running an agent (claude)"),
            "{err}"
        );
        assert!(pane.state().submitted.is_empty());
        let dead = FakePane::new(PaneState {
            failing: true,
            ..PaneState::default()
        });
        let err = tmux_sender(&dead, FakeProcs::new())
            .deliver(4242, "/compact", DeliverOpts::default(), NO_TAIL)
            .await
            .unwrap_err();
        assert!(
            err.to_string()
                .contains("attempt 1: display-message -p… failed: boom"),
            "{err}"
        );
    }
}
