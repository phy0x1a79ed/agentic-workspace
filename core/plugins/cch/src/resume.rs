//! The deferred resume: wait for a self-directed command to run, type its follow-up, and keep the
//! promise until the session has taken that follow-up up; plus the `done` and `lost` events that
//! tell reflection how it ended.
//!
//! Each promise is watched by one tokio task. Typed is not delivered: an enqueued line lives only in
//! the worker's memory, and a worker swap loses it, so the promise stays on disk until the
//! transcript shows the resume taken out of the queue and, for `/compact`, a `compact_boundary`.

use std::collections::HashMap;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use awm_harness::claude::{records, roster, ClaudeHome};
use awm_harness::contract::{lost_notice, HarnessEvent, Kind, Reporter};
use awm_plugin_sdk::{ids, procfs, Hub};
use serde_json::{json, Value};
use tokio::time::Instant;

use crate::lane::Opener;
use crate::promises::{
    held, nudged, render_followup, Liveness, Pending, Promises, APPLIED, AWAITING_COMMAND, RESUME_SENT,
};
use crate::sender::{DeliverOpts, DeliveryError, ResumeWatch, SendTail, Sender};
use crate::target::{self, Resolve};
use crate::transcript::{self, Tail};
use crate::watcher::{self, Outcome, Probe, TranscriptView};

/// How long the transcript gets to show a resume arriving. Generous next to the ~100 ms a settled
/// session takes, because this runs detached and costs nobody's turn.
pub const VERIFY_WAIT: Duration = Duration::from_secs(15);
/// How often the transcript is read while verifying.
pub const VERIFY_POLL: Duration = Duration::from_millis(500);

/// How many *deliveries* may fail before the promise is given up as lost. Waiting is deliberately
/// unbounded: a session whose turn ran 63 minutes once had its promise abandoned three minutes
/// before its `/compact` began. A live session that has not reached its command yet is the
/// mechanism working; the answer to a long wait is a pause request, not giving up.
pub const MAX_DELIVERY_ROUNDS: u32 = 4;

/// The hold before re-arming after `n` failed delivery rounds (the last repeats). A `/compact` once
/// ran 94 s and its resume failed four times in 7.7 s, because a re-entered wait saw the start line
/// still there and returned at once. Nothing about a failing lane changes in 40 ms; 30+60+120 s
/// carries the last round past the longest compaction observed.
pub const ROUND_BACKOFF: [Duration; 3] =
    [Duration::from_secs(30), Duration::from_secs(60), Duration::from_secs(120)];

/// Typed before the resume after a move: Claude Code drops a session's MCP servers on `/cd` and
/// does not connect them again, so the session would come back without awm.
pub const RECONNECT_AFTER_MOVE: &str = "/mcp reconnect awm";

/// How long the resume after a self-directed `/mcp` waits for the command's output to reach the
/// transcript. A reconnect prints once the server has answered.
pub const MCP_OUTPUT_WAIT: Duration = Duration::from_secs(30);
/// How often the transcript is read for that output.
pub const MCP_OUTPUT_POLL: Duration = Duration::from_millis(500);

/// Pause requests per promise. Text arriving unbidden in somebody's session is rationed: three
/// across the life of a promise, at most one per round.
pub const NUDGE_LIMIT: u32 = 3;

/// How long a dead pid's session is looked for under a new pid. The daemon swaps an outdated
/// worker in place, and a swap during `/compact` loses the compaction and everything queued
/// behind it (the new process came 3 to 51 s after the command began).
pub const SUCCESSOR_WAIT: Duration = Duration::from_secs(120);
/// How often the successor is looked for.
pub const SUCCESSOR_POLL: Duration = Duration::from_secs(2);
/// How many times the command or the resume is typed again after a worker swap.
pub const MAX_REISSUES: u32 = 2;

/// How long after the resume is typed the session gets to show it taken up, and for `/compact` the
/// `compact_boundary`. The longest compaction measured ran 94 s.
pub const FOLLOW_THROUGH: Duration = Duration::from_secs(1800);
/// How often the transcript is read during the follow-through.
pub const FOLLOW_POLL: Duration = Duration::from_secs(2);

/// The platform code of a Claude Code session in an R9 id.
pub const PLATFORM: &str = "cc";

/// `text` as cch types it into a session on its own account: after `from cch@<node>: `, so no
/// line cch wrote reads as the user's. A follow-up reflection passed in carries its own prefix
/// and is typed as given.
pub fn from_cch(text: &str) -> String {
    format!("from cch@{}: {text}", awm_plugin_sdk::config::node_name())
}
/// The spool of events reflection did not take, beside the promises.
pub const UNSENT_FILE: &str = "unsent.jsonl";

// Statuses that say outright that typing now lands in a queue or behind a modal. `shell` is not
// among them: see `watcher::is_settled`.
const NEVER_SETTLED: [&str; 2] = ["busy", "waiting"];

/// What a follow-through ended in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Verdict {
    Done,
    Rearm,
    Lost,
}

/// What the resume machine asks of a transcript reader beyond the sender; [`Tail`] is the real one.
pub trait ResumeTail: SendTail {
    /// Did `text` reach the session after `since_ms`? A resume is often word for word the last one.
    fn landed_since(&self, text: &str, since_ms: i64) -> Option<bool>;
    /// Was `text` taken from the queue into a turn after `since_ms`? Stricter than landed.
    fn taken_up(&self, text: &str, since_ms: i64) -> Option<bool>;
    /// What the local command `text` printed in a run after `since_ms`.
    fn output(&self, text: &str, since_ms: i64) -> Option<String>;
    /// Follow the same session in the process that took it over.
    fn rebind(&mut self, repl_pid: u32);
    /// The session id the reader last read under.
    fn session_id(&self) -> Option<String>;
}

impl ResumeTail for Tail {
    fn landed_since(&self, text: &str, since_ms: i64) -> Option<bool> {
        Tail::landed(self, text, Some(since_ms))
    }
    fn taken_up(&self, text: &str, since_ms: i64) -> Option<bool> {
        Tail::taken_up(self, text, since_ms)
    }
    fn output(&self, text: &str, since_ms: i64) -> Option<String> {
        Tail::output(self, text, since_ms)
    }
    fn rebind(&mut self, repl_pid: u32) {
        Tail::rebind(self, repl_pid)
    }
    fn session_id(&self) -> Option<String> {
        Tail::session_id(self).map(str::to_string)
    }
}

/// The effects the resume machine has besides typing and the store; [`LiveWorld`] is the real one.
pub trait World: Send + Sync {
    /// Wait for the command to start or the session to settle ([`watcher::await_completion`]).
    fn await_completion<T: TranscriptView, P: Probe>(
        &self,
        repl_pid: u32,
        injected_at_ms: i64,
        label: &str,
        text: &str,
        tail: &mut T,
        probe: &mut P,
    ) -> impl Future<Output = Outcome> + Send;
    /// `(pid, procStart)` of the live process other than `old_pid` that holds `session_id` now.
    fn successor(&self, session_id: &str, old_pid: u32) -> impl Future<Output = Option<(u32, String)>> + Send;
    /// The Remote Control link of `repl_pid`'s session, if it has one.
    fn rc_link(&self, repl_pid: u32) -> impl Future<Output = Option<String>> + Send;
    /// The working directory `pid`'s session record names, for the lost event.
    fn cwd(&self, pid: u32) -> impl Future<Output = Option<String>> + Send;
    /// Pause the machine: between rounds, and between reads of the transcript.
    fn sleep(&self, pause: Duration) -> impl Future<Output = ()> + Send;
}

/// The live [`World`]: Claude Code's records under `home`, the watcher and tokio's clock.
#[derive(Debug, Clone)]
pub struct LiveWorld {
    pub home: ClaudeHome,
}

impl World for LiveWorld {
    async fn await_completion<T: TranscriptView, P: Probe>(
        &self,
        repl_pid: u32,
        injected_at_ms: i64,
        label: &str,
        text: &str,
        tail: &mut T,
        probe: &mut P,
    ) -> Outcome {
        watcher::await_completion(repl_pid, injected_at_ms, label, text, tail, probe).await
    }

    async fn successor(&self, session_id: &str, old_pid: u32) -> Option<(u32, String)> {
        target::successor(&self.home, session_id, Some(old_pid)).await
    }

