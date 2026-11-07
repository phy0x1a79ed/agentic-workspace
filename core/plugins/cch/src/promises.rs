//! The on-disk record of a resume cch still owes a session, so a promise outlives the process
//! that made it: the service is respawned on every gateway restart, and the wait can run for minutes.
//!
//! A promise is written down before its watcher starts, and [`Promises::load_all`] reads them back
//! on boot. It belongs to a session id: the file is named by the REPL pid, only where the session
//! lives now, and carries the `procStart` seen then. The daemon can swap a session's worker for a
//! new process, so a dead pid sends the promise looking for its session under a new pid before
//! anything gives up on it, and that check stays fail-closed because pids are recycled. A promise
//! is kept until the session has taken the resume up, not until it was typed: an enqueued line
//! lives only in the worker's memory.

use std::future::Future;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;
use tokio::sync::Mutex;

use crate::guards;

/// Age is worth a log line and nothing more. A long-held promise is the mechanism working: a hard
/// cutoff once binned, at boot, promises nobody had ever waited on. Liveness decides, not the clock.
pub const OLD_ENOUGH_TO_MENTION_MS: i64 = 20 * 60 * 1000;

/// A change's outcome once its dialog was answered and it took effect.
pub const APPLIED: &str = "applied";
/// A change's outcome when it did not take effect. An outcome still empty when the resume goes in
/// reads as success: a session that settled with no dialog open has nothing left to answer.
pub const FAILED: &str = "failed";

/// The phase until the resume is typed.
pub const AWAITING_COMMAND: &str = "awaiting_command";
/// The phase once the resume is typed, until the transcript shows it taken up (and, for
/// `/compact`, the `compact_boundary`).
pub const RESUME_SENT: &str = "resume_sent";

/// One settings command a session ran on itself, and how it ended.
///
/// `key` names the change across the merge of promises and the detached task that answers its
/// confirm dialog, so that task can write the outcome back onto whichever promise carries it now.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct Change {
    pub kind: String,
    pub value: String,
    pub key: String,
    pub outcome: String,
    pub detail: String,
    /// A line the follow-up carries once the change applied, such as a move's scope brief.
    pub note: String,
}

impl Change {
    /// A change of `kind` to `value`, named `key`, with no outcome yet.
    pub fn new(kind: &str, value: &str, key: &str) -> Self {
        Self {
            kind: kind.into(),
            value: value.into(),
            key: key.into(),
            ..Self::default()
        }
    }

    /// The same change with `outcome` and `detail`.
    pub fn ended(mut self, outcome: &str, detail: &str) -> Self {
        self.outcome = outcome.into();
        self.detail = detail.into();
        self
    }

    fn from_value(c: &Value) -> Result<Self, String> {
        let c = c.as_object().ok_or("a change is not an object")?;
        let opt = |k: &str| or_default(c.get(k)).map(py_str).unwrap_or_default();
        Ok(Self {
            kind: py_str(c.get("kind").ok_or("a change has no kind")?),
            value: opt("value"),
            key: opt("key"),
            outcome: opt("outcome"),
            detail: opt("detail"),
            note: opt("note"),
        })
    }
}

/// A follow-up that has been promised but not yet delivered. Its JSON keys are the Python
/// dataclass's, so records written by either read in the other.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Pending {
    pub repl_pid: u32,
    pub proc_start: String,
    pub session_id: String,
    pub text: String,
    pub followup: String,
    pub injected_at_ms: i64,
    pub name: Option<String>,
    pub hosting: String,
    /// Rounds of waiting that ended without the resume landing, and why the last did, so the
    /// `pending` verb answers "what is it waiting for", not only "how long".
    pub holds: u32,
    pub last_outcome: String,
    /// Pause requests made of the session. Counted here, not in the watcher, so the cap survives a
    /// restart: a session asked three times is not asked three more by the next process.
    pub nudges: u32,
    /// The settings commands this promise reports on. The delivered text is rendered from these at
    /// delivery, because an outcome is known only once its dialog is answered; `followup` is the
    /// same text rendered without outcomes.
    pub changes: Vec<Change>,
    /// The caller's own closing prompt, empty for the default.
    pub closing: String,
    pub phase: String,
    /// How many times the command or the resume was typed again after a worker swap.
    pub reissues: u32,
    /// Names this promise in the events cch sends reflection.
    pub ticket: String,
}

impl Pending {
    /// A fresh promise, waiting for its command, with nothing held, nudged or changed.
    pub fn new(
        repl_pid: u32,
        proc_start: &str,
        session_id: &str,
        text: &str,
        followup: &str,
        injected_at_ms: i64,
    ) -> Self {
        Self {
            repl_pid,
            proc_start: proc_start.into(),
            session_id: session_id.into(),
            text: text.into(),
            followup: followup.into(),
            injected_at_ms,
            name: None,
            hosting: String::new(),
            holds: 0,
            last_outcome: String::new(),
            nudges: 0,
            changes: Vec::new(),
            closing: String::new(),
            phase: AWAITING_COMMAND.into(),
            reissues: 0,
            ticket: String::new(),
        }
    }

    /// Parse a record as Python's `_parse` does: unknown keys ignored, a falsy optional field read
    /// as its default, numbers and strings coerced the way `int()` and `str()` coerce them.
    pub fn from_value(data: &Value) -> Result<Self, String> {
        let d = data.as_object().ok_or("the record is not an object")?;
        let need = |k: &str| d.get(k).ok_or_else(|| format!("missing {k}"));
        let opt_str = |k: &str| or_default(d.get(k)).map(py_str).unwrap_or_default();
        let opt_int =
            |k: &str| -> Result<i64, String> { or_default(d.get(k)).map_or(Ok(0), py_int) };
        let count = |k: &str| -> Result<u32, String> {
            u32::try_from(opt_int(k)?).map_err(|e| format!("{k}: {e}"))
        };
        let changes = match or_default(d.get("changes")) {
            None => Vec::new(),
            Some(Value::Array(items)) => items
                .iter()
                .map(Change::from_value)
                .collect::<Result<_, _>>()?,
            Some(_) => return Err("changes is not a list".into()),
        };
        Ok(Self {
            repl_pid: u32::try_from(py_int(need("repl_pid")?)?)
                .map_err(|e| format!("repl_pid: {e}"))?,
            proc_start: py_str(need("proc_start")?),
            session_id: opt_str("session_id"),
            text: py_str(need("text")?),
            followup: py_str(need("followup")?),
            injected_at_ms: py_int(need("injected_at_ms")?)?,
            name: match d.get("name") {
                None | Some(Value::Null) => None,
                Some(v) => Some(py_str(v)),
            },
            hosting: opt_str("hosting"),
            holds: count("holds")?,
            last_outcome: opt_str("last_outcome"),
            nudges: count("nudges")?,
            changes,
            closing: opt_str("closing"),
            phase: or_default(d.get("phase")).map_or_else(|| AWAITING_COMMAND.into(), py_str),
            reissues: count("reissues")?,
            ticket: opt_str("ticket"),
        })
    }
}

impl<'de> Deserialize<'de> for Pending {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        Self::from_value(&Value::deserialize(d)?).map_err(serde::de::Error::custom)
    }
}

impl<'de> Deserialize<'de> for Change {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        Self::from_value(&Value::deserialize(d)?).map_err(serde::de::Error::custom)
    }
}

// Python's `x or default`: a falsy value reads as missing.
fn or_default(v: Option<&Value>) -> Option<&Value> {
    v.filter(|v| match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    })
}

fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => other.to_string(),
    }
}

fn py_int(v: &Value) -> Result<i64, String> {
    match v {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f.trunc() as i64))
            .ok_or_else(|| format!("{n} is not an int")),
        Value::Bool(b) => Ok(i64::from(*b)),
        Value::String(s) => s.trim().parse().map_err(|_| format!("{s:?} is not an int")),
        other => Err(format!("{other} is not an int")),
    }
}

fn phrase(kind: &str, value: &str) -> String {
    match kind {
        "effort" => format!("effort change to {value}"),
        "model" => format!("model change to {value}"),
        "rename" => format!("rename to {value}"),
        "cd" => format!("move to {value}"),
        other => format!("{other} change to {value}"),
    }
}