    async fn rc_link(&self, repl_pid: u32) -> Option<String> {
        let home = self.home.clone();
        let read = tokio::task::spawn_blocking(move || {
            let bridge = match records::job_of_pid(&home, repl_pid) {
                Some(job) => roster::job_bridge_id(&home, &job),
                None => records::pid_bridge_id(&home, repl_pid),
            };
            roster::bridge_link(bridge.as_deref())
        })
        .await;
        read.unwrap_or_else(|e| {
            // The link is a nicety.
            tracing::warn!("cch: could not read the Remote Control link of pid {repl_pid}: {e}");
            None
        })
    }

    async fn cwd(&self, pid: u32) -> Option<String> {
        transcript::session_and_cwd_for(&self.home, pid).await.1
    }

    async fn sleep(&self, pause: Duration) {
        tokio::time::sleep(pause).await
    }
}

// --- events (harness-contract.md § Events) ---

/// The R9 id of a Claude Code session UUID, or empty when there is none or it is not hex.
pub fn r9(session_uuid: &str) -> String {
    if session_uuid.is_empty() {
        return String::new();
    }
    ids::r9(PLATFORM, session_uuid).unwrap_or_else(|e| {
        tracing::warn!("cch: an event names no session: {e}");
        String::new()
    })
}

fn event(item: &Pending, kind: Kind, reason: &str) -> HarnessEvent {
    HarnessEvent {
        ticket: Some(item.ticket.clone()),
        command: Some(item.text.clone()),
        followup: Some(item.followup.clone()),
        reason: Some(reason.into()),
        name: Some(item.name.clone().unwrap_or_default()),
        pid: Some(item.repl_pid.into()),
        injected_at_ms: Some(item.injected_at_ms),
        reissues: Some(item.reissues.into()),
        ..HarnessEvent::new(r9(&item.session_id), kind)
    }
}

/// The `done` event: the session took the follow-up up after the command finished.
pub fn done_event(item: &Pending, reason: &str) -> HarnessEvent {
    event(item, Kind::Done, reason)
}

/// The `lost` event, with the session's folder, whether the session was told, and when.
pub fn lost_event(item: &Pending, reason: &str, cwd: &str, told_session: bool, at_ms: i64) -> HarnessEvent {
    HarnessEvent {
        cwd: Some(cwd.into()),
        told_session: Some(told_session),
        at_ms: Some(at_ms),
        ..event(item, Kind::Lost, reason)
    }
}

/// Where events reflection did not take are spooled: `unsent.jsonl` beside the promises.
pub fn spool_path(promises_dir: &Path) -> PathBuf {
    promises_dir.join(UNSENT_FILE)
}

/// The reporter that sends cch's events to reflection through `hub`, spooling beside the promises.
pub fn reporter(hub: Hub, promises: &Promises) -> Reporter {
    Reporter::new(hub, spool_path(promises.dir()))
}

/// How giving a promise up went: whether the session was told, and whether reflection took the event.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LostReport {
    pub told_session: bool,
    pub reported: bool,
}

fn who(item: &Pending) -> String {
    match item.name.as_deref().filter(|n| !n.is_empty()) {
        Some(name) => name.into(),
        None if !item.session_id.is_empty() => item.session_id.clone(),
        None => format!("pid {}", item.repl_pid),
    }
}

fn shout_lost(item: &Pending, reason: &str) {
    tracing::error!("cch: LOST {} for session {}: {reason}", item.text, who(item));
}

/// The live [`Liveness`] behind [`Promises::load_all`]: `/proc`, the session records, and the
/// `lost` event for a promise whose session has no process left.
#[derive(Clone)]
pub struct LiveLiveness {
    pub home: ClaudeHome,
    pub reporter: Reporter,
}

impl Liveness for LiveLiveness {
    async fn proc_start(&self, pid: u32) -> Option<String> {
        tokio::task::spawn_blocking(move || procfs::start_ticks(pid)).await.ok().flatten()
    }

    async fn successor(&self, session_id: &str, old_pid: u32) -> Option<(u32, String)> {
        target::successor(&self.home, session_id, Some(old_pid)).await
    }

    async fn lost(&self, item: &Pending, reason: &str) {
        shout_lost(item, reason);
        let cwd = transcript::session_and_cwd_for(&self.home, item.repl_pid).await.1;
        let event = lost_event(item, reason, &cwd.unwrap_or_default(), false, watcher::now_ms());
        self.reporter.report(&event).await;
    }
}

// --- the resume machine ---

type Active = Arc<Mutex<HashMap<u32, Vec<i64>>>>;

// A watcher's entry in the active map, removed however the watcher ends, abort included.
struct ActiveEntry {
    active: Active,
    repl_pid: u32,
    injected_at_ms: i64,
}

impl ActiveEntry {
    fn enter(active: &Active, repl_pid: u32, injected_at_ms: i64) -> Self {
        let mut map = active.lock().unwrap_or_else(|e| e.into_inner());
        map.entry(repl_pid).or_default().push(injected_at_ms);
        Self { active: active.clone(), repl_pid, injected_at_ms }
    }
}

impl Drop for ActiveEntry {
    fn drop(&mut self) {
        let mut map = self.active.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(stamps) = map.get_mut(&self.repl_pid) {
            if let Some(at) = stamps.iter().position(|ms| *ms == self.injected_at_ms) {
                stamps.remove(at);
            }
            if stamps.is_empty() {
                map.remove(&self.repl_pid);
            }
        }
    }
}

/// How long to hold off before re-arming, after `failures` failed delivery rounds.
pub fn round_backoff(failures: u32) -> Duration {
    if failures == 0 {
        return Duration::ZERO;
    }
    ROUND_BACKOFF[(failures as usize).min(ROUND_BACKOFF.len()) - 1]
}

/// The pause request: one line, and unmistakably not a new instruction.
///
/// It goes in as plain text, never a slash command, and that asymmetry is why it lands: a queued
/// slash command waits for a turn boundary, while a plain prompt is handed to the running turn.
pub fn nudge_text(item: &Pending, waited_s: i64) -> String {
    from_cch(&format!(
        "[awm reflection] Your queued `{text}` cannot run until the current turn ends, and it has been waiting \
         {minutes} minutes. Please finish the step you are on and end your turn without starting anything new — \
         `{text}` will run then, and reflection will send you \"{followup}\" straight afterwards, so nothing is \
         lost by stopping here. Automated message from the reflection service; it is not a new instruction from \
         the user.",
        text = item.text,
        minutes = waited_s.div_euclid(60),
        followup = item.followup,
    ))
}

fn opts(what: &str) -> DeliverOpts<'_> {
    DeliverOpts { what, ..DeliverOpts::default() }
}

/// The resume machine over its effects: the sender types and reads the session record, `world`
/// waits and looks things up, `promises` is the store and `reporter` carries the events.
///
/// Clones share one active-watcher map, the in-process witness of supersession.
#[derive(Clone)]
pub struct Resumer<D, O, P, W> {
    pub sender: Sender<D, O, P>,
    pub world: W,
    pub promises: Promises,
    pub reporter: Reporter,
    active: Active,
}