/// The resume prompt for `changes`: what each did, then the closing prompt (or the default).
/// A change with no outcome yet reads as succeeded.
pub fn render_followup(changes: &[Change], link: Option<&str>, closing: &str) -> String {
    let closing = closing.trim();
    let tail = if closing.is_empty() {
        guards::DEFAULT_FOLLOWUP
    } else {
        closing
    };
    let (mut done, mut failed, mut notes, mut mcp, mut rc) =
        (Vec::new(), Vec::new(), Vec::new(), Vec::new(), false);
    for c in changes {
        match c.kind.as_str() {
            "rc_on" => rc = true,
            "mcp" if c.detail.is_empty() => mcp.push(format!(
                "`/mcp {}` ran. Claude Code did not record what it printed, so check that server's tools.",
                c.value
            )),
            "mcp" => mcp.push(format!("`/mcp {}` printed: {}.", c.value, c.detail)),
            _ if c.outcome == FAILED => {
                let why = if c.detail.is_empty() { "no reason given" } else { &c.detail };
                failed.push(format!("The {} did not apply ({why}).", phrase(&c.kind, &c.value)));
            }
            _ => {
                done.push(phrase(&c.kind, &c.value));
                if !c.note.is_empty() {
                    notes.push(c.note.clone());
                }
            }
        }
    }
    let mut parts = Vec::new();
    if !done.is_empty() {
        parts.push(format!("The {} succeeded.", done.join(" and the ")));
    }
    if rc {
        parts.push(match link.filter(|l| !l.is_empty()) {
            Some(link) => format!("Remote Control is on: {link}."),
            None => "Remote Control was turned on; run reflection link to read its link.".into(),
        });
    }
    parts.extend(failed);
    parts.extend(mcp);
    parts.extend(notes);
    parts.push(tail.into());
    parts.join(" ")
}

/// The same promise, one round older, with `outcome` as its reason.
///
/// `injected_at_ms` deliberately does not move: it is the watcher's reference for "the session
/// reacted after this moment", and refreshing it would make the next wait sit out its cap waiting
/// for a reaction that already happened.
pub fn held(pending: &Pending, outcome: &str) -> Pending {
    Pending {
        holds: pending.holds + 1,
        last_outcome: outcome.into(),
        ..pending.clone()
    }
}

/// The same promise, with one more pause request against it.
pub fn nudged(pending: &Pending) -> Pending {
    Pending {
        nudges: pending.nudges + 1,
        ..pending.clone()
    }
}

/// What [`Promises::load_all`] asks of the world.
pub trait Liveness: Send + Sync {
    /// The process's start time, or None when no process holds `pid`.
    fn proc_start(&self, pid: u32) -> impl Future<Output = Option<String>> + Send;
    /// `(pid, procStart)` of the live process other than `old_pid` that holds `session_id` now.
    fn successor(
        &self,
        session_id: &str,
        old_pid: u32,
    ) -> impl Future<Output = Option<(u32, String)>> + Send;
    /// Announce that `item` is lost and why: a lost promise is never dropped in silence.
    fn lost(&self, item: &Pending, reason: &str) -> impl Future<Output = ()> + Send;
}

// One lock for every store, as Python's module lock: read-merge-write must not interleave.
static LOCK: Mutex<()> = Mutex::const_new(());

/// The promise store: one `<pid>.json` per promise and one `<pid>.done` stamp per delivery, in `dir`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Promises {
    dir: PathBuf,
}

impl Promises {
    /// The store in `dir`, created on first write.
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }

    /// Where the records live.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    fn path(&self, repl_pid: u32) -> PathBuf {
        self.dir.join(format!("{repl_pid}.json"))
    }

    fn done_path(&self, repl_pid: u32) -> PathBuf {
        self.dir.join(format!("{repl_pid}.done"))
    }

    /// Write `pending` down before its watcher starts.
    ///
    /// An outcome already on disk for a change survives a rewrite from a watcher that has not seen
    /// it. Best-effort by design: a store that cannot be written must not stop the command the
    /// caller asked for, only cost it surviving a restart.
    pub async fn record(&self, pending: &Pending) {
        let _held = LOCK.lock().await;
        let mut pending = pending.clone();
        if let Some(on_disk) = self.read(pending.repl_pid).await {
            for c in &mut pending.changes {
                if !c.outcome.is_empty() {
                    continue;
                }
                if let Some(known) = on_disk
                    .changes
                    .iter()
                    .find(|k| k.key == c.key && !k.outcome.is_empty())
                {
                    *c = known.clone();
                }
            }
        }
        self.write(&pending).await;
    }

    async fn write(&self, pending: &Pending) {
        let written = async {
            tokio::fs::create_dir_all(&self.dir).await?;
            let tmp = self.dir.join(format!(".{}.tmp", pending.repl_pid));
            tokio::fs::write(&tmp, serde_json::to_vec(pending)?).await?;
            tokio::fs::rename(&tmp, self.path(pending.repl_pid)).await
        }
        .await;
        if let Err(e) = written {
            tracing::warn!(
                "cch: could not record the pending resume for pid {} ({e}); it will not survive a service restart",
                pending.repl_pid
            );
        }
    }

    /// Forget the promise for `repl_pid`.
    pub async fn clear(&self, repl_pid: u32) {
        let _ = tokio::fs::remove_file(self.path(repl_pid)).await;
    }

    /// Move a promise from `old_pid` to the pid `item` now names.
    pub async fn rekey(&self, old_pid: u32, item: &Pending) {
        let _held = LOCK.lock().await;
        self.write(item).await;
        if old_pid != item.repl_pid {
            self.clear(old_pid).await;
        }
    }

    /// Note that the promise stamped `injected_at_ms` has gone in, or is going in.
    ///
    /// A watcher for an older promise that wakes after the newer one was delivered and cleared
    /// finds no file to tell it so; this stamp does.
    pub async fn mark_delivered(&self, repl_pid: u32, injected_at_ms: i64) {
        let _held = LOCK.lock().await;
        let stamp = injected_at_ms.max(self.delivered_ms(repl_pid).await);
        let written = async {
            tokio::fs::create_dir_all(&self.dir).await?;
            tokio::fs::write(self.done_path(repl_pid), stamp.to_string()).await
        }
        .await;
        if let Err(e) = written {
            tracing::warn!("cch: could not note the delivery for pid {repl_pid}: {e}");
        }
    }

    /// The newest promise stamp delivered to `repl_pid`, or 0.
    pub async fn delivered_ms(&self, repl_pid: u32) -> i64 {
        match tokio::fs::read_to_string(self.done_path(repl_pid)).await {
            Ok(s) => s.trim().parse().unwrap_or(0),
            Err(_) => 0,
        }
    }

    /// The promise on disk for `repl_pid`, or None. No liveness check.
    pub async fn read(&self, repl_pid: u32) -> Option<Pending> {
        let bytes = tokio::fs::read(self.path(repl_pid)).await.ok()?;
        serde_json::from_slice(&bytes).ok()
    }

    /// Whether the promise on disk still reports the change `key`.
    pub async fn has_change(&self, repl_pid: u32, key: &str) -> bool {
        self.read(repl_pid)
            .await
            .is_some_and(|p| p.changes.iter().any(|c| c.key == key))
    }

    /// Write how change `key` ended onto the promise that carries it. False when no promise
    /// carries it, which is the normal case once the resume has gone in.
    pub async fn settle(&self, repl_pid: u32, key: &str, outcome: &str, detail: &str) -> bool {
        let _held = LOCK.lock().await;
        let Some(mut found) = self.read(repl_pid).await else {
            return false;
        };
        if !found.changes.iter().any(|c| c.key == key) {
            return false;
        }
        for c in found.changes.iter_mut().filter(|c| c.key == key) {
            c.outcome = outcome.into();
            c.detail = detail.into();
        }
        self.write(&found).await;
        true
    }

    // `item` moved to the live process that holds its session now, or None when the session has
    // no live process left: then the promise is lost, and that is announced.
    async fn follow<L: Liveness>(&self, item: Pending, live: &L) -> Option<Pending> {
        let Some((pid, started)) = live.successor(&item.session_id, item.repl_pid).await else {
            self.clear(item.repl_pid).await;
            live.lost(
                &item,
                "the session has no live process any more, so nothing is left to resume",
            )
            .await;
            return None;
        };
        tracing::warn!(
            "cch: session {} moved from pid {} to pid {pid} (a worker swap); its promise follows it",
            who(&item),
            item.repl_pid
        );
        let moved = Pending {
            repl_pid: pid,
            proc_start: started,
            ..item.clone()
        };
        self.rekey(item.repl_pid, &moved).await;
        Some(moved)
    }

    /// Every promise still worth keeping, clearing the ones that are not.
    ///
    /// Worth keeping means the session is still there: its pid is live and started when the
    /// promise says, or its session id lives on in a new process. A promise never stays on a pid
    /// whose start time changed. Age is logged, never acted on. Unreadable records are removed
    /// here, because nothing else sweeps this directory and one left once would stay forever.
    pub async fn load_all<L: Liveness>(&self, now_ms: i64, live: &L) -> Vec<Pending> {
        let mut out = Vec::new();
        let Ok(mut dir) = tokio::fs::read_dir(&self.dir).await else {
            return out;
        };
        let (mut entries, mut stamps) = (Vec::new(), Vec::new());
        while let Ok(Some(e)) = dir.next_entry().await {
            let name = e.file_name().to_string_lossy().into_owned();
            if let Some(stem) = name.strip_suffix(".json") {
                entries.push((name.clone(), stem.to_string()));
            } else if let Some(stem) = name.strip_suffix(".done") {
                stamps.push(stem.to_string());
            }
        }
        entries.sort();
        stamps.sort();
        for stem in stamps {
            let Some(pid) = digits(&stem) else { continue };
            if live.proc_start(pid).await.is_none() {
                let _ = tokio::fs::remove_file(self.done_path(pid)).await;
            }
        }
        for (name, _) in entries {
            let path = self.dir.join(&name);
            let parsed = match tokio::fs::read(&path).await {
                Ok(bytes) => serde_json::from_slice::<Value>(&bytes)
                    .map_err(|e| e.to_string())
                    .and_then(|v| Pending::from_value(&v)),
                Err(e) => Err(e.to_string()),
            };
            let mut item = match parsed {
                Ok(item) => item,
                Err(e) => {
                    tracing::warn!("cch: discarding unreadable pending record {name}: {e}");
                    let _ = tokio::fs::remove_file(&path).await;
                    continue;
                }
            };
            let started = live.proc_start(item.repl_pid).await;
            let moved_on = match &started {
                None => true,
                Some(s) => !item.proc_start.is_empty() && item.proc_start != *s,
            };
            if moved_on {
                match self.follow(item, live).await {
                    Some(moved) => item = moved,
                    None => continue,
                }
            }
            let age_ms = now_ms - item.injected_at_ms;
            if age_ms > OLD_ENOUGH_TO_MENTION_MS {
                tracing::info!(
                    "cch: session {} has been owed a resume for {}s ({} round(s) of holding, {} pause request(s); \
                     last: {})",
                    who(&item),
                    age_ms.div_euclid(1000),
                    item.holds,
                    item.nudges,
                    if item.last_outcome.is_empty() { "still waiting" } else { &item.last_outcome }
                );
            }
            out.push(item);
        }
        out
    }
}