impl<D, O, P, W> Resumer<D, O, P, W>
where
    D: Resolve,
    O: Opener,
    P: Probe + Sync,
    W: World,
{
    /// A machine with no watcher active yet.
    pub fn new(sender: Sender<D, O, P>, world: W, promises: Promises, reporter: Reporter) -> Self {
        Self { sender, world, promises, reporter, active: Active::default() }
    }

    /// Is anything visibly wrong with typing into this session right now?
    ///
    /// The last-instant re-check between a wait and a write. It refuses only on what it can see, a
    /// `busy` or `waiting` status or a foreground tool call: the watcher already did the careful
    /// work, and re-running it here would throw a round away over a record mid-rewrite.
    async fn free_to_receive<T: ResumeTail>(&mut self, repl_pid: u32, tail: &mut T) -> bool {
        if let Some((status, _)) = self.sender.probe.read_status(repl_pid).await {
            if NEVER_SETTLED.contains(&status.as_str()) {
                return false;
            }
        }
        tail.poll().await;
        tail.tool_call_in_flight() != Some(true)
    }

    /// Did `text` reach the session? `Some(false)` only when a read in this window got through and
    /// never mentioned it; `None` when none did, which must not license a second delivery. A tail
    /// that has lost its file still answers from older reads, so those count only with a fresh one.
    pub async fn verify_landed<T: SendTail>(&self, tail: &mut T, text: &str) -> Option<bool> {
        let deadline = Instant::now() + VERIFY_WAIT;
        let mut read = false;
        loop {
            read = tail.poll().await || read;
            if tail.landed(text) == Some(true) {
                return Some(true);
            }
            if Instant::now() >= deadline {
                break;
            }
            self.world.sleep(VERIFY_POLL).await;
        }
        if read { tail.landed(text) } else { None }
    }

    /// Keep the promise another round, and ask the session to pause. Holding is silent by itself,
    /// and silence is how an hour goes by: the session cannot see it is sitting on its own command.
    async fn hold_and_nudge<T: ResumeTail>(&mut self, item: Pending, tail: &mut T) -> Pending {
        let who = who(&item);
        let reason = format!("still waiting for {} to run (round {})", item.text, item.holds + 1);
        if item.nudges >= NUDGE_LIMIT {
            let item = held(&item, &format!("{reason}; asked {} times already, now holding quietly", item.nudges));
            self.promises.record(&item).await;
            return item;
        }
        let waited_s = ((self.sender.probe.now_ms() - item.injected_at_ms).div_euclid(1000)).max(0);
        let text = nudge_text(&item, waited_s);
        tail.watch(&text);
        let item = match self.sender.deliver(item.repl_pid, &text, opts("a pause request"), Some(tail)).await {
            Ok(_) => held(&nudged(&item), &format!("{reason}; asked the session to pause")),
            Err(e) => {
                // A nudge is a courtesy, not the promise: failing one must not spend the resume's
                // delivery budget.
                tracing::warn!("cch: could not ask session {who} to pause; still holding its resume: {e}");
                held(&item, &format!("{reason}; the pause request could not be delivered"))
            }
        };
        self.promises.record(&item).await;
        item
    }

    /// Has a newer promise replaced `item` for its session? A session owes one resume, and an older
    /// watcher that carried on would deliver a second and clear the first's record. Three witnesses:
    /// the active watchers, the `.done` stamp and the record on disk.
    pub async fn superseded(&self, item: &Pending) -> bool {
        {
            let map = self.active.lock().unwrap_or_else(|e| e.into_inner());
            if map.get(&item.repl_pid).is_some_and(|s| s.iter().any(|ms| *ms > item.injected_at_ms)) {
                return true;
            }
        }
        if self.promises.delivered_ms(item.repl_pid).await > item.injected_at_ms {
            return true;
        }
        self.promises.read(item.repl_pid).await.is_some_and(|d| d.injected_at_ms != item.injected_at_ms)
    }

    /// `item` with each `/mcp` change carrying what the command printed.
    pub async fn mcp_outputs<T: ResumeTail>(&self, item: Pending, tail: &mut T) -> Pending {
        tail.poll().await;
        // Claude Code records a local command only when it is submitted to an idle prompt; one
        // drained from the queue leaves no start line and no output.
        let owed: Vec<(String, String)> = item
            .changes
            .iter()
            .filter(|c| c.kind == "mcp" && c.detail.is_empty())
            .map(|c| (c.key.clone(), format!("/mcp {}", c.value)))
            .filter(|(_, line)| tail.started(line, Some(item.injected_at_ms)) == Some(true))
            .collect();
        if owed.is_empty() {
            return item;
        }
        let deadline = Instant::now() + MCP_OUTPUT_WAIT;
        let mut said: HashMap<String, String> = HashMap::new();
        loop {
            tail.poll().await;
            for (key, line) in &owed {
                if let Some(out) = tail.output(line, item.injected_at_ms) {
                    said.insert(key.clone(), out);
                }
            }
            if said.len() == owed.len() || Instant::now() >= deadline {
                break;
            }
            self.world.sleep(MCP_OUTPUT_POLL).await;
        }
        let changes = item
            .changes
            .iter()
            .map(|c| match said.get(&c.key) {
                Some(out) => c.clone().ended(APPLIED, out),
                None => c.clone(),
            })
            .collect();
        Pending { changes, ..item }
    }

    /// `item` with the resume worded from what its changes did by now: an outcome is known only
    /// once its dialog has been answered.
    async fn resume_text<T: ResumeTail>(&self, mut item: Pending, tail: &mut T) -> Pending {
        if item.changes.is_empty() {
            // The default is cch's own line; a follow-up the caller gave is typed as given.
            if item.followup.trim() == crate::guards::DEFAULT_FOLLOWUP {
                item.followup = from_cch(crate::guards::DEFAULT_FOLLOWUP);
            }
            return item;
        }
        if let Some(on_disk) = self.promises.read(item.repl_pid).await {
            if on_disk.injected_at_ms == item.injected_at_ms {
                item.changes = on_disk.changes;
            }
        }
        let item = self.mcp_outputs(item, tail).await;
        let link = if item.changes.iter().any(|c| c.kind == "rc_on") {
            self.world.rc_link(item.repl_pid).await
        } else {
            None
        };
        Pending { followup: from_cch(&render_followup(&item.changes, link.as_deref(), &item.closing)), ..item }
    }

    /// Wait for the command to run, type the resume, and hold the promise until it is taken up.
    ///
    /// A resume goes in once the command is running (anything typed then is drained into the turn
    /// on the other side of it) or once the session is between turns, never before the session
    /// reaches the command: queued ahead of nothing and behind the command, it would run first, on
    /// the old context. Such a session is waited on again for as long as it lives, and asked once a
    /// round to pause. Only failing deliveries are bounded, by [`MAX_DELIVERY_ROUNDS`].
    pub async fn await_and_resume<T: ResumeTail>(&mut self, item: Pending, mut tail: T) {
        let who = who(&item);
        tail.watch(&item.text);
        tail.watch(&item.followup);
        let _active = ActiveEntry::enter(&self.active, item.repl_pid, item.injected_at_ms);
        self.run(item, &mut tail, &who).await;
    }

    async fn alive(&mut self, item: &Pending) -> bool {
        self.sender
            .probe
            .proc_start(item.repl_pid)
            .await
            .is_some_and(|started| item.proc_start.is_empty() || started == item.proc_start)
    }

    /// `item` moved to the process that took over its session, or None after [`SUCCESSOR_WAIT`].
    async fn await_successor(&self, item: &Pending, who: &str) -> Option<Pending> {
        let deadline = Instant::now() + SUCCESSOR_WAIT;
        loop {
            if let Some((pid, started)) = self.world.successor(&item.session_id, item.repl_pid).await {
                tracing::warn!(
                    "cch: session {who} moved from pid {} to pid {pid}, a worker swap; following it",
                    item.repl_pid
                );
                let moved = Pending { repl_pid: pid, proc_start: started, ..item.clone() };
                self.promises.rekey(item.repl_pid, &moved).await;
                return Some(moved);
            }
            if Instant::now() >= deadline {
                return None;
            }
            self.world.sleep(SUCCESSOR_POLL).await;
        }
    }

    /// Did the command finish before its worker was swapped out? A swap during `/compact` leaves the
    /// start line and loses the compaction, so for it only the boundary counts.
    async fn command_survived<T: ResumeTail>(tail: &mut T, item: &Pending) -> bool {
        tail.poll().await;
        if watcher::is_compaction(&item.text) {
            return tail.compacted(Some(item.injected_at_ms)) == Some(true);
        }
        tail.started(&item.text, Some(item.injected_at_ms)) == Some(true)
    }

    /// Type the command again into the session's new worker.
    async fn reissue<T: ResumeTail>(&mut self, item: Pending, tail: &mut T, who: &str) -> Result<Pending, DeliveryError> {
        let now = self.sender.probe.now_ms();
        tail.rebind(item.repl_pid);
        tail.watch(&item.text);
        tracing::warn!(
            "cch: typing {} again into session {who}, whose worker was swapped before it finished (re-issue {} of \
             {MAX_REISSUES})",
            item.text,
            item.reissues + 1
        );
        self.sender
            .deliver(item.repl_pid, &item.text, opts("the command again after a worker swap"), Some(tail))
            .await?;
        let item = Pending {
            injected_at_ms: now,
            phase: AWAITING_COMMAND.into(),
            reissues: item.reissues + 1,
            last_outcome: "typed again after a worker swap".into(),
            ..item
        };
        self.promises.record(&item).await;
        Ok(item)
    }

    /// Forget the promise and say out loud that it was lost: an ERROR line, a notice typed into a
    /// live session, and the `lost` event.
    pub async fn lost(&mut self, item: &Pending, reason: &str, alive: bool) -> LostReport {
        self.promises.clear(item.repl_pid).await;
        shout_lost(item, reason);
        let mut told = false;
        if alive {
            let notice = from_cch(&lost_notice(&item.text, &item.followup, reason));
            match self.sender.deliver(item.repl_pid, &notice, opts("the lost-resume notice"), None::<&mut Tail>).await {
                Ok(_) => told = true,
                Err(e) => tracing::warn!("cch: could not tell session {} its resume was lost: {e}", who(item)),
            }
        }
        let cwd = self.world.cwd(item.repl_pid).await.unwrap_or_default();
        let event = lost_event(item, reason, &cwd, told, watcher::now_ms());
        let reported = self.reporter.report(&event).await;
        LostReport { told_session: told, reported }
    }

    async fn done(&self, item: &Pending, reason: &str) {
        self.reporter.report(&done_event(item, reason)).await;
    }

    /// Hold the promise until the session has taken the resume up.
    ///
    /// The resume waits in the worker's memory until the command ends, and a worker swap in that
    /// window loses both, so a swap sends the command or the resume again into the new worker.
    async fn follow_through<T: ResumeTail>(&mut self, mut item: Pending, tail: &mut T, who: &str) -> (Verdict, Pending) {
        let compaction = watcher::is_compaction(&item.text);
        tail.watch(&item.text);
        tail.watch(&item.followup);
        let deadline = Instant::now() + FOLLOW_THROUGH;
        loop {
            tail.poll().await;
            if self.superseded(&item).await {
                self.done(&item, "a newer command for the session replaced it").await;
                return (Verdict::Done, item);
            }
            let Some(took) = tail.taken_up(&item.followup, item.injected_at_ms) else {
                // No transcript to read, now or ever: typing the resume twice is the worse mistake.
                self.promises.clear(item.repl_pid).await;
                self.done(&item, "typed; the session has no transcript to confirm it").await;
                return (Verdict::Done, item);
            };
            let finished = !compaction || tail.compacted(Some(item.injected_at_ms)) == Some(true);
            if took && finished {
                self.promises.clear(item.repl_pid).await;
                self.done(&item, "").await;
                tracing::info!(
                    "cch: session {who} took up its resume{}",
                    if compaction { " after the compaction finished" } else { "" }
                );
                return (Verdict::Done, item);
            }
            if took {
                let reason = "the session took up its resume, but no compact_boundary followed the /compact, so the \
                              compaction did not finish";
                self.lost(&item, reason, true).await;
                return (Verdict::Lost, item);
            }
            if !self.alive(&item).await {
                let Some(moved) = self.await_successor(&item, who).await else {
                    let reason = "the session's process went away after its resume was typed and before it was taken \
                                  up, and no new process took over the session";
                    self.lost(&item, reason, false).await;
                    return (Verdict::Lost, item);
                };
                item = moved;
                tail.rebind(item.repl_pid);
                if item.reissues >= MAX_REISSUES {
                    let reason = format!("the session's worker was swapped again after {MAX_REISSUES} re-issues");
                    self.lost(&item, &reason, true).await;
                    return (Verdict::Lost, item);
                }
                if compaction && !Self::command_survived(tail, &item).await {
                    return match self.reissue(item.clone(), tail, who).await {
                        Ok(reissued) => (Verdict::Rearm, reissued),
                        Err(e) => self.swap_refused(item, &e).await,
                    };
                }
                let what = opts("the resume again after a worker swap");
                if let Err(e) = self.sender.deliver(item.repl_pid, &item.followup, what, Some(tail)).await {
                    return self.swap_refused(item, &e).await;
                }
                item = Pending {
                    reissues: item.reissues + 1,
                    last_outcome: "resume typed again after a worker swap".into(),
                    ..item
                };
                self.promises.record(&item).await;
                continue;
            }
            if Instant::now() >= deadline {
                let reason = format!(
                    "the session did not take up its resume{} within {} minutes",
                    if compaction { " or finish the compaction" } else { "" },
                    FOLLOW_THROUGH.as_secs() / 60
                );
                self.lost(&item, &reason, true).await;
                return (Verdict::Lost, item);
            }
            self.world.sleep(FOLLOW_POLL).await;
        }
    }

    async fn swap_refused(&mut self, item: Pending, e: &DeliveryError) -> (Verdict, Pending) {
        let reason = format!("its worker was swapped and the new one would not take the text: {e}");
        self.lost(&item, &reason, true).await;
        (Verdict::Lost, item)
    }

    /// `item` under the session id its process writes now. A /clear gives the session a new id in
    /// the same process, and a worker swap or a restart looks the session up by id.
    pub async fn follow_session_id<T: ResumeTail>(&self, item: Pending, tail: &T, who: &str) -> Pending {
        let Some(current) = tail.session_id().filter(|c| !c.is_empty() && *c != item.session_id) else {
            return item;
        };
        tracing::info!("cch: session {who} now writes as {current} (a /clear); its promise follows");
        let item = Pending { session_id: current, ..item };
        self.promises.record(&item).await;
        item
    }

    async fn run<T: ResumeTail>(&mut self, mut item: Pending, tail: &mut T, who: &str) {
        let mut failures = 0;
        while failures < MAX_DELIVERY_ROUNDS {
            if item.phase == RESUME_SENT {
                let (verdict, next) = self.follow_through(item, tail, who).await;
                item = next;
                if verdict == Verdict::Rearm {
                    continue;
                }
                return;
            }
            // Spacing between failed *deliveries* only. A round that ended in a hold re-arms at
            // once: that is the wait working, and it is unbounded by design.
            let pause = round_backoff(failures);
            if !pause.is_zero() {
                tracing::info!(
                    "cch: holding {}s before the next delivery round for session {who} ({failures} failed so far)",
                    pause.as_secs()
                );
                self.world.sleep(pause).await;
            }
            let mut outcome = self
                .world
                .await_completion(item.repl_pid, item.injected_at_ms, who, &item.text, tail, &mut self.sender.probe)
                .await;
            if self.superseded(&item).await {
                tracing::info!(
                    "cch: a newer command replaced the one session {who} was waiting on; its resume is that \
                     command's to send"
                );
                return;
            }
            item = self.follow_session_id(item, tail, who).await;
            if outcome == Outcome::Vanished {
                let Some(moved) = self.await_successor(&item, who).await else {
                    let reason =
                        "the session's process went away before its command ran, and no new process took over the session";
                    self.lost(&item, reason, false).await;
                    return;
                };
                item = moved;
                tail.rebind(item.repl_pid);
                if Self::command_survived(tail, &item).await {
                    continue;
                }
                if item.reissues >= MAX_REISSUES {
                    let reason = format!("the session's worker was swapped again after {MAX_REISSUES} re-issues");
                    self.lost(&item, &reason, true).await;
                    return;
                }
                match self.reissue(item.clone(), tail, who).await {
                    Ok(reissued) => item = reissued,
                    Err(e) => {
                        failures += 1;
                        item = held(&item, &format!("could not type the command again after a worker swap: {e}"));
                        self.promises.record(&item).await;
                    }
                }
                continue;
            }
            if outcome == Outcome::TimedOut {
                // Re-ask before nudging: a round once expired 377 ms before the `/compact` start
                // line, and a pause request typed then is drained into the fresh context.
                tail.poll().await;
                if !watcher::has_started(tail, &item.text, item.injected_at_ms) {
                    item = self.hold_and_nudge(item, tail).await;
                    continue;
                }
                tracing::info!(
                    "cch: session {who} reached its command just as the round expired; sending its resume rather \
                     than a pause request"
                );
                outcome = Outcome::Started;
            }
            // A session seen to have started its command is mid-turn by definition, and queuing is
            // what we want of it: the free-to-receive check is for the settled path alone.
            if outcome == Outcome::Settled && !self.free_to_receive(item.repl_pid, tail).await {
                item = held(&item, "the session started a turn between settling and the resume going in");
                self.promises.record(&item).await;
                continue;
            }
            if self.superseded(&item).await {
                return;
            }
            item = self.resume_text(item, tail).await;
            tail.watch(&item.followup);

            // A copy from an earlier round can turn up after its verify gave up on it: a paste
            // queued behind `/compact` shows only when the compaction ends.
            tail.poll().await;
            if tail.landed_since(&item.followup, item.injected_at_ms) == Some(true) {
                tracing::info!("cch: the resume for session {who} arrived from an earlier round; not typing it again");
                item.phase = RESUME_SENT.into();
                self.promises.mark_delivered(item.repl_pid, item.injected_at_ms).await;
                self.promises.record(&item).await;
                continue;
            }

            // `resume_sent` before the resume is typed, so a restart in the gap replays a
            // follow-through, which never types it again unless a worker swap lost it.
            item.phase = RESUME_SENT.into();
            self.promises.mark_delivered(item.repl_pid, item.injected_at_ms).await;
            self.promises.record(&item).await;
            if item.changes.iter().any(|c| c.kind == "cd") {
                self.reconnect(item.repl_pid, who, tail).await;
            }
            let delivered =
                self.sender.deliver(item.repl_pid, &item.followup, opts("the deferred resume"), Some(tail)).await;
            if let Err(e) = delivered {
                // Back to waiting, keeping the original `injected_at_ms`: a fresh one would make the
                // next wait sit out its cap for a reaction that already happened.
                failures += 1;
                item.phase = AWAITING_COMMAND.into();
                item = held(&item, &format!("could not be delivered: {e}"));
                self.promises.record(&item).await;
                tracing::error!("cch: could not deliver the deferred resume to session {who}; it is still pending: {e}");
                continue;
            }

            match self.verify_landed(tail, &item.followup).await {
                // No transcript to read: no licence to type the same resume twice.
                None => {
                    self.promises.clear(item.repl_pid).await;
                    return;
                }
                Some(true) => continue,
                Some(false) => {}
            }
            failures += 1;
            item.phase = AWAITING_COMMAND.into();
            item = held(&item, "delivered, but the session's transcript never showed it arriving");
            self.promises.record(&item).await;
            tracing::warn!(
                "cch: the deferred resume for session {who} was sent but its transcript never showed it arriving; \
                 holding the promise and waiting for the session again"
            );
        }
        let reason = format!("its resume could not be delivered after {MAX_DELIVERY_ROUNDS} rounds");
        let alive = self.alive(&item).await;
        self.lost(&item, &reason, alive).await;
    }

    /// Type [`RECONNECT_AFTER_MOVE`]; a failure is logged and the resume still goes in.
    async fn reconnect<T: ResumeTail>(&mut self, repl_pid: u32, who: &str, tail: &mut T) {
        let what = opts("the MCP reconnect after a move");
        if let Err(e) = self.sender.deliver(repl_pid, RECONNECT_AFTER_MOVE, what, Some(tail)).await {
            tracing::warn!("cch: could not reconnect awm for session {who} after its move: {e}");
        }
    }
}