fn digits(stem: &str) -> Option<u32> {
    (!stem.is_empty() && stem.bytes().all(|b| b.is_ascii_digit()))
        .then(|| stem.parse().ok())
        .flatten()
}

fn who(item: &Pending) -> &str {
    item.name
        .as_deref()
        .filter(|n| !n.is_empty())
        .unwrap_or(&item.session_id)
}

#[cfg(test)]
pub(crate) mod tests {
    use std::sync::Mutex as StdMutex;

    use super::*;

    pub(crate) const SID: &str = "00000000-0000-4000-8000-000000000001";
    const CONTINUE: &str = guards::DEFAULT_FOLLOWUP;

    /// Every pid alive with start `start` (dead when None); no session has a successor.
    pub(crate) struct FakeLiveness {
        pub start: Option<String>,
        pub lost: StdMutex<Vec<(String, String)>>,
    }

    impl FakeLiveness {
        pub(crate) fn new(start: Option<&str>) -> Self {
            Self {
                start: start.map(str::to_string),
                lost: StdMutex::new(Vec::new()),
            }
        }
    }

    impl Liveness for FakeLiveness {
        async fn proc_start(&self, _pid: u32) -> Option<String> {
            self.start.clone()
        }
        async fn successor(&self, _session_id: &str, _old_pid: u32) -> Option<(u32, String)> {
            None
        }
        async fn lost(&self, item: &Pending, reason: &str) {
            self.lost
                .lock()
                .unwrap()
                .push((item.followup.clone(), reason.into()));
        }
    }

    fn promise() -> Pending {
        Pending {
            name: Some("test".into()),
            hosting: "tmux".into(),
            ..Pending::new(4242, "999", SID, "/compact", "resume", 1_000)
        }
    }