/// The live [`ResumeWatch`]: one detached tokio task per promise, so the wait outlives the verb
/// that armed it. There is no transport branch: the wait reads the session record, and delivery
/// detects the lane again when the time comes.
#[derive(Clone)]
pub struct LiveWatch<D, O, P, W> {
    pub resumer: Resumer<D, O, P, W>,
}

impl<T, D, O, P, W> ResumeWatch<T> for LiveWatch<D, O, P, W>
where
    T: ResumeTail + Sync + 'static,
    D: Resolve + Clone + 'static,
    O: Opener + Clone + 'static,
    P: Probe + Sync + Clone + 'static,
    W: World + Clone + 'static,
{
    async fn arm(&mut self, promise: Pending, tail: T) {
        let mut resumer = self.resumer.clone();
        tokio::spawn(async move { resumer.await_and_resume(promise, tail).await });
    }
}

/// Re-arm the watchers for the resumes a previous process of this service promised and did not
/// deliver. Called once on boot: every restart takes the in-flight watchers with it, and without
/// this a session that compacted at the wrong moment is left idle forever.
///
/// Which promises survive is [`Promises::load_all`]'s question, asked of the process. Reachability
/// is not checked (the lane is detected again at delivery, perhaps minutes away), nor is age. Each
/// survivor is armed with a reader from `open_tail`. Answers `{"pending": n, "resumed": n}`.
pub async fn replay_pending<L, T, W>(
    promises: &Promises,
    live: &L,
    now_ms: i64,
    open_tail: impl Fn(u32) -> T,
    watch: &mut W,
) -> Value
where
    L: Liveness,
    W: ResumeWatch<T>,
{
    let items = promises.load_all(now_ms, live).await;
    let pending = items.len();
    let mut resumed = 0;
    for item in items {
        tracing::info!(
            "cch: re-arming the deferred resume for session {} (a command was injected before this service \
             restarted)",
            who(&item)
        );
        let tail = open_tail(item.repl_pid);
        watch.arm(item, tail).await;
        resumed += 1;
    }
    json!({"pending": pending, "resumed": resumed})
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::sync::atomic::{AtomicU32, Ordering};

    use awm_harness::contract::events::Sender as EventSender;
    use tokio::sync::Notify;

    use super::*;
    use crate::guards;
    use crate::lane::{Key, Lane, LaneError};
    use crate::promises::tests::SID;
    use crate::promises::{Change, FAILED};
    use crate::sender::SendRequest;
    use crate::target::{ResolveError, Target, TmuxLane};
    use crate::tmux::TmuxError;

    const OLD: u32 = 4242;
    const CONTINUE: &str = guards::DEFAULT_FOLLOWUP;
    // One fixed moment: the stamp tells a watcher its promise is still the newest, so two promises
    // built for one test must agree on it.
    const PROMISED_AT_MS: i64 = 1_700_000_000_000;

    type Hook = Box<dyn FnMut(&mut Script, &str) + Send>;

    /// One session as a test scripts it: its processes, its record, what was typed into it, what
    /// the waits answer, and what reflection was sent.
    struct Script {
        alive: HashMap<u32, String>,
        status: Option<(String, i64)>,
        unresolvable: bool,
        lane_gone: bool,
        seen: Vec<String>,
        on_write: Option<Hook>,
        snapshot: bool,
        phases_on_disk: Vec<Vec<String>>,
        outcomes: VecDeque<Outcome>,
        // What every wait answers once `outcomes` is spent; None stops the machine there.
        then: Option<Outcome>,
        on_wait: Option<Box<dyn FnMut() + Send>>,
        waits: Vec<i64>,
        slept: Vec<Duration>,
        cwd: Option<String>,
        replies: VecDeque<Value>,
        events: Vec<Value>,
        // The swap tests' transcript.
        landed: bool,
        taken: bool,
        compacted: bool,
    }

    impl Default for Script {
        fn default() -> Self {
            Self {
                alive: HashMap::from([(OLD, "999".to_string())]),
                status: None,
                unresolvable: false,
                lane_gone: false,
                seen: Vec::new(),
                on_write: None,
                snapshot: false,
                phases_on_disk: Vec::new(),
                outcomes: VecDeque::new(),
                then: Some(Outcome::Settled),
                on_wait: None,
                waits: Vec::new(),
                slept: Vec::new(),
                cwd: None,
                replies: VecDeque::new(),
                events: Vec::new(),
                landed: false,
                taken: false,
                compacted: false,
            }
        }
    }

    impl Script {
    }

    type Shared = Arc<Mutex<Script>>;

    fn lock(s: &Shared) -> std::sync::MutexGuard<'_, Script> {
        s.lock().unwrap()
    }

    #[derive(Clone)]
    struct FakeDetect(Shared);

    impl Resolve for FakeDetect {
        async fn resolve(&self, _repl_pid: u32, _expect: Option<&str>) -> Result<Target, LaneError> {
            if lock(&self.0).unresolvable {
                return Err(ResolveError("mid re-host".into()).into());
            }
            Ok(Target::Tmux(TmuxLane {
                pane: "%7".into(),
                session_id: SID.into(),
                repl_pid: OLD,
                name: Some("test".into()),
            }))
        }
    }

    /// A lane that takes anything: a committed line is "seen", then the script's hook runs.
    struct FakeWriter {
        s: Shared,
        promises: Promises,
        typed: String,
    }

    impl Lane for FakeWriter {
        fn label(&self) -> String {
            "the fake lane".into()
        }
        async fn write(&mut self, text: &str) -> Result<(), LaneError> {
            self.typed = text.into();
            Ok(())
        }
        async fn clear(&mut self) -> Result<(), LaneError> {
            Ok(())
        }
        async fn check_not_rejected(&mut self) -> Result<(), LaneError> {
            Ok(())
        }
        async fn commit(&mut self) -> Result<(), LaneError> {
            let snapshot = {
                let mut s = lock(&self.s);
                s.seen.push(self.typed.clone());
                if let Some(mut hook) = s.on_write.take() {
                    hook(&mut s, &self.typed);
                    s.on_write = Some(hook);
                }
                s.snapshot
            };
            if snapshot {
                let phases = self.promises.read(OLD).await.into_iter().map(|p| p.phase).collect();
                lock(&self.s).phases_on_disk.push(phases);
            }
            Ok(())
        }
        async fn read(&mut self) -> Result<String, LaneError> {
            panic!("the resume never reads a screen")
        }
        async fn glance(&mut self) -> Result<String, LaneError> {
            panic!("the resume never reads a screen")
        }
        async fn repaint(&mut self) -> Result<bool, LaneError> {
            panic!("the resume never repaints")
        }
        fn forget(&mut self) {
            panic!("the resume never reads a screen")
        }
        async fn press(&mut self, _key: Key) -> Result<(), LaneError> {
            panic!("the resume presses no keys")
        }
        async fn close(self) {}
    }

    #[derive(Clone)]
    struct FakeOpener {
        s: Shared,
        promises: Promises,
    }

    impl FakeOpener {
        fn open(&self) -> Result<FakeWriter, LaneError> {
            if lock(&self.s).lane_gone {
                return Err(TmuxError("pane is gone".into()).into());
            }
            Ok(FakeWriter { s: self.s.clone(), promises: self.promises.clone(), typed: String::new() })
        }
    }

    impl Opener for FakeOpener {
        type Lane = FakeWriter;
        async fn open_writer(&self, _target: &Target) -> Result<FakeWriter, LaneError> {
            self.open()
        }
        async fn open_session(&self, _target: &Target) -> Result<FakeWriter, LaneError> {
            self.open()
        }
    }

    #[derive(Clone)]
    struct FakeProbe(Shared);

    impl Probe for FakeProbe {
        async fn read_status(&mut self, _pid: u32) -> Option<(String, i64)> {
            lock(&self.0).status.clone()
        }
        async fn proc_start(&mut self, pid: u32) -> Option<String> {
            lock(&self.0).alive.get(&pid).cloned()
        }
        fn now_ms(&self) -> i64 {
            watcher::now_ms()
        }
    }

    fn successor_in(s: &Shared, session_id: &str, old_pid: u32) -> Option<(u32, String)> {
        let s = lock(s);
        s.alive
            .iter()
            .find(|(pid, _)| **pid != old_pid && session_id == SID)
            .map(|(pid, started)| (*pid, started.clone()))
    }

    #[derive(Clone)]
    struct FakeWorld {
        s: Shared,
        stop: Arc<Notify>,
    }

    impl World for FakeWorld {
        async fn await_completion<T: TranscriptView, P: Probe>(
            &self,
            _repl_pid: u32,
            injected_at_ms: i64,
            _label: &str,
            _text: &str,
            tail: &mut T,
            _probe: &mut P,
        ) -> Outcome {
            let moves = lock(&self.s).on_wait.is_some();
            if moves {
                // The watcher has been reading the old file all along.
                tail.poll().await;
            }
            let next = {
                let mut s = lock(&self.s);
                s.waits.push(injected_at_ms);
                if let Some(hook) = s.on_wait.as_mut() {
                    hook();
                }
                s.outcomes.pop_front().or(s.then)
            };
            match next {
                Some(outcome) => outcome,
                None => {
                    // The wait is unbounded by design, so a test that wants several rounds ends it here.
                    self.stop.notify_one();
                    std::future::pending().await
                }
            }
        }
        async fn successor(&self, session_id: &str, old_pid: u32) -> Option<(u32, String)> {
            successor_in(&self.s, session_id, old_pid)
        }
        async fn rc_link(&self, _repl_pid: u32) -> Option<String> {
            None
        }
        async fn cwd(&self, _pid: u32) -> Option<String> {
            lock(&self.s).cwd.clone()
        }
        async fn sleep(&self, pause: Duration) {
            lock(&self.s).slept.push(pause);
            tokio::time::sleep(pause).await
        }
    }

    /// The script's processes, for the store's own liveness checks.
    struct ScriptLiveness {
        s: Shared,
        reporter: Reporter,
    }

    impl Liveness for ScriptLiveness {
        async fn proc_start(&self, pid: u32) -> Option<String> {
            lock(&self.s).alive.get(&pid).cloned()
        }
        async fn successor(&self, session_id: &str, old_pid: u32) -> Option<(u32, String)> {
            successor_in(&self.s, session_id, old_pid)
        }
        async fn lost(&self, item: &Pending, reason: &str) {
            shout_lost(item, reason);
            self.reporter.report(&lost_event(item, reason, "", false, watcher::now_ms())).await;
        }
    }

    type Machine = Resumer<FakeDetect, FakeOpener, FakeProbe, FakeWorld>;

    struct Harness {
        s: Shared,
        stop: Arc<Notify>,
        _dir: tempfile::TempDir,
        promises: Promises,
        machine: Machine,
    }

    fn harness() -> Harness {
        let s: Shared = Arc::default();
        let dir = tempfile::tempdir().unwrap();
        let promises = Promises::new(dir.path().join("pending"));
        let events = s.clone();
        let send: EventSender = Arc::new(move |event: Value| {
            let mut script = lock(&events);
            script.events.push(event);
            let reply = script.replies.pop_front().unwrap_or_else(|| json!({"ok": true}));
            Box::pin(async move { Ok(reply) })
        });
        let reporter = Reporter::with_sender(send, spool_path(promises.dir()));
        let stop = Arc::new(Notify::new());
        let sender = Sender {
            detect: FakeDetect(s.clone()),
            opener: FakeOpener { s: s.clone(), promises: promises.clone() },
            probe: FakeProbe(s.clone()),
        };
        let world = FakeWorld { s: s.clone(), stop: stop.clone() };
        let machine = Resumer::new(sender, world, promises.clone(), reporter);
        Harness { s, stop, _dir: dir, promises, machine }
    }

    impl Harness {
        fn script(&self) -> std::sync::MutexGuard<'_, Script> {
            lock(&self.s)
        }

        fn outcomes(&self, outcomes: Vec<Outcome>, then: Option<Outcome>) {
            let mut s = self.script();
            s.outcomes = outcomes.into();
            s.then = then;
        }

        fn seen(&self) -> Vec<String> {
            self.script().seen.clone()
        }

        fn liveness(&self) -> ScriptLiveness {
            ScriptLiveness { s: self.s.clone(), reporter: self.machine.reporter.clone() }
        }

        async fn load(&self) -> Vec<Pending> {
            self.promises.load_all(watcher::now_ms(), &self.liveness()).await
        }

        /// The `lost` events sent so far, newest first.
        fn lost_events(&self) -> Vec<Value> {
            self.script().events.iter().rev().filter(|e| e["kind"] == "lost").cloned().collect()
        }

        /// Run the machine on `item`; true when it ran out of outcomes and stopped in a wait.
        async fn run<T: ResumeTail + Sync>(&self, item: Pending, tail: T) -> bool {
            let mut machine = self.machine.clone();
            tokio::select! {
                () = machine.await_and_resume(item, tail) => false,
                () = self.stop.notified() => true,
            }
        }

        async fn send(&self, text: &str, change: Option<Change>, followup: Option<&str>) {
            let req = SendRequest {
                change,
                followup: followup.map(str::to_string),
                ..SendRequest::new(text, Some(OLD))
            };
            self.machine.sender.clone().send(&req, &self.promises, told(None, None), &mut NoWatch).await.unwrap();
        }
    }

    fn promise() -> Pending {
        Pending {
            name: Some("test".into()),
            hosting: "tmux".into(),
            ticket: "t-1".into(),
            ..Pending::new(OLD, "999", SID, "/compact", "resume", PROMISED_AT_MS)
        }
    }

    struct NoWatch;

    impl<T: Send> ResumeWatch<T> for NoWatch {
        async fn arm(&mut self, _promise: Pending, _tail: T) {}
    }

    /// A transcript that answers whatever the test says it answers.
    struct Told {
        in_flight: Option<bool>,
        landed: Option<bool>,
        started: Option<bool>,
        compacted: Option<bool>,
        // Some: a copy turns up once anything has been typed.
        landed_since_typed: Option<Shared>,
        session_id: Option<String>,
        polls: u32,
        // The poll from which the `/mcp` output is in; None means it must never be asked for.
        output_from_poll: Option<u32>,
    }

    fn told(in_flight: Option<bool>, landed: Option<bool>) -> Told {
        Told {
            in_flight,
            landed,
            started: Some(false),
            compacted: Some(false),
            landed_since_typed: None,
            session_id: None,
            polls: 0,
            output_from_poll: None,
        }
    }

    /// What an unreadable transcript answers: nothing.
    fn unread() -> Told {
        Told { started: None, compacted: None, ..told(None, None) }
    }

    impl TranscriptView for Told {
        async fn poll(&mut self) -> bool {
            self.polls += 1;
            self.in_flight.is_some()
        }
        fn watch(&mut self, _text: &str) {}
        fn tool_call_in_flight(&self) -> Option<bool> {
            self.in_flight
        }
        fn queued(&self, _text: &str) -> Option<bool> {
            self.in_flight.map(|_| false)
        }
        fn consumed(&self, _text: &str) -> Option<bool> {
            self.landed
        }
        fn started(&self, _text: &str, _since_ms: Option<i64>) -> Option<bool> {
            self.started
        }
        fn compacted(&self, _since_ms: Option<i64>) -> Option<bool> {
            self.compacted
        }
    }

    impl SendTail for Told {
        fn expect(&mut self, _text: &str) {}
        fn landed(&self, _text: &str) -> Option<bool> {
            self.landed
        }
    }

    impl ResumeTail for Told {
        fn landed_since(&self, _text: &str, _since_ms: i64) -> Option<bool> {
            Some(self.landed_since_typed.as_ref().is_some_and(|s| !lock(s).seen.is_empty()))
        }
        fn taken_up(&self, _text: &str, _since_ms: i64) -> Option<bool> {
            // Unknown, so the follow-through settles at once; the swap tests cover what comes after.
            None
        }
        fn output(&self, line: &str, since_ms: i64) -> Option<String> {
            let from = self.output_from_poll.expect("waited for output that is never recorded");
            assert_eq!((line, since_ms), ("/mcp reconnect awm", 1000));
            (self.polls >= from).then(|| "Reconnected to the test server".to_string())
        }
        fn rebind(&mut self, _repl_pid: u32) {}
        fn session_id(&self) -> Option<String> {
            self.session_id.clone()
        }
    }

    /// The swap tests' transcript, moved along by what gets typed.
    struct Swapped {
        s: Shared,
        // Some: `taken_up` answers yes from this call on, counting calls.
        taken_from_call: Option<(u32, Arc<AtomicU32>)>,
    }

    impl TranscriptView for Swapped {
        async fn poll(&mut self) -> bool {
            true
        }
        fn watch(&mut self, _text: &str) {}
        fn tool_call_in_flight(&self) -> Option<bool> {
            Some(false)
        }
        fn queued(&self, _text: &str) -> Option<bool> {
            Some(false)
        }
        fn consumed(&self, _text: &str) -> Option<bool> {
            Some(lock(&self.s).taken)
        }
        fn started(&self, _text: &str, _since_ms: Option<i64>) -> Option<bool> {
            Some(true)
        }
        fn compacted(&self, _since_ms: Option<i64>) -> Option<bool> {
            Some(lock(&self.s).compacted)
        }
    }

    impl SendTail for Swapped {
        fn expect(&mut self, _text: &str) {}
        fn landed(&self, _text: &str) -> Option<bool> {
            Some(lock(&self.s).landed)
        }
    }

    impl ResumeTail for Swapped {
        fn landed_since(&self, _text: &str, _since_ms: i64) -> Option<bool> {
            Some(lock(&self.s).landed)
        }
        fn taken_up(&self, _text: &str, _since_ms: i64) -> Option<bool> {
            match &self.taken_from_call {
                Some((from, calls)) => Some(calls.fetch_add(1, Ordering::SeqCst) + 1 >= *from),
                None => Some(lock(&self.s).taken),
            }
        }
        fn output(&self, _text: &str, _since_ms: i64) -> Option<String> {
            None
        }
        fn rebind(&mut self, _repl_pid: u32) {}
        fn session_id(&self) -> Option<String> {
            None
        }
    }

    // A subscriber that keeps each event as "LEVEL message", for the tests that a failure is said out loud.

    // --- the resume machine (test_cch_pending.py) ---

    #[tokio::test(start_paused = true)]
    async fn test_the_promise_is_cleared_once_the_resume_is_delivered() {
        let h = harness();
        h.promises.record(&promise()).await;
        assert!(!h.run(promise(), unread()).await);
        assert_eq!(h.seen(), ["resume"]);
        assert_eq!(h.load().await, [], "a delivered resume must not be replayed");
    }

    #[tokio::test(start_paused = true)]
    async fn test_an_undeliverable_resume_is_listed_lost_and_shouts() {
        // A resume that cannot be delivered after every round must not vanish in a log line.
        let h = harness();
        h.script().lane_gone = true;
        h.promises.record(&promise()).await;
        assert!(!h.run(promise(), unread()).await);
        assert_eq!(h.load().await, []);
        let lost: Vec<Value> = h.lost_events().iter().map(|e| e["followup"].clone()).collect();
        assert_eq!(lost, [json!("resume")]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_started_command_is_resumed_without_waiting_for_a_free_session() {
        // A session running its `/compact` is busy, and that is exactly when the resume goes in:
        // everything typed during a compaction is drained into the turn that follows it.
        let h = harness();
        h.outcomes(vec![Outcome::Started], None);
        h.script().status = Some(("busy".into(), 9));
        h.promises.record(&promise()).await;
        assert!(!h.run(promise(), told(Some(false), Some(true))).await);
        assert_eq!(h.seen(), ["resume"]);
        assert_eq!(h.load().await, []);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_session_holding_up_its_own_command_is_asked_to_pause() {
        // The pause request goes in as plain text, which the queue hands to the running turn.
        let h = harness();
        h.outcomes(vec![Outcome::TimedOut, Outcome::Started], None);
        h.script().status = Some(("busy".into(), 9));
        h.promises.record(&promise()).await;
        assert!(!h.run(promise(), told(Some(false), Some(true))).await);
        let [nudge, resumed] = h.seen().try_into().unwrap();
        assert_eq!(resumed, "resume");
        assert!(nudge.starts_with(&from_cch("[awm reflection]")));
        assert!(nudge.contains("/compact") && nudge.contains("end your turn"));
        assert!(nudge.contains("not a new instruction"));
        assert!(!nudge.contains('\n'), "a newline in a paste submits it early");
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_resume_that_turns_up_after_its_verify_gave_up_is_not_sent_again() {
        // A resume pasted behind `/compact` showed in the transcript only when the compaction ended,
        // after the verify had called it lost, and each later round typed another copy in.
        let h = harness();
        h.outcomes(vec![], Some(Outcome::Started));
        h.promises.record(&promise()).await;
        let tail = Told { landed_since_typed: Some(h.s.clone()), ..told(Some(false), Some(false)) };
        assert!(!h.run(promise(), tail).await);
        assert_eq!(h.seen(), ["resume"]);
        assert_eq!(h.load().await, []);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_vanished_session_clears_its_promise_without_typing() {
        let h = harness();
        h.outcomes(vec![], Some(Outcome::Vanished));
        h.promises.record(&promise()).await;
        assert!(!h.run(promise(), unread()).await);
        assert_eq!(h.seen(), Vec::<String>::new(), "there is nobody left to resume");
        assert_eq!(h.load().await, []);
    }

    // --- replay on boot ---

    #[derive(Default)]
    struct Armed(Vec<Pending>);

    impl<T: Send> ResumeWatch<T> for Armed {
        async fn arm(&mut self, promise: Pending, _tail: T) {
            self.0.push(promise);
        }
    }

    async fn replay(h: &Harness, watch: &mut Armed) -> Value {
        replay_pending(&h.promises, &h.liveness(), watcher::now_ms(), |_| unread(), watch).await
    }

    #[tokio::test(start_paused = true)]
    async fn test_replay_re_arms_the_watcher_without_re_running_the_command() {
        // Replay picks the wait back up, and must not paste `/compact` a second time.
        let h = harness();
        h.promises.record(&promise()).await;
        let mut armed = Armed::default();
        assert_eq!(replay(&h, &mut armed).await, json!({"pending": 1, "resumed": 1}));
        assert_eq!(armed.0.iter().map(|i| i.text.as_str()).collect::<Vec<_>>(), ["/compact"]);
        assert_eq!(armed.0.iter().map(|i| i.followup.as_str()).collect::<Vec<_>>(), ["resume"]);
        assert_eq!(h.seen(), Vec::<String>::new());
        assert_eq!(h.load().await.len(), 1, "still owed until the re-armed watcher delivers it");
    }

    #[tokio::test(start_paused = true)]
    async fn test_replay_refuses_a_recycled_pid() {
        // The record would describe a process that is gone, and the resume land in a stranger's prompt.
        let h = harness();
        h.script().alive = HashMap::from([(OLD, "different".to_string())]);
        h.promises.record(&promise()).await;
        let mut armed = Armed::default();
        assert_eq!(replay(&h, &mut armed).await, json!({"pending": 0, "resumed": 0}));
        assert!(armed.0.is_empty(), "must not re-arm");
        assert_eq!(h.load().await, []);
    }

    // --- a resume that says what happened ---

    fn effort() -> Change {
        Change::new("effort", "low", "k-effort")
    }

    fn rename() -> Change {
        Change::new("rename", "scout", "k-rename")
    }

    async fn resume_settled(h: &Harness, item: Pending) {
        h.outcomes(vec![], Some(Outcome::Settled));
        assert!(!h.run(item, told(Some(false), None)).await);
    }

    async fn the_one_promise(h: &Harness) -> Pending {
        let mut items = h.load().await;
        assert_eq!(items.len(), 1);
        items.remove(0)
    }

    #[tokio::test(start_paused = true)]
    async fn test_the_resume_is_worded_from_the_outcome_known_at_delivery() {
        let h = harness();
        h.send("/effort low", Some(effort()), None).await;
        let item = the_one_promise(&h).await;
        h.promises.settle(OLD, "k-effort", FAILED, "the cursor was on No").await;
        resume_settled(&h, item).await;
        assert_eq!(h.seen()[1..], [from_cch(&format!("The effort change to low did not apply (the cursor was on No). {CONTINUE}"))]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_move_is_followed_by_an_mcp_reconnect_before_its_resume() {
        let h = harness();
        h.send("/cd /x", Some(Change::new("cd", "/x", "k-cd")), None).await;
        let item = the_one_promise(&h).await;
        resume_settled(&h, item).await;
        assert_eq!(h.seen()[1..], [RECONNECT_AFTER_MOVE.to_string(), from_cch(&format!("The move to /x succeeded. {CONTINUE}"))]);
    }

    // --- one resume per session ---

    fn merged() -> String {
        from_cch(&format!("The rename to scout and the effort change to low succeeded. {CONTINUE}"))
    }

    async fn two_self_commands(h: &Harness) -> Pending {
        h.send("/rename scout", Some(rename()), None).await;
        h.send("/effort low", Some(effort()), None).await;
        h.promises.read(OLD).await.unwrap()
    }

    /// The first command's promise, as its own watcher still holds it.
    fn older_than(item: &Pending) -> Pending {
        Pending { injected_at_ms: item.injected_at_ms - 5, text: "/rename scout".into(), ..item.clone() }
    }

    #[tokio::test(start_paused = true)]
    async fn test_two_self_commands_send_exactly_one_resume() {
        // Both watchers once delivered, and after the back-off both delivered again: four resumes
        // for two commands.
        let h = harness();
        let newer = two_self_commands(&h).await;
        let older = Pending { changes: vec![rename()], ..older_than(&newer) };
        h.outcomes(vec![], Some(Outcome::Settled));
        assert!(!h.run(older, told(Some(false), None)).await);
        assert_eq!(h.seen()[2..], [] as [String; 0], "the older watcher stands down");
        assert!(h.promises.read(OLD).await.is_some(), "and leaves the newer promise on disk");
        assert!(!h.run(newer, told(Some(false), None)).await);
        assert_eq!(h.seen()[2..], [merged()]);
    }

    // --- a session whose transcript moves under the wait ---

    #[tokio::test(start_paused = true)]
    async fn test_a_promise_follows_its_session_through_a_clear() {
        // A worker swap or a restart finds the session by id, and /clear changes it.
        let h = harness();
        let item = Pending::new(999_999_992, "", "old-id", "/clear", "Go on.", 1000);
        h.promises.record(&item).await;
        let cleared = Told { session_id: Some("new-id".into()), ..unread() };
        let moved = h.machine.follow_session_id(item, &cleared, "me").await;
        assert_eq!(moved.session_id, "new-id");
        assert_eq!(h.promises.read(999_999_992).await.unwrap().session_id, "new-id");
        assert_eq!(h.machine.follow_session_id(moved.clone(), &cleared, "me").await, moved);
    }

    // --- worker swaps (test_cch_swap.py) ---

    async fn run_swapped(h: &Harness, item: Pending, outcomes: Vec<Outcome>) {
        h.promises.record(&item).await;
        h.outcomes(outcomes, Some(Outcome::Started));
        let tail = Swapped { s: h.s.clone(), taken_from_call: None };
        assert!(!h.run(item, tail).await);
    }

    #[tokio::test(start_paused = true)]
    async fn test_the_promise_stays_on_disk_until_the_resume_is_taken_up() {
        let h = harness();
        {
            let mut s = h.script();
            s.on_write = Some(Box::new(|s, _text| s.landed = true));
            s.snapshot = true;
            s.compacted = true;
        }
        let calls = Arc::new(AtomicU32::new(0));
        let tail = Swapped { s: h.s.clone(), taken_from_call: Some((4, calls.clone())) };
        h.promises.record(&promise()).await;
        h.outcomes(vec![], Some(Outcome::Started));
        assert!(!h.run(promise(), tail).await);
        assert_eq!(h.script().phases_on_disk, [[RESUME_SENT]]);
        assert_eq!(calls.load(Ordering::SeqCst), 4);
        assert_eq!(h.load().await, []);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_replay_of_a_sent_resume_does_not_type_it_again() {
        let h = harness();
        {
            let mut s = h.script();
            s.taken = true;
            s.compacted = true;
        }
        run_swapped(&h, Pending { phase: RESUME_SENT.into(), ..promise() }, vec![]).await;
        assert_eq!(h.seen(), Vec::<String>::new());
        assert_eq!(h.load().await, []);
    }

    #[tokio::test(start_paused = true)]
    async fn test_an_event_reflection_refuses_is_spooled_and_sent_again() {
        let h = harness();
        h.script().replies = VecDeque::from([json!({"ok": false, "error": "down"}), json!({"ok": true})]);
        let reporter = &h.machine.reporter;
        assert!(!reporter.report(&done_event(&promise(), "")).await);
        assert_eq!(reporter.resend().await, 1);
        let kinds: Vec<Value> = h.script().events.iter().map(|e| e["kind"].clone()).collect();
        assert_eq!(kinds, [json!("done"), json!("done")]);
        assert_eq!(reporter.resend().await, 0);
        assert_eq!(reporter.spool_path(), h.promises.dir().join(UNSENT_FILE));
    }

    // --- the Rust seams ---

}