    fn store() -> (tempfile::TempDir, Promises) {
        let dir = tempfile::tempdir().unwrap();
        let store = Promises::new(dir.path().join("pending"));
        (dir, store)
    }

    #[tokio::test]
    async fn test_a_long_held_promise_survives_while_its_session_lives() {
        // A promise held for an hour is this mechanism working, not a stale record.
        let (_d, store) = store();
        store
            .record(&Pending {
                injected_at_ms: 0,
                ..promise()
            })
            .await;
        let kept = store
            .load_all(4 * 60 * 60 * 1000, &FakeLiveness::new(Some("999")))
            .await;
        assert_eq!(
            kept.iter().map(|i| i.followup.as_str()).collect::<Vec<_>>(),
            ["resume"]
        );
    }

    #[tokio::test]
    async fn test_a_promise_whose_session_is_gone_is_dropped() {
        // Liveness, not age, is what decides.
        let (_d, store) = store();
        store.record(&promise()).await;
        let live = FakeLiveness::new(None);
        assert_eq!(store.load_all(2_000, &live).await, []);
        assert_eq!(
            live.lost.lock().unwrap().len(),
            1,
            "a lost promise is announced, never dropped in silence"
        );
        assert_eq!(store.read(4242).await, None);
    }

    #[tokio::test]
    async fn test_a_promise_on_a_recycled_pid_is_dropped() {
        let (_d, store) = store();
        store.record(&promise()).await;
        assert_eq!(
            store
                .load_all(2_000, &FakeLiveness::new(Some("different")))
                .await,
            []
        );
    }

    #[test]
    fn test_holding_a_promise_records_why_without_moving_its_clock() {
        let item = Pending {
            injected_at_ms: 1234,
            ..promise()
        };
        let once = held(&item, "the session has not been free");
        assert_eq!(once.holds, 1);
        assert_eq!(once.last_outcome, "the session has not been free");
        assert_eq!(once.injected_at_ms, 1234);
        assert_eq!(held(&once, "again").holds, 2);
    }

    #[tokio::test]
    async fn test_pause_requests_are_counted_on_the_promise_so_a_restart_cannot_reset_them() {
        let (_d, store) = store();
        let item = nudged(&nudged(&promise()));
        assert_eq!(item.nudges, 2);
        store.record(&item).await;
        let reloaded = store.load_all(2_000, &FakeLiveness::new(Some("999"))).await;
        assert_eq!(reloaded.len(), 1);
        assert_eq!(reloaded[0].nudges, 2);
    }

    #[tokio::test]
    async fn test_an_unreadable_record_is_discarded_not_carried_forever() {
        let (_d, store) = store();
        tokio::fs::create_dir_all(store.dir()).await.unwrap();
        let junk = store.dir().join("4242.json");
        tokio::fs::write(&junk, "{not json").await.unwrap();
        assert_eq!(
            store.load_all(2_000, &FakeLiveness::new(Some("999"))).await,
            []
        );
        assert!(!junk.exists());
    }

    #[test]
    fn test_the_resume_names_each_change_and_how_it_ended() {
        let effort = Change::new("effort", "low", "k-effort");
        let rename = Change::new("rename", "scout", "k-rename");
        let link = "https://example.invalid/code/session_0";
        let cases: Vec<(Vec<Change>, Option<&str>, &str, String)> = vec![
            (vec![effort.clone()], None, "", format!("The effort change to low succeeded. {CONTINUE}")),
            (vec![Change::new("model", "opus", "k")], None, "", format!("The model change to opus succeeded. {CONTINUE}")),
            (vec![rename.clone()], None, "", format!("The rename to scout succeeded. {CONTINUE}")),
            (vec![Change::new("rc_on", "", "k")], Some(link), "", format!("Remote Control is on: {link}. {CONTINUE}")),
            (
                vec![Change::new("rc_on", "", "k")],
                None,
                "",
                format!("Remote Control was turned on; run reflection link to read its link. {CONTINUE}"),
            ),
            (
                vec![Change::new("effort", "low", "k").ended(FAILED, "the cursor was on No")],
                None,
                "",
                format!("The effort change to low did not apply (the cursor was on No). {CONTINUE}"),
            ),
            (
                vec![rename.clone(), effort.clone()],
                None,
                "",
                format!("The rename to scout and the effort change to low succeeded. {CONTINUE}"),
            ),
            (
                vec![rename.clone(), Change::new("model", "opus", "k").ended(FAILED, "a hook asked")],
                None,
                "",
                format!("The rename to scout succeeded. The model change to opus did not apply (a hook asked). {CONTINUE}"),
            ),
            (vec![effort.clone()], None, "go on with T3", "The effort change to low succeeded. go on with T3".into()),
            (vec![], None, "", CONTINUE.into()),
            (vec![], None, "go on with T3", "go on with T3".into()),
        ];
        for (changes, link, closing, expected) in cases {
            assert_eq!(render_followup(&changes, link, closing), expected);
        }
    }

    #[tokio::test]
    async fn test_settling_a_change_nobody_carries_is_a_no_op() {
        let (_d, store) = store();
        assert!(!store.settle(4242, "nope", APPLIED, "").await);
    }

    #[test]
    fn test_an_mcp_change_reports_what_the_command_printed() {
        let said =
            Change::new("mcp", "reconnect awm", "k1").ended(APPLIED, "Reconnected to the server");
        let silent = Change::new("mcp", "disable all", "k2");
        let text = render_followup(&[said, silent], None, "");
        assert!(text.starts_with(
            "`/mcp reconnect awm` printed: Reconnected to the server. `/mcp disable all` ran. Claude Code did not \
             record what it printed"
        ));
        assert!(text.ends_with(guards::DEFAULT_FOLLOWUP));
    }

    #[tokio::test]
    async fn a_record_round_trips_with_the_python_keys_and_tolerates_unknown_ones() {
        let (_d, store) = store();
        let item = Pending {
            changes: vec![Change::new("effort", "low", "k")],
            ticket: "t".into(),
            ..promise()
        };
        store.record(&item).await;
        let raw: Value = serde_json::from_slice(
            &tokio::fs::read(store.dir().join("4242.json"))
                .await
                .unwrap(),
        )
        .unwrap();
        let keys: Vec<&str> = raw
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        assert_eq!(
            keys,
            [
                "repl_pid",
                "proc_start",
                "session_id",
                "text",
                "followup",
                "injected_at_ms",
                "name",
                "hosting",
                "holds",
                "last_outcome",
                "nudges",
                "changes",
                "closing",
                "phase",
                "reissues",
                "ticket"
            ]
        );
        assert_eq!(store.read(4242).await, Some(item));
        let sparse = serde_json::json!({
            "repl_pid": "7", "proc_start": 12, "text": "/compact", "followup": "go", "injected_at_ms": 5,
            "holds": null, "phase": "", "from_the_future": true,
        });
        let parsed = Pending::from_value(&sparse).unwrap();
        assert_eq!(
            (
                parsed.repl_pid,
                parsed.proc_start.as_str(),
                parsed.phase.as_str()
            ),
            (7, "12", AWAITING_COMMAND)
        );
        assert!(
            !store.dir().join(".4242.tmp").exists(),
            "written through a temp file, then renamed"
        );
    }

    #[tokio::test]
    async fn delivery_stamps_only_move_forward_and_dead_ones_are_swept() {
        let (_d, store) = store();
        assert_eq!(store.delivered_ms(4242).await, 0);
        store.mark_delivered(4242, 50).await;
        store.mark_delivered(4242, 20).await;
        assert_eq!(store.delivered_ms(4242).await, 50);
        store.load_all(0, &FakeLiveness::new(None)).await;
        assert_eq!(store.delivered_ms(4242).await, 0);
    }

    #[tokio::test]
    async fn an_outcome_on_disk_survives_a_rewrite_and_rekey_moves_the_file() {
        let (_d, store) = store();
        let item = Pending {
            changes: vec![Change::new("effort", "low", "k")],
            ..promise()
        };
        store.record(&item).await;
        assert!(store.has_change(4242, "k").await);
        assert!(store.settle(4242, "k", FAILED, "no").await);
        store.record(&held(&item, "waiting")).await;
        let now = store.read(4242).await.unwrap();
        assert_eq!((now.changes[0].outcome.as_str(), now.holds), (FAILED, 1));
        store
            .rekey(
                4242,
                &Pending {
                    repl_pid: 4343,
                    ..now
                },
            )
            .await;
        assert_eq!(store.read(4242).await, None);
        assert_eq!(store.read(4343).await.unwrap().changes[0].detail, "no");
    }
}
