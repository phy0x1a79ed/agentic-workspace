//! The control verbs: model, effort, rename, cd, mcp, message, interrupt, link, stop and Remote
//! Control, on the caller's own session or on any other session on this node named by job or name.
//!
//! Before it types, a verb refuses a target whose record says it is waiting on an answer or whose
//! screen shows a modal, since a keystroke into a modal answers it. `/effort` and `/model` are the
//! exception that answers one on purpose: the "Change effort level?" / "Switch model?" dialog.

use std::collections::HashSet;
use std::fmt::Write as _;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

use awm_harness::claude::cli::{self, StopOutcome};
use awm_harness::claude::records;
use awm_harness::claude::roster::{self, JobId};
use awm_harness::claude::{trust, ClaudeHome};
use awm_plugin_sdk::procfs;
use regex::Regex;
use serde_json::{json, Value};
use tokio::sync::{Mutex, OwnedMutexGuard};
use tokio::time::{sleep, Instant};

use crate::guards::{self, EmptyText};
use crate::lane::{AnyLane, Host, Key, Lane, LaneError, Opener};
use crate::lineage;
use crate::paths::Paths;
use crate::permission_mode::{walk_lock, WALK_WAIT};
use crate::promises::{Change, Promises, APPLIED, FAILED};
use crate::sender::{self, DeliverOpts, Delivery, DeliveryError, ResumeWatch, SendError, SendRequest, Sender};
use crate::settings_guard::{self, Snapshot, TokioPace};
use crate::target::{self, Resolve, ResolveError};
use crate::transcript::Tail;
use crate::watcher::{self, LiveProbe};

/// High is the ceiling for any session, whatever the model.
pub const EFFORTS: [&str; 3] = ["low", "medium", "high"];

/// How long `remote_control(on)` waits for the bridge id to reach the record.
pub const LINK_WAIT: Duration = Duration::from_secs(15);
/// How long `remote_control(off)` waits for its modal, and `/effort`/`/model` for their dialog.
pub const MODAL_WAIT: Duration = Duration::from_secs(8);
/// How long `remote_control(off)` waits for the bridge id to clear.
pub const CLEAR_WAIT: Duration = Duration::from_secs(10);
pub const POLL: Duration = Duration::from_millis(500);
/// A session acting on itself has to wait for its own turn to end before the modal exists.
pub const SELF_MODAL_WAIT: Duration = Duration::from_secs(600);
pub const SELF_POLL: Duration = Duration::from_secs(2);
/// Longest a modal's list is walked before giving up. The Remote Control modal has a handful.
pub const MAX_MODAL_STEPS: usize = 6;
/// Beat between a keypress and reading the screen back.
pub const KEY_SETTLE: Duration = Duration::from_millis(350);
/// Lets `stop` of the caller's own session return its answer first.
pub const SELF_STOP_DELAY: Duration = Duration::from_millis(1500);
/// How long `cd` waits for an idle session's record to show the new directory.
pub const MOVE_WAIT: Duration = Duration::from_secs(8);
/// How long `mcp` waits for another idle session to print the command's result.
pub const MCP_OUTPUT_WAIT: Duration = Duration::from_secs(30);
/// How long a dialog offering a different switch is left for whoever asked for that one, before
/// it is closed so the session is not left waiting.
pub const WRONG_OFFER_GRACE: Duration = Duration::from_secs(6);

/// The Remote Control modal's option that `remote_control(off)` selects, lowercased.
pub const DISCONNECT_OPTION: &str = "disconnect this session";
/// What the caller's own session is told once its Remote Control is off.
pub const SELF_OFF_FOLLOWUP: &str = "Remote Control is now off. Continue with what you were doing.";
pub use crate::resume::RECONNECT_AFTER_MOVE;

const HOOK_ASKED: &str = "a premodelswitch hook asked you to confirm";
const NO_CALLER: &str = "could not tell which session is calling, so there is no session of your own to act on. \
                         Name another with `job`, or call through a session's own `awm mcp` proxy";
const BUSY: &str = "another screen walk on this session is still running. Nothing was pressed.";

static CURSOR: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[❯›▶]\s*([^\n]*)").unwrap());
static YES_SWITCH: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^(?:\d+\s*[.)]\s*)?yes,?\s+switch to\s+(.+)$").unwrap());
static NO_OPTION: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^(?:\d+\s*[.)]\s*)?no\b").unwrap());
static WORDS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());
static BRACKETS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\[.*?\]").unwrap());

/// How a session is hosted: a background job behind the daemon, or a terminal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionKind {
    Background,
    Interactive,
}

impl SessionKind {
    /// The record's word: `bg` or `interactive`.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Background => "bg",
            Self::Interactive => "interactive",
        }
    }
}

/// One session on this node that a verb may act on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Target {
    pub pid: u32,
    pub kind: SessionKind,
    pub job: Option<String>,
    pub name: Option<String>,
    pub alive: bool,
    pub is_self: bool,
    pub session_id: Option<String>,
}

impl Target {
    /// The name, else the job id, else the pid.
    pub fn label(&self) -> String {
        [self.name.as_deref(), self.job.as_deref()]
            .into_iter()
            .flatten()
            .find(|s| !s.is_empty())
            .map_or_else(|| self.pid.to_string(), str::to_string)
    }
}

/// No session on this node answers to `unknown_ref`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("no session on this node has the job id or name {}; `list_other` shows them", py_repr(.unknown_ref))]
pub struct UnknownSession {
    pub unknown_ref: String,
}

/// Why a verb could not run. A refusal is not an error: it comes back as an `ok: false` result.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ControlError {
    #[error(transparent)]
    Resolve(#[from] ResolveError),
    #[error(transparent)]
    Unknown(#[from] UnknownSession),
    #[error(transparent)]
    Lane(#[from] LaneError),
    #[error(transparent)]
    Delivery(#[from] DeliveryError),
    #[error(transparent)]
    Empty(#[from] EmptyText),
    /// Another screen walk held the session past the walk lock's wait.
    #[error("{0}")]
    Busy(String),
}

impl From<SendError> for ControlError {
    fn from(e: SendError) -> Self {
        match e {
            SendError::Unidentified(e) => e.into(),
            SendError::Empty(e) => e.into(),
            SendError::Delivery(e) => e.into(),
        }
    }
}

/// Detached work a verb leaves behind: it outlives the call and dies with the process.
pub type Detached = Pin<Box<dyn Future<Output = ()> + Send + 'static>>;

/// A target's screen, open to read and press keys, with the walk lock of its session.
pub struct Screen<L> {
    lane: L,
    lock: Arc<Mutex<()>>,
}

impl<L: Lane> Screen<L> {
    /// `lock` is the session's [`walk_lock`]: every press here takes it, so screen walks on one
    /// session never overlap.
    pub fn new(lane: L, lock: Arc<Mutex<()>>) -> Self {
        Self { lane, lock }
    }

    async fn read(&mut self) -> Result<String, ControlError> {
        Ok(self.lane.read().await?)
    }

    async fn press(&mut self, key: Key) -> Result<(), ControlError> {
        Ok(self.lane.press(key).await?)
    }

    fn forget(&mut self) {
        self.lane.forget();
    }

    fn hold(&self) -> impl Future<Output = Result<OwnedMutexGuard<()>, ControlError>> + Send + 'static {
        let lock = self.lock.clone();
        async move {
            tokio::time::timeout(WALK_WAIT, lock.lock_owned())
                .await
                .map_err(|_| ControlError::Busy(BUSY.into()))
        }
    }

    async fn close(self) {
        self.lane.close().await;
    }
}

/// Everything the verbs do to the world beyond reading Claude Code's records under `home`.
/// [`Live`] is the real one; the tests fake it.
pub trait Effects: Clone + Send + Sync + 'static {
    type Lane: Lane + 'static;

    fn home(&self) -> &ClaudeHome;
    /// The promise store the caller's own `/model`/`/effort` outcome is written onto.
    fn promises(&self) -> &Promises;
    /// The workspace's projects directory: a worktree under it is one awm created.
    fn projects_root(&self) -> PathBuf;
    /// Open `t`'s screen to read it and press keys.
    fn open_screen(&self, t: &Target) -> impl Future<Output = Result<Screen<Self::Lane>, ControlError>> + Send;
    /// Type `text` into another session and submit it.
    fn deliver(
        &self,
        t: &Target,
        text: &str,
        what: &str,
        attempts: u32,
    ) -> impl Future<Output = Result<Delivery, DeliveryError>> + Send;
    /// The session record's status, or None when it cannot be read.
    fn status(&self, t: &Target) -> impl Future<Output = Option<String>> + Send;
    /// Whether `t` is between turns, so a typed command runs now rather than when the turn ends.
    fn between_turns(&self, t: &Target) -> impl Future<Output = bool> + Send;
    /// Send to the caller's own session, which is resumed once the command has run.
    fn send_self(&self, req: SendRequest) -> impl Future<Output = Result<Value, SendError>> + Send;
    /// What `line` printed in `t`'s transcript after `since_ms`, waiting up to [`MCP_OUTPUT_WAIT`].
    fn await_output(&self, t: &Target, line: &str, since_ms: i64) -> impl Future<Output = Option<String>> + Send;
    /// Whether `/cd dir` would move without opening Claude Code's trust dialog.
    fn trusted(&self, dir: &Path) -> impl Future<Output = bool> + Send;
    /// Record trust for an awm worktree under `projects_root`; the entries added.
    fn grant_awm_worktree(&self, dir: &Path, projects_root: &Path) -> impl Future<Output = Vec<String>> + Send;
    /// Run `job` detached.
    fn spawn(&self, job: Detached);
}

/// Ends a background job for `stop`. [`RemoveJob`] stops and removes it, [`KeepJob`] only stops it.
pub trait Remover: Send + Sync + 'static {
    fn remove(&self, job: &JobId) -> impl Future<Output = StopOutcome> + Send;
}

/// `claude stop` then `claude rm`.
#[derive(Debug, Clone)]
pub struct RemoveJob {
    pub home: ClaudeHome,
    pub claude: PathBuf,
}

impl Remover for RemoveJob {
    async fn remove(&self, job: &JobId) -> StopOutcome {
        let probe = cli::JobProbe { home: self.home.clone() };
        cli::stop_and_remove(job, &self.claude, &cli::ProcessRunner, &probe).await
    }
}

/// `claude stop` alone, so the job resumes by id.
#[derive(Debug, Clone)]
pub struct KeepJob {
    pub claude: PathBuf,
}

impl Remover for KeepJob {
    async fn remove(&self, job: &JobId) -> StopOutcome {
        cli::stop_keeping(job, &self.claude, &cli::ProcessRunner).await
    }
}

// --- small helpers -----------------------------------------------------------

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    match tokio::task::spawn_blocking(f).await {
        Ok(v) => v,
        Err(e) => std::panic::resume_unwind(e.into_panic()),
    }
}

/// Python's `repr` of a string, as the refusal texts quote one.
fn py_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if c.is_control() && (c as u32) < 0x100 => {
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c if c.is_control() => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

fn py_opt_repr(s: Option<&str>) -> String {
    s.map_or_else(|| "None".into(), py_repr)
}

/// Python's `:g` for these whole and half seconds.
fn secs(d: Duration) -> String {
    d.as_secs_f64().to_string()
}

// Unicode format characters (Cf), which Python's `isprintable` rejects with the controls.
fn is_format(c: char) -> bool {
    matches!(c,
        '\u{ad}' | '\u{600}'..='\u{605}' | '\u{61c}' | '\u{6dd}' | '\u{70f}' | '\u{180e}'
        | '\u{200b}'..='\u{200f}' | '\u{202a}'..='\u{202e}' | '\u{2060}'..='\u{2064}'
        | '\u{2066}'..='\u{206f}' | '\u{feff}' | '\u{fff9}'..='\u{fffb}')
}

/// Python's `str.isprintable`: no control, format or separator character but the plain space.
fn is_printable(s: &str) -> bool {
    s.chars().all(|c| c == ' ' || !(c.is_control() || c.is_whitespace() || is_format(c)))
}

fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

fn text_of(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

fn str_of(v: Option<&Value>) -> Option<String> {
    v.and_then(Value::as_str).map(str::to_string)
}

/// `{**base, **extra}`: a key already in `base` keeps its place and takes the new value.
fn with(mut base: Value, extra: Value) -> Value {
    if let (Some(b), Value::Object(e)) = (base.as_object_mut(), extra) {
        for (k, v) in e {
            b.insert(k, v);
        }
    }
    base
}

fn reason(why: impl Into<String>) -> Value {
    json!({"ok": false, "reason": why.into()})
}

fn is_true(out: &Value, key: &str) -> bool {
    truthy(out.get(key))
}

fn new_change(kind: &str, value: &str, note: &str) -> Change {
    let key = uuid::Uuid::new_v4().simple().to_string();
    Change { note: note.into(), ..Change::new(kind, value, &key[..12]) }
}

// --- resolving `job` ---------------------------------------------------------

fn alive(pid: Option<u32>, start: Option<&str>) -> bool {
    match (pid, start) {
        (Some(pid), Some(start)) if !start.is_empty() => procfs::start_ticks(pid).as_deref() == Some(start),
        _ => false,
    }
}

/// Every session on this node that `list_other` would show.
fn candidates(home: &ClaudeHome) -> Vec<Target> {
    let mut out: Vec<Target> = roster::load(home)
        .into_iter()
        .filter(|j| j.has_record)
        .filter_map(|j| {
            let pid = j.repl_pid?;
            Some(Target {
                pid,
                kind: SessionKind::Background,
                alive: alive(Some(pid), j.repl_proc_start.as_deref()),
                job: Some(j.short),
                name: Some(j.name),
                is_self: false,
                session_id: j.session_id,
            })
        })
        .collect();
    let mut files: Vec<PathBuf> = std::fs::read_dir(home.sessions())
        .map(|d| {
            d.flatten()
                .map(|e| e.path())
                .filter(|p| p.extension().is_some_and(|x| x == "json"))
                .collect()
        })
        .unwrap_or_default();
    files.sort();
    for path in files {
        let rec = records::read_json(&path);
        let pid = rec.get("pid").and_then(Value::as_u64).and_then(|n| u32::try_from(n).ok());
        let start = rec.get("procStart").filter(|v| !v.is_null()).map(text_of);
        if rec.get("kind").and_then(Value::as_str) == Some("bg") || !alive(pid, start.as_deref()) {
            continue;
        }
        let Some(pid) = pid else { continue };
        out.push(Target {
            pid,
            kind: SessionKind::Interactive,
            job: None,
            name: str_of(rec.get("name")),
            alive: true,
            is_self: false,
            session_id: str_of(rec.get("sessionId")),
        });
    }
    out
}

fn describe(t: &Target) -> String {
    let at = match &t.job {
        Some(job) if !job.is_empty() => format!("job {job}"),
        _ => format!("interactive pid {}", t.pid),
    };
    format!("{at} named {}{}", py_opt_repr(t.name.as_deref()), if t.alive { "" } else { " (not running)" })
}

async fn caller_target(home: &ClaudeHome, caller_pid: Option<u32>) -> Result<Target, ControlError> {
    let Some(pid) = caller_pid.filter(|p| *p != 0) else {
        return Err(ResolveError(NO_CALLER.into()).into());
    };
    let record = target::read_session_record(home, pid).await?;
    let bg = record.get("kind").and_then(Value::as_str) == Some("bg");
    let job = record.get("jobId").filter(|v| bg && truthy(Some(v))).map(text_of);
    Ok(Target {
        pid,
        kind: if bg { SessionKind::Background } else { SessionKind::Interactive },
        job,
        name: str_of(record.get("name")),
        alive: true,
        is_self: true,
        session_id: str_of(record.get("sessionId")),
    })
}

async fn mark_self(home: &ClaudeHome, mut t: Target, caller_pid: Option<u32>) -> Target {
    let Some(caller) = caller_pid.filter(|p| *p != 0) else { return t };
    let home = home.clone();
    let caller_job = blocking(move || records::job_of_pid(&home, caller)).await;
    t.is_self = t.pid == caller || (t.job.is_some() && t.job == caller_job);
    t
}

/// The session `job` names (a job id, a session id, or a name, exactly then case-blind), or the
/// caller's own when it is omitted. A name two sessions share is refused with the candidates.
pub async fn resolve<E: Effects>(fx: &E, job: Option<&str>, caller_pid: Option<u32>) -> Result<Target, ControlError> {
    let wanted = job.map(str::trim).unwrap_or_default();
    if wanted.is_empty() {
        return caller_target(fx.home(), caller_pid).await;
    }
    let home = fx.home().clone();
    let cands = blocking(move || candidates(&home)).await;
    let folded = wanted.to_lowercase();
    let tiers: [Vec<&Target>; 3] = [
        cands
            .iter()
            .filter(|t| t.job.as_deref() == Some(wanted) || t.session_id.as_deref() == Some(wanted))
            .collect(),
        cands.iter().filter(|t| t.name.as_deref() == Some(wanted)).collect(),
        cands.iter().filter(|t| t.name.as_deref().unwrap_or_default().to_lowercase() == folded).collect(),
    ];
    for matches in tiers {
        match matches.as_slice() {
            [] => {}
            [one] => return Ok(mark_self(fx.home(), (*one).clone(), caller_pid).await),
            many => {
                let listed: Vec<String> = many.iter().map(|t| describe(t)).collect();
                return Err(ResolveError(format!(
                    "{} names {} sessions: {}. Pass the job id.",
                    py_repr(wanted),
                    many.len(),
                    listed.join("; ")
                ))
                .into());
            }
        }
    }
    Err(UnknownSession { unknown_ref: wanted.into() }.into())
}

fn require_running(t: &Target) -> Result<(), ControlError> {
    if t.alive {
        return Ok(());
    }
    Err(ResolveError(format!("{} is not running, so there is nothing to type into", describe(t))).into())
}

// --- reading and reaching the target ----------------------------------------

/// The Remote Control session id now: the job record's, or the live session record's.
async fn bridge_id(home: &ClaudeHome, t: &Target) -> Option<String> {
    let (home, job, pid) = (home.clone(), t.job.clone().filter(|j| !j.is_empty()), t.pid);
    blocking(move || match job {
        Some(job) => roster::job_bridge_id(&home, &job),
        None => records::pid_bridge_id(&home, pid),
    })
    .await
}

fn refused(t: &Target, why: &str) -> Value {
    json!({"ok": false, "refused": true, "session": t.label(), "job": t.job, "reason": why})
}

fn identify(t: &Target) -> Value {
    json!({"session": t.label(), "job": t.job, "self": t.is_self})
}

fn ok_for(t: &Target) -> Value {
    with(json!({"ok": true}), identify(t))
}

async fn modal_refusal<E: Effects>(
    fx: &E,
    t: &Target,
    screen: &mut Screen<E::Lane>,
) -> Result<Option<Value>, ControlError> {
    if fx.status(t).await.as_deref() == Some("waiting") {
        let why = format!(
            "{} is waiting on an answer to a prompt, and a keystroke would answer it; nothing was sent",
            t.label()
        );
        return Ok(Some(with(refused(t, &why), json!({"kind": "modal"}))));
    }
    if let Some(marker) = guards::modal_on_screen(&screen.read().await?) {
        let why = format!(
            "{} is showing a modal or permission prompt (seen: {}), and a keystroke would answer it; nothing was sent",
            t.label(),
            py_repr(marker)
        );
        return Ok(Some(with(refused(t, &why), json!({"kind": "modal"}))));
    }
    Ok(None)
}

async fn check_clear<E: Effects>(fx: &E, t: &Target) -> Result<Option<Value>, ControlError> {
    let mut screen = fx.open_screen(t).await?;
    let out = modal_refusal(fx, t, &mut screen).await;
    screen.close().await;
    out
}

fn delivered(t: &Target, text: &str, d: &Delivery) -> Value {
    with(
        ok_for(t),
        json!({
            "hosting": d.lane.hosting(),
            "text": text,
            "submitted": d.submitted,
            "confirmed": d.confirmed.as_str(),
        }),
    )
}

struct Command<'a> {
    text: &'a str,
    what: &'a str,
    confirm: bool,
    allow_modal: bool,
    change: Option<Change>,
    attempts: u32,
}

impl<'a> Command<'a> {
    fn new(text: &'a str, what: &'a str) -> Self {
        Self { text, what, confirm: false, allow_modal: false, change: None, attempts: sender::ATTEMPTS }
    }
}

/// Submit the command to the target; the caller's own session gets a resume prompt after it.
async fn command<E: Effects>(fx: &E, t: &Target, cmd: Command<'_>) -> Result<Value, ControlError> {
    if t.is_self {
        let req = SendRequest {
            confirm: cmd.confirm,
            allow_modal: cmd.allow_modal,
            change: cmd.change,
            ..SendRequest::new(cmd.text, Some(t.pid))
        };
        let out = fx.send_self(req).await?;
        return Ok(with(out, json!({"self": true, "job": t.job})));
    }
    if let Some(no) = guards::refusal(cmd.text, cmd.confirm, cmd.allow_modal)? {
        return Ok(no);
    }
    let result = fx.deliver(t, cmd.text, cmd.what, cmd.attempts).await?;
    Ok(delivered(t, cmd.text, &result))
}

/// The confirm dialog a settings command may raise: which one, and its title, lowercased.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Switch {
    Effort,
    Model,
}

impl Switch {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Effort => "effort",
            Self::Model => "model",
        }
    }

    pub fn title(self) -> &'static str {
        match self {
            Self::Effort => "change effort level?",
            Self::Model => "switch model?",
        }
    }
}

/// Type a settings command. `answers` is the dialog and value for one that may ask to confirm.
async fn slash<E: Effects>(
    fx: &E,
    job: Option<&str>,
    caller_pid: Option<u32>,
    text: &str,
    what: &str,
    change: Change,
    answers: Option<(Switch, &str)>,
) -> Result<Value, ControlError> {
    let t = resolve(fx, job, caller_pid).await?;
    require_running(&t)?;
    if let Some(no) = check_clear(fx, &t).await? {
        return Ok(no);
    }
    let inline = answers.is_some() && !t.is_self && fx.between_turns(&t).await;
    let mut guard = DefaultsGuard::new(answers.map(|(kind, _)| kind), fx.home().settings()).await;
    let carried = change.clone();
    let out = command(fx, &t, Command { change: Some(change), ..Command::new(text, what) }).await?;
    let Some((kind, value)) = answers else { return Ok(out) };
    if !is_true(&out, "ok") || !is_true(&out, "submitted") {
        return Ok(out);
    }
    if inline {
        let dialog = answer_switch_confirm(fx, &t, kind, value, Wait::INLINE, None, Some(&mut guard)).await?;
        let dialog = with(dialog, guard.restore().await);
        return Ok(with_dialog(out, dialog));
    }
    answer_later(fx, &t, kind, value.into(), Some(carried), guard);
    let mut detail = String::from(
        "the command runs when the turn ends, and the service then answers Claude Code's confirm dialog",
    );
    if t.is_self {
        detail.push_str("; a follow-up prompt reports the result");
    }
    Ok(with(out, json!({"switch_dialog": {"ok": true, "answered": false, "pending": true, "detail": detail}})))
}

/// Holds the user's saved defaults while `/effort` or `/model` runs: both save their value as the
/// default for new sessions (see [`settings_guard`]).
pub struct DefaultsGuard {
    on: bool,
    path: PathBuf,
    snap: Option<Snapshot>,
}

impl DefaultsGuard {
    /// A guard over `settings` for the command that raises `answers`; off for any other command.
    pub async fn new(answers: Option<Switch>, settings: PathBuf) -> Self {
        let on = answers.is_some();
        let snap = if on { settings_guard::snapshot(&settings).await } else { None };
        Self { on, path: settings, snap }
    }

    /// Whether this guard puts anything back.
    pub fn on(&self) -> bool {
        self.on
    }

    /// Re-read the defaults: the dialog can open long after the command went in.
    async fn refresh(&mut self) {
        if self.on {
            if let Some(snap) = settings_guard::snapshot(&self.path).await {
                self.snap = Some(snap);
            }
        }
    }

    async fn restore(&self) -> Value {
        if !self.on {
            return json!({});
        }
        let out = settings_guard::restore(self.snap.as_ref(), settings_guard::WRITE_WAIT, &TokioPace::default()).await;
        if out.restored.is_empty() {
            json!({})
        } else {
            json!({"defaults_restored": out.restored})
        }
    }
}

fn with_dialog(out: Value, dialog: Value) -> Value {
    let failed = !is_true(&dialog, "ok");
    let why = dialog.get("reason").cloned();
    let mut out = with(out, json!({"switch_dialog": dialog}));
    if failed {
        out = with(out, json!({"ok": false, "reason": why}));
    }
    out
}

/// True once the promise that carried a change has been delivered: seen on disk, then gone.
struct PromiseGone {
    pid: u32,
    key: String,
    seen: bool,
}

impl PromiseGone {
    fn new(pid: u32, change: &Change) -> Self {
        Self { pid, key: change.key.clone(), seen: false }
    }

    async fn check(&mut self, promises: &Promises) -> bool {
        let here = promises.has_change(self.pid, &self.key).await;
        self.seen |= here;
        self.seen && !here
    }
}

fn summary(out: &Value) -> String {
    ["detail", "reason"]
        .iter()
        .find_map(|k| out.get(*k).filter(|v| truthy(Some(v))).map(text_of))
        .unwrap_or_else(|| if is_true(out, "ok") { "True".into() } else { "False".into() })
}

/// Answer the confirm dialog from a detached task, then write how it ended onto the promise.
fn answer_later<E: Effects>(
    fx: &E,
    t: &Target,
    kind: Switch,
    value: String,
    change: Option<Change>,
    mut guard: DefaultsGuard,
) {
    let (fx2, t) = (fx.clone(), t.clone());
    fx.spawn(Box::pin(async move {
        let mut give_up = change.as_ref().map(|c| PromiseGone::new(t.pid, c));
        let answered =
            answer_switch_confirm(&fx2, &t, kind, &value, Wait::SELF, give_up.as_mut(), Some(&mut guard)).await;
        let out = answered.unwrap_or_else(|e| {
            tracing::warn!("reflection: answering the {} dialog of {} failed: {e}", kind.as_str(), t.label());
            reason(format!("the service could not read the session's screen ({e})"))
        });
        tracing::info!("reflection: {} dialog of {}: {}", kind.as_str(), t.label(), summary(&out));
        if let Some(change) = &change {
            let (outcome, detail) = if is_true(&out, "ok") { (APPLIED, String::new()) } else {
                (FAILED, out.get("reason").map(text_of).unwrap_or_default())
            };
            fx2.promises().settle(t.pid, &change.key, outcome, &detail).await;
        }
        guard.restore().await;
    }));
}

// --- verbs: one-line commands -----------------------------------------------

fn is_model_name(name: &str) -> bool {
    let mut chars = name.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && chars.all(|c| c.is_ascii_alphanumeric() || "._[]:-".contains(c))
}

fn is_rc_name(name: &str) -> bool {
    let b = name.as_bytes();
    (1..=64).contains(&b.len())
        && b[0].is_ascii_alphanumeric()
        && b[1..].iter().all(|c| c.is_ascii_alphanumeric() || b"._-".contains(c))
}

/// Switch the target's model with `/model <name>`, answering the confirm dialog it may raise.
pub async fn model<E: Effects>(
    fx: &E,
    name: &str,
    job: Option<&str>,
    caller_pid: Option<u32>,
) -> Result<Value, ControlError> {
    if !is_model_name(name) {
        return Ok(reason("model must be a model name or alias"));
    }
    let text = format!("/model {name}");
    let change = new_change("model", name, "");
    slash(fx, job, caller_pid, &text, "the model change", change, Some((Switch::Model, name))).await
}

/// Set the target's effort with `/effort <level>`, at most high, answering the confirm dialog.
pub async fn effort<E: Effects>(
    fx: &E,
    level: &str,
    job: Option<&str>,
    caller_pid: Option<u32>,
) -> Result<Value, ControlError> {
    if level == "xhigh" || level == "max" {
        return Ok(reason(format!(
            "effort {level} is above the limit. High is the upper limit for any model."
        )));
    }
    if !EFFORTS.contains(&level) {
        return Ok(reason(format!("effort must be one of {}", EFFORTS.join(", "))));
    }
    let text = format!("/effort {level}");
    let change = new_change("effort", level, "");
    slash(fx, job, caller_pid, &text, "the effort change", change, Some((Switch::Effort, level))).await
}

/// Rename the target with `/rename <name>`.
pub async fn rename<E: Effects>(
    fx: &E,
    name: &str,
    job: Option<&str>,
    caller_pid: Option<u32>,
) -> Result<Value, ControlError> {
    let name = name.trim();
    if name.is_empty() || name.chars().count() > 80 || !is_printable(name) || name.starts_with('-') {
        return Ok(reason("name must be a short printable string on one line that does not start with '-'"));
    }
    if name.to_lowercase().contains("<warm") {
        return Ok(reason("a name may not carry the warm pool's mark"));
    }
    let text = format!("/rename {name}");
    slash(fx, job, caller_pid, &text, "the rename", new_change("rename", name, ""), None).await
}

async fn realpath(path: &str) -> PathBuf {
    tokio::fs::canonicalize(path).await.unwrap_or_else(|_| PathBuf::from(path))
}

/// Move the target to `path` by typing `/cd`, then reconnect its awm server, which the move drops.
///
/// A directory Claude Code has not been trusted in is refused: `/cd` would open the trust dialog,
/// whose default is to stay put, and the next keystroke would answer it. A worktree of an awm
/// project is trusted first instead, since awm made it. `brief` reaches the session once the move
/// applied: the caller's own in its follow-up, any other as a message after the reconnect.
pub async fn cd<E: Effects>(
    fx: &E,
    path: &str,
    job: Option<&str>,
    caller_pid: Option<u32>,
    brief: &str,
) -> Result<Value, ControlError> {
    let path = path.trim();
    if path.is_empty() || !is_printable(path) || !Path::new(path).is_absolute() {
        return Ok(reason("path must be an absolute directory, on one line"));
    }
    let want = realpath(path).await;
    let shown = want.to_string_lossy().into_owned();
    if !tokio::fs::metadata(&want).await.is_ok_and(|m| m.is_dir()) {
        return Ok(reason(format!("{shown} is not a directory on this node")));
    }
    if !fx.trusted(&want).await {
        let granted = fx.grant_awm_worktree(&want, &fx.projects_root()).await;
        if !granted.is_empty() {
            tracing::info!("reflection: trusted awm worktree {shown} ({})", granted.join(", "));
        }
    }
    if !fx.trusted(&want).await {
        return Ok(reason(format!(
            "Claude Code is not trusted in {shown} or any directory above it, so `/cd` would open its trust \
             dialog and wait on a person. Open `claude` once in {shown} and accept the dialog, then retry."
        )));
    }
    let t = resolve(fx, job, caller_pid).await?;
    require_running(&t)?;
    if cwd(fx.home(), &t).await.as_deref() == Some(shown.as_str()) {
        return Ok(with(
            ok_for(&t),
            json!({"cwd": shown, "changed": false, "detail": format!("{} is already in {shown}", t.label())}),
        ));
    }
    if let Some(no) = check_clear(fx, &t).await? {
        return Ok(no);
    }
    let text = format!("/cd {shown}");
    let change = new_change("cd", &shown, brief);
    let out = command(fx, &t, Command { change: Some(change), ..Command::new(&text, "the move") }).await?;
    if t.is_self || !is_true(&out, "ok") || !is_true(&out, "submitted") {
        return Ok(with(out, json!({"cwd": shown})));
    }
    if !fx.between_turns(&t).await {
        let (fx2, t2, want2, brief2) = (fx.clone(), t.clone(), shown.clone(), brief.to_string());
        fx.spawn(Box::pin(async move {
            reconnect_once_moved(&fx2, &t2, &want2, Wait::SELF, &brief2).await;
        }));
        return Ok(with(
            out,
            json!({
                "cwd": shown,
                "moved": false,
                "detail": format!(
                    "{} is mid-turn, so the move runs when that turn ends; its awm server is reconnected after it",
                    t.label()
                ),
            }),
        ));
    }
    let moved = await_cwd(fx.home(), &t, &shown, Wait::INLINE_MOVE).await;
    let reconnected = moved && reconnect(fx, &t).await;
    let mut out = with(out, json!({"cwd": shown, "moved": moved, "mcp_reconnected": reconnected}));
    if moved && !brief.is_empty() {
        out = with(out, json!({"brief_sent": send_brief(fx, &t, brief).await}));
    }
    if !moved {
        out = with(
            out,
            json!({"detail": format!(
                "the session record does not show {shown} after {}s; `list_other` shows where it is",
                secs(MOVE_WAIT)
            )}),
        );
    }
    Ok(out)
}

async fn cwd(home: &ClaudeHome, t: &Target) -> Option<String> {
    let record = target::read_session_record(home, t.pid).await.ok()?;
    let cwd = record.get("cwd").and_then(Value::as_str).filter(|c| !c.is_empty())?;
    Some(realpath(cwd).await.to_string_lossy().into_owned())
}

async fn await_cwd(home: &ClaudeHome, t: &Target, want: &str, wait: Wait) -> bool {
    let deadline = Instant::now() + wait.within;
    while cwd(home, t).await.as_deref() != Some(want) {
        if Instant::now() >= deadline {
            return false;
        }
        sleep(wait.poll).await;
    }
    true
}

async fn reconnect<E: Effects>(fx: &E, t: &Target) -> bool {
    match mcp_other(fx, t, RECONNECT_AFTER_MOVE).await {
        Ok(_) => true,
        Err(e) => {
            tracing::warn!("reflection: could not reconnect awm for {} after its move: {e}", t.label());
            false
        }
    }
}

async fn send_brief<E: Effects>(fx: &E, t: &Target, brief: &str) -> bool {
    match fx.deliver(t, brief, "the brief after a move", sender::ATTEMPTS).await {
        Ok(_) => true,
        Err(e) => {
            tracing::warn!("reflection: could not give {} its brief after its move: {e}", t.label());
            false
        }
    }
}

async fn reconnect_once_moved<E: Effects>(fx: &E, t: &Target, want: &str, wait: Wait, brief: &str) {
    if await_cwd(fx.home(), t, want, wait).await {
        reconnect(fx, t).await;
        if !brief.is_empty() {
            send_brief(fx, t, brief).await;
        }
    } else {
        tracing::warn!("reflection: {} never reached {want}; awm was not reconnected", t.label());
    }
}

/// Reconnect, enable or disable MCP servers by typing the `/mcp` line that does it.
///
/// The caller's own session runs it when its turn ends and gets one follow-up with what the
/// command printed. Another session between turns answers with its output here; one mid-turn runs
/// it when that turn ends.
pub async fn mcp<E: Effects>(
    fx: &E,
    action: &str,
    server: &str,
    job: Option<&str>,
    caller_pid: Option<u32>,
) -> Result<Value, ControlError> {
    let line = match guards::mcp_line(action, server) {
        Ok(line) => line,
        Err(why) => return Ok(reason(why)),
    };
    let t = resolve(fx, job, caller_pid).await?;
    require_running(&t)?;
    if let Some(no) = check_clear(fx, &t).await? {
        return Ok(no);
    }
    if t.is_self {
        let req = SendRequest {
            allow_modal: true,
            change: Some(new_change("mcp", &format!("{action} {}", server.trim()), "")),
            ..SendRequest::new(&line, Some(t.pid))
        };
        return Ok(with(fx.send_self(req).await?, json!({"self": true, "job": t.job})));
    }
    Ok(mcp_other(fx, &t, &line).await?)
}

/// Type `line` into another session; read its output when it runs at once.
async fn mcp_other<E: Effects>(fx: &E, t: &Target, line: &str) -> Result<Value, DeliveryError> {
    let between = fx.between_turns(t).await;
    let since = watcher::now_ms();
    let result = fx.deliver(t, line, &format!("`{line}`"), sender::ATTEMPTS).await?;
    let out = delivered(t, line, &result);
    if !between {
        return Ok(with(
            out,
            json!({"output": null, "detail": format!("{} is mid-turn, so `{line}` runs when that turn ends", t.label())}),
        ));
    }
    Ok(match fx.await_output(t, line, since).await {
        None => with(
            out,
            json!({"output": null, "detail": format!(
                "`{line}` printed nothing readable within {}s", secs(MCP_OUTPUT_WAIT)
            )}),
        ),
        Some(said) => with(out, json!({"output": said})),
    })
}

/// Type `text` into another session's prompt, under the guards a self-send has.
///
/// Takes no `submit`, `followup` or `delay_ms`: a send with a ref drops them, as it always has.
pub async fn message<E: Effects>(
    fx: &E,
    job: Option<&str>,
    text: &str,
    caller_pid: Option<u32>,
    confirm: bool,
) -> Result<Value, ControlError> {
    if job.is_none_or(|j| j.trim().is_empty()) {
        return Ok(reason("job is required: the session to message"));
    }
    if let Some(no) = guards::refusal(text, confirm, false)? {
        return Ok(no);
    }
    let t = resolve(fx, job, caller_pid).await?;
    require_running(&t)?;
    if let Some(no) = check_clear(fx, &t).await? {
        return Ok(no);
    }
    command(fx, &t, Command { confirm, ..Command::new(text, "the message") }).await
}

/// Press Escape once in the target, which interrupts a running turn.
pub async fn interrupt<E: Effects>(fx: &E, job: Option<&str>, caller_pid: Option<u32>) -> Result<Value, ControlError> {
    let t = resolve(fx, job, caller_pid).await?;
    require_running(&t)?;
    let mut screen = fx.open_screen(&t).await?;
    let out = interrupt_on(fx, &t, &mut screen).await;
    screen.close().await;
    out
}

async fn interrupt_on<E: Effects>(fx: &E, t: &Target, screen: &mut Screen<E::Lane>) -> Result<Value, ControlError> {
    let _held = screen.hold().await?;
    if let Some(no) = modal_refusal(fx, t, screen).await? {
        return Ok(no);
    }
    let before = fx.status(t).await;
    screen.press(Key::Escape).await?;
    Ok(with(ok_for(t), json!({"status_before": before})))
}

// --- link --------------------------------------------------------------------

fn link_fields(bridge: Option<&str>) -> Value {
    let link = roster::bridge_link(bridge);
    let on = bridge.is_some_and(|b| !b.is_empty());
    let mut out = json!({"remote_control": on, "link": link});
    if link.is_none() {
        let why = if on { "the bridge id is not in a form with an app link" } else { "Remote Control is off" };
        out = with(out, json!({"link_reason": why}));
    }
    out
}

/// Read the target's Remote Control link from its record. Types nothing.
pub async fn link<E: Effects>(fx: &E, job: Option<&str>, caller_pid: Option<u32>) -> Result<Value, ControlError> {
    let t = resolve(fx, job, caller_pid).await?;
    let bridge = bridge_id(fx.home(), &t).await;
    Ok(with(with(ok_for(&t), json!({"running": t.alive})), link_fields(bridge.as_deref())))
}

// --- stop --------------------------------------------------------------------

/// A stop that keeps the job is done once stopped; one that removes it, once removed.
fn done(result: &StopOutcome) -> bool {
    if result.kept {
        result.stopped
    } else {
        result.removed
    }
}

fn stop_failure(result: &StopOutcome) -> String {
    if !result.detail.is_empty() {
        result.detail.clone()
    } else if result.kept {
        "claude stop failed".into()
    } else {
        "claude rm failed".into()
    }
}

fn outcome_json(result: &StopOutcome) -> Value {
    serde_json::to_value(result).unwrap_or_default()
}

async fn remove_job<R: Remover>(remove: &R, job: &str) -> StopOutcome {
    match JobId::parse(job) {
        Ok(id) => remove.remove(&id).await,
        Err(e) => StopOutcome { stopped: false, removed: false, kept: false, detail: e.to_string() },
    }
}

/// Stop a background session and remove its job through `remove`.
///
/// The caller's own session is refused without `confirm`, and is stopped after this call has
/// returned. A terminal session the caller retires itself is left open for its terminal to close.
pub async fn stop<E: Effects, R: Remover>(
    fx: &E,
    job: Option<&str>,
    caller_pid: Option<u32>,
    confirm: bool,
    remove: R,
) -> Result<Value, ControlError> {
    let Some(job) = job.map(str::trim).filter(|j| !j.is_empty()) else {
        return Ok(reason("job is required: the session to stop"));
    };
    let t = match resolve(fx, Some(job), caller_pid).await {
        Ok(t) => t,
        Err(e @ (ControlError::Resolve(_) | ControlError::Unknown(_))) => {
            // A job whose session has exited has no live row to resolve, and would otherwise stay
            // in the fleet view as stopped for good.
            let listed = lineage::is_job(job)
                && tokio::fs::metadata(fx.home().jobs().join(job)).await.is_ok_and(|m| m.is_dir());
            if !listed {
                return Err(e);
            }
            let result = remove_job(&remove, job).await;
            let mut out = with(json!({"ok": done(&result), "job": job, "exited": true}), outcome_json(&result));
            if !done(&result) {
                out = with(out, json!({"reason": stop_failure(&result)}));
            }
            return Ok(out);
        }
        Err(e) => return Err(e),
    };
    let terminal = t.kind != SessionKind::Background || t.job.as_deref().is_none_or(str::is_empty);
    if terminal && t.is_self && confirm {
        // The caller learns of it, so the sessions that wait on it wake. Its terminal closes it.
        return Ok(with(
            ok_for(&t),
            json!({
                "terminal": true,
                "stopped": false,
                "note": "a terminal session is retired but still open: type /exit into it, or end the turn and let \
                         its owner close it",
            }),
        ));
    }
    if terminal {
        let why = format!("{} is a terminal session, not a background job; close its terminal instead", describe(&t));
        return Ok(refused(&t, &why));
    }
    if t.is_self && !confirm {
        let why = "that is your own session; stopping it ends this conversation. Pass confirm=true to proceed.";
        return Ok(with(refused(&t, why), json!({"kind": "self"})));
    }
    let short = t.job.clone().unwrap_or_default();
    if t.is_self {
        let name = t.name.clone().unwrap_or_else(|| "None".into());
        let job = short.clone();
        fx.spawn(Box::pin(async move {
            sleep(SELF_STOP_DELAY).await;
            // Nobody is left to hear the answer, so the log is the only record.
            let result = remove_job(&remove, &job).await;
            if done(&result) {
                let removed = if result.kept { "" } else { " and removed its job" };
                tracing::info!("cch: stopped own session {job} ({name}){removed}");
            } else {
                tracing::warn!("reflection: own session {job} ({name}) was not removed: {}", result.detail);
            }
        }));
        return Ok(with(ok_for(&t), json!({"stopping": true})));
    }
    let result = remove_job(&remove, &short).await;
    let mut out = with(with(json!({"ok": done(&result)}), identify(&t)), outcome_json(&result));
    if !done(&result) {
        out = with(out, json!({"reason": stop_failure(&result)}));
    }
    Ok(out)
}

// --- Remote Control ----------------------------------------------------------

/// How long a screen wait lasts, and how often it looks.
#[derive(Debug, Clone, Copy)]
struct Wait {
    within: Duration,
    poll: Duration,
}

impl Wait {
    const INLINE: Self = Self { within: MODAL_WAIT, poll: POLL };
    const INLINE_MOVE: Self = Self { within: MOVE_WAIT, poll: POLL };
    const SELF: Self = Self { within: SELF_MODAL_WAIT, poll: SELF_POLL };
}

/// Turn the target's Remote Control on or off.
///
/// On types `/rc <name>` and waits briefly for the link. Off types `/rc`, which opens a modal, and
/// selects "Disconnect this session" in it. Both ask the record first: a bare `/rc` on a connected
/// session opens that modal, so the state decides which command is safe to type.
pub async fn remote_control<E: Effects>(
    fx: &E,
    on: bool,
    name: Option<&str>,
    job: Option<&str>,
    caller_pid: Option<u32>,
) -> Result<Value, ControlError> {
    let t = resolve(fx, job, caller_pid).await?;
    require_running(&t)?;
    let bridge = bridge_id(fx.home(), &t).await;
    if on {
        if name.is_some_and(|n| !is_rc_name(n)) {
            return Ok(reason("name must be a plain label"));
        }
        if bridge.is_some() {
            let out = with(ok_for(&t), json!({"changed": false, "detail": "Remote Control is already on"}));
            return Ok(with(out, link_fields(bridge.as_deref())));
        }
        return turn_on(fx, &t, name).await;
    }
    if bridge.is_none() {
        let out = with(ok_for(&t), json!({"changed": false, "detail": "Remote Control is already off"}));
        return Ok(with(out, link_fields(None)));
    }
    turn_off(fx, &t).await
}

async fn turn_on<E: Effects>(fx: &E, t: &Target, name: Option<&str>) -> Result<Value, ControlError> {
    if let Some(no) = check_clear(fx, t).await? {
        return Ok(no);
    }
    let text = match name.filter(|n| !n.is_empty()) {
        Some(n) => format!("/rc {n}"),
        None => "/rc".into(),
    };
    let what = "the Remote Control command";
    if t.is_self {
        // The command queues behind this very turn, so there is nothing to wait for here; the
        // resume prompt carries the link.
        let change = new_change("rc_on", name.unwrap_or_default(), "");
        let sent =
            command(fx, t, Command { change: Some(change), allow_modal: true, ..Command::new(&text, what) }).await?;
        if !is_true(&sent, "ok") {
            return Ok(sent);
        }
        return Ok(with(
            sent,
            json!({
                "link": null,
                "link_reason": "the command runs when your current turn ends; a follow-up prompt will carry the link",
            }),
        ));
    }
    let queued = mid_turn(fx, t).await;
    // One attempt: a second `/rc` would land in whatever the first opened.
    let sent = command(fx, t, Command { allow_modal: true, attempts: 1, ..Command::new(&text, what) }).await?;
    if queued {
        // The command queues behind the turn; the link arrives as the `remote_control` session
        // event when it runs, so waiting out the link timeout here would only delay the answer.
        if !is_true(&sent, "ok") {
            return Ok(sent);
        }
        let why = format!("{} is mid-turn, so the command runs when that turn ends", t.label());
        return Ok(with(sent, json!({"applies": "turn_end", "link": null, "link_reason": why})));
    }
    let (found, why) = await_link(fx, t).await;
    let mut out = with(sent, json!({"remote_control": found.is_some(), "link": found}));
    if found.is_none() {
        out = with(out, json!({"link_reason": why}));
    }
    Ok(out)
}

async fn await_link<E: Effects>(fx: &E, t: &Target) -> (Option<String>, String) {
    let deadline = Instant::now() + LINK_WAIT;
    loop {
        if let Some(found) = roster::bridge_link(bridge_id(fx.home(), t).await.as_deref()) {
            return (Some(found), String::new());
        }
        if Instant::now() >= deadline {
            break;
        }
        sleep(POLL).await;
    }
    let mut why = format!("no bridge id in the job record after {}s", secs(LINK_WAIT));
    if mid_turn(fx, t).await {
        why.push_str("; the session is mid-turn, so the command may still be queued");
    }
    (None, why)
}

/// Whether the record says `t` is in a turn, so a typed command waits for it to end.
async fn mid_turn<E: Effects>(fx: &E, t: &Target) -> bool {
    matches!(fx.status(t).await.as_deref(), Some("busy" | "shell"))
}

async fn turn_off<E: Effects>(fx: &E, t: &Target) -> Result<Value, ControlError> {
    if t.is_self {
        return turn_off_later(fx, t, true).await;
    }
    if mid_turn(fx, t).await {
        return turn_off_later(fx, t, false).await;
    }
    if !fx.between_turns(t).await {
        let why = format!(
            "{} is mid-turn, so `/rc` would queue and open its modal later with nobody to answer it. Interrupt it or \
             retry when it is idle.",
            t.label()
        );
        return Ok(refused(t, &why));
    }
    if let Some(no) = check_clear(fx, t).await? {
        return Ok(no);
    }
    // One attempt: a second `/rc` + Enter would land in the modal the first opened.
    let error = fx.deliver(t, "/rc", "the Remote Control command", 1).await.err().map(|e| e.to_string());
    drive_disconnect(fx, t, Wait::INLINE, error).await
}

/// Queue `/rc` behind the turn and answer its modal from a detached walk once the turn ends. Only
/// the caller's own session (`resume`) gets the follow-up prompt.
async fn turn_off_later<E: Effects>(fx: &E, t: &Target, resume: bool) -> Result<Value, ControlError> {
    if let Some(no) = check_clear(fx, t).await? {
        return Ok(no);
    }
    fx.deliver(t, "/rc", "the Remote Control command", 1).await?;
    let (fx2, t2) = (fx.clone(), t.clone());
    let who = if resume { "self" } else { "turn-end" };
    fx.spawn(Box::pin(async move {
        match drive_disconnect(&fx2, &t2, Wait::SELF, None).await {
            Ok(out) => {
                tracing::info!("reflection: {who} Remote Control off for {}: {}", t2.label(), summary(&out));
                if !resume || is_true(&out, "modal_open") || is_true(&out, "refused") {
                    return;
                }
                let prompt = crate::resume::from_cch(SELF_OFF_FOLLOWUP);
                if let Err(e) = fx2.deliver(&t2, &prompt, "the resume prompt", sender::ATTEMPTS).await {
                    tracing::warn!("reflection: self Remote Control off for {} failed: {e}", t2.label());
                }
            }
            Err(e) => tracing::warn!("reflection: {who} Remote Control off for {} failed: {e}", t2.label()),
        }
    }));
    if !resume {
        let detail = format!(
            "{} is mid-turn, so `/rc` is queued behind that turn; when its modal opens the service selects \
             'Disconnect this session'",
            t.label()
        );
        return Ok(with(
            ok_for(t),
            json!({"changed": false, "remote_control": true, "applies": "turn_end", "detail": detail}),
        ));
    }
    Ok(with(
        ok_for(t),
        json!({
            "changed": false,
            "remote_control": true,
            "detail": "`/rc` is queued behind this turn; when its modal opens the service selects 'Disconnect this \
                       session' and then resumes you",
        }),
    ))
}

/// The text of the option the cursor is on, from the latest paint of it.
pub fn selected_option(screen: &str) -> Option<String> {
    CURSOR.captures_iter(&guards::plain_screen(screen)).last().map(|c| c[1].trim().to_string())
}

/// Which key moves the cursor toward the disconnect option, or None if unknown.
fn direction(screen: &str) -> Option<Key> {
    let plain = guards::plain_screen(screen);
    let lines: Vec<&str> = plain.split('\n').collect();
    let target = lines.iter().rposition(|ln| ln.to_lowercase().contains(DISCONNECT_OPTION))?;
    let cursor = lines.iter().rposition(|ln| CURSOR.is_match(ln))?;
    Some(if cursor > target { Key::Up } else { Key::Down })
}

/// Walk the Remote Control modal to "Disconnect this session" and confirm it.
///
/// Refuses, and closes whatever is open with Escape, when the modal is not the one expected, so a
/// wrong layout can never press Enter on a different option.
async fn drive_disconnect<E: Effects>(
    fx: &E,
    t: &Target,
    wait: Wait,
    delivery_error: Option<String>,
) -> Result<Value, ControlError> {
    let mut screen = fx.open_screen(t).await?;
    let out = drive_on(fx, t, &mut screen, wait, delivery_error).await;
    screen.close().await;
    out
}

fn shows_disconnect(screen: &str) -> bool {
    guards::plain_screen(screen).to_lowercase().contains(DISCONNECT_OPTION)
}

async fn drive_on<E: Effects>(
    fx: &E,
    t: &Target,
    sess: &mut Screen<E::Lane>,
    wait: Wait,
    delivery_error: Option<String>,
) -> Result<Value, ControlError> {
    let Some(screen) = await_modal(sess, wait).await? else {
        let why = delivery_error.unwrap_or_else(|| {
            format!("the Remote Control modal did not appear within {}s; nothing was selected", secs(wait.within))
        });
        return Ok(with(with(json!({"ok": false}), identify(t)), json!({"remote_control": true, "reason": why})));
    };
    let held = sess.hold().await?;
    if !shows_disconnect(&screen) {
        sess.press(Key::Escape).await?;
        let why = "a modal opened but it does not offer 'Disconnect this session'; it was closed with Escape and \
                   nothing was selected";
        return Ok(with(refused(t, why), json!({"kind": "unexpected_modal", "modal_open": false})));
    }
    if let Some(why) = walk_to_disconnect(sess, screen).await? {
        sess.press(Key::Escape).await?;
        return Ok(with(refused(t, &why), json!({"kind": "unexpected_modal", "modal_open": false})));
    }
    sess.press(Key::Enter).await?;
    sleep(KEY_SETTLE).await;
    drop(held);
    let cleared = await_cleared(fx, t).await;
    let bridge = if cleared { None } else { bridge_id(fx.home(), t).await };
    let mut out = with(
        with(json!({"ok": cleared}), identify(t)),
        json!({"remote_control": !cleared, "changed": cleared, "bridge_cleared": cleared}),
    );
    out = with(out, link_fields(bridge.as_deref()));
    if !cleared {
        let why = format!(
            "'Disconnect this session' was selected but the bridge id did not clear within {}s",
            secs(CLEAR_WAIT)
        );
        let open = guards::modal_on_screen(&sess.read().await?).is_some();
        out = with(out, json!({"reason": why, "modal_open": open}));
    }
    Ok(out)
}

/// The screen once a modal is on it, or None if none appears within the wait.
async fn await_modal<L: Lane>(sess: &mut Screen<L>, wait: Wait) -> Result<Option<String>, ControlError> {
    let deadline = Instant::now() + wait.within;
    loop {
        let screen = sess.read().await?;
        if shows_disconnect(&screen) || guards::modal_on_screen(&screen).is_some() {
            return Ok(Some(screen));
        }
        if Instant::now() >= deadline {
            return Ok(None);
        }
        sleep(wait.poll).await;
    }
}

/// Move the cursor onto 'Disconnect this session'. `Some` is the reason it could not.
async fn walk_to_disconnect<L: Lane>(sess: &mut Screen<L>, mut screen: String) -> Result<Option<String>, ControlError> {
    let mut key = None;
    for _ in 0..=MAX_MODAL_STEPS {
        if selected_option(&screen).is_some_and(|picked| picked.to_lowercase().contains(DISCONNECT_OPTION)) {
            return Ok(None);
        }
        let step = match key {
            Some(k) => k,
            None => match direction(&screen) {
                Some(k) => *key.insert(k),
                None => {
                    return Ok(Some(
                        "the modal shows no selection cursor to steer by; it was closed with Escape and nothing was \
                         selected"
                            .into(),
                    ))
                }
            },
        };
        sess.forget();
        sess.press(step).await?;
        sleep(KEY_SETTLE).await;
        screen = sess.read().await?;
    }
    Ok(Some(format!(
        "the cursor did not reach 'Disconnect this session' in {MAX_MODAL_STEPS} steps; the modal was closed with Escape"
    )))
}

async fn await_cleared<E: Effects>(fx: &E, t: &Target) -> bool {
    let deadline = Instant::now() + CLEAR_WAIT;
    loop {
        if bridge_id(fx.home(), t).await.is_none() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        sleep(POLL).await;
    }
}

// --- the effort / model confirm dialog ---------------------------------------

/// What the dialog's selection says to do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Verdict {
    /// A PreModelSwitch hook raised it: the user's own gate.
    Hook,
    /// Yes to the switch that was asked for.
    Enter,
    No,
    /// Yes to some other switch.
    Wrong,
    Unclear,
}

/// Whether "Yes, switch to <offered>" is the switch that was asked for.
fn offer_names(kind: Switch, value: &str, offered: &str) -> bool {
    let offered = offered.trim().to_lowercase();
    match kind {
        // `(?![a-z0-9])` after the level: `high` must not take an offer of `higher`.
        Switch::Effort => offered
            .strip_prefix(value.to_lowercase().as_str())
            .is_some_and(|rest| !rest.starts_with(|c: char| c.is_ascii_lowercase() || c.is_ascii_digit())),
        // A model is offered under its display name, which an alias or a dated id only partly
        // spells, so every word of the request must appear in the offer.
        Switch::Model => {
            let base = BRACKETS.replace_all(&value.to_lowercase(), "").into_owned();
            let words: Vec<&str> = WORDS
                .split(&base)
                .filter(|w| !w.is_empty() && *w != "claude" && !(w.len() >= 8 && w.bytes().all(|b| b.is_ascii_digit())))
                .collect();
            let offer: HashSet<&str> = WORDS.split(&offered).collect();
            !words.is_empty() && words.iter().all(|w| offer.contains(w))
        }
    }
}

/// The verdict and selected option for the dialog text from its title down.
fn read_switch_dialog(kind: Switch, value: &str, dialog: &str) -> (Verdict, String) {
    if dialog.to_lowercase().contains(HOOK_ASKED) {
        return (Verdict::Hook, String::new());
    }
    let picked = selected_option(dialog).unwrap_or_default();
    if let Some(yes) = YES_SWITCH.captures(&picked) {
        let verdict = if offer_names(kind, value, &yes[1]) { Verdict::Enter } else { Verdict::Wrong };
        return (verdict, picked);
    }
    let verdict = if NO_OPTION.is_match(&picked) { Verdict::No } else { Verdict::Unclear };
    (verdict, picked)
}

/// The byte index of the last place `needle` (lowercase ASCII) starts in `hay`, case-blind.
fn rfind_folded(hay: &str, needle: &str) -> Option<usize> {
    hay.char_indices().rev().map(|(i, _)| i).find(|&i| {
        let mut rest = hay[i..].chars().flat_map(char::to_lowercase);
        needle.chars().all(|n| rest.next() == Some(n))
    })
}

/// Answer Claude Code's "Change effort level?" / "Switch model?" dialog.
///
/// Presses Enter only with the cursor on "Yes, switch to <value>". The cursor on "No" closes the
/// dialog with Escape. A dialog a PreModelSwitch hook raised is left alone. No dialog within the
/// wait is success, since a conversation without output switches without asking; `give_up` ends
/// the wait early with that same answer, and `before_press` re-reads the defaults just before Enter.
async fn answer_switch_confirm<E: Effects>(
    fx: &E,
    t: &Target,
    kind: Switch,
    value: &str,
    wait: Wait,
    give_up: Option<&mut PromiseGone>,
    before_press: Option<&mut DefaultsGuard>,
) -> Result<Value, ControlError> {
    let mut screen = fx.open_screen(t).await?;
    let out = answer_on(fx, &mut screen, kind, value, wait, give_up, before_press).await;
    screen.close().await;
    out
}

async fn answer_on<E: Effects>(
    fx: &E,
    sess: &mut Screen<E::Lane>,
    kind: Switch,
    value: &str,
    wait: Wait,
    mut give_up: Option<&mut PromiseGone>,
    mut before_press: Option<&mut DefaultsGuard>,
) -> Result<Value, ControlError> {
    let deadline = Instant::now() + wait.within;
    let mut wrong_since: Option<Instant> = None;
    let (mut verdict, mut picked, mut screen);
    loop {
        screen = guards::current_screen(&sess.read().await?);
        (verdict, picked) = match rfind_folded(&screen, kind.title()) {
            Some(at) => {
                let (v, p) = read_switch_dialog(kind, value, &screen[at..]);
                (Some(v), p)
            }
            None => (None, String::new()),
        };
        match verdict {
            Some(Verdict::Enter) => {
                if let Some(guard) = before_press.as_deref_mut() {
                    guard.refresh().await;
                }
                let _held = sess.hold().await?;
                sess.press(Key::Enter).await?;
                sleep(KEY_SETTLE).await;
                return Ok(json!({
                    "ok": true, "answered": true, "dialog": true,
                    "detail": format!("pressed Enter on {}", py_repr(&picked)),
                }));
            }
            Some(Verdict::Hook) => {
                return Ok(json!({
                    "ok": false, "answered": false, "dialog": true,
                    "reason": "a PreModelSwitch hook asked for the confirmation, so it was left for you to answer",
                }))
            }
            Some(Verdict::No) => {
                let _held = sess.hold().await?;
                sess.press(Key::Escape).await?;
                return Ok(json!({
                    "ok": false, "answered": false, "dialog": true,
                    "reason": format!("the dialog's cursor was on {}, so it was closed with Escape", py_repr(&picked)),
                }));
            }
            _ => {}
        }
        let now = Instant::now();
        if verdict != Some(Verdict::Wrong) {
            wrong_since = None;
        } else if wrong_since.is_none() {
            wrong_since = Some(now);
        }
        if wrong_since.is_some_and(|since| now.duration_since(since) >= WRONG_OFFER_GRACE) {
            let _held = sess.hold().await?;
            sess.press(Key::Escape).await?;
            return Ok(json!({
                "ok": false, "answered": false, "dialog": true,
                "reason": format!(
                    "the dialog offered {}, not a switch to {value}; it was closed with Escape", py_repr(&picked)
                ),
            }));
        }
        let gave_up = match give_up.as_deref_mut() {
            Some(gone) => gone.check(fx.promises()).await,
            None => false,
        };
        if gave_up || now >= deadline {
            break;
        }
        sleep(wait.poll).await;
    }
    if verdict.is_some() {
        let shown = if picked.is_empty() { "none" } else { picked.as_str() };
        return Ok(json!({
            "ok": false, "answered": false, "dialog": true,
            "reason": format!("the dialog's selection ({}) was not one to answer; nothing was pressed", py_repr(shown)),
        }));
    }
    if let Some(other) = guards::modal_on_screen(&screen) {
        return Ok(json!({
            "ok": false, "answered": false, "dialog": false,
            "reason": format!("a different prompt was showing instead ({}); nothing was pressed", py_repr(other)),
        }));
    }
    Ok(json!({"ok": true, "answered": false, "dialog": false, "detail": "no confirm dialog appeared"}))
}

// --- the live effects ----------------------------------------------------------

struct LiveParts<W> {
    paths: Paths,
    host: Host,
    promises: Promises,
    watch: W,
}

/// The real [`Effects`]: this host's sessions, the sender, the promise store and tokio.
pub struct Live<W> {
    parts: Arc<LiveParts<W>>,
}

impl<W> Clone for Live<W> {
    fn clone(&self) -> Self {
        Self { parts: self.parts.clone() }
    }
}

impl<W> Live<W> {
    /// `watch` arms the resume of a command sent to the caller's own session.
    pub fn new(paths: Paths, host: Host, promises: Promises, watch: W) -> Self {
        Self { parts: Arc::new(LiveParts { paths, host, promises, watch }) }
    }

    fn sender(&self) -> Sender<Host, Host, LiveProbe> {
        let p = &self.parts;
        Sender { detect: p.host.clone(), opener: p.host.clone(), probe: LiveProbe { home: p.paths.home.clone() } }
    }
}

impl<W: ResumeWatch<Tail> + Clone + Send + Sync + 'static> Effects for Live<W> {
    type Lane = AnyLane;

    fn home(&self) -> &ClaudeHome {
        &self.parts.paths.home
    }

    fn promises(&self) -> &Promises {
        &self.parts.promises
    }

    fn projects_root(&self) -> PathBuf {
        self.parts.paths.projects_dir()
    }

    async fn open_screen(&self, t: &Target) -> Result<Screen<AnyLane>, ControlError> {
        let lane = self.parts.host.resolve(t.pid, None).await?;
        let lock = walk_lock(&lane);
        Ok(Screen::new(self.parts.host.open_session(&lane).await?, lock))
    }

    async fn deliver(&self, t: &Target, text: &str, what: &str, attempts: u32) -> Result<Delivery, DeliveryError> {
        let mut tail = Tail::new(self.home().clone(), t.pid);
        tail.watch(text);
        tail.poll().await;
        let opts = DeliverOpts { enter: true, attempts, what };
        self.sender().deliver(t.pid, text, opts, Some(&mut tail)).await
    }

    async fn status(&self, t: &Target) -> Option<String> {
        watcher::read_status(self.home(), t.pid).await.map(|(status, _)| status)
    }

    async fn between_turns(&self, t: &Target) -> bool {
        let Some((status, at)) = watcher::read_status(self.home(), t.pid).await else { return false };
        let mut tail = Tail::new(self.home().clone(), t.pid);
        tail.poll().await;
        watcher::is_settled(&status, at, watcher::now_ms(), &tail, "")
    }

    async fn send_self(&self, req: SendRequest) -> Result<Value, SendError> {
        let pid = req.caller_pid.unwrap_or_default();
        let tail = Tail::new(self.home().clone(), pid);
        let mut watch = self.parts.watch.clone();
        self.sender().send(&req, &self.parts.promises, tail, &mut watch).await
    }

    async fn await_output(&self, t: &Target, line: &str, since_ms: i64) -> Option<String> {
        let mut tail = Tail::new(self.home().clone(), t.pid);
        tail.watch(line);
        let deadline = Instant::now() + MCP_OUTPUT_WAIT;
        loop {
            tail.poll().await;
            let said = tail.output(line, since_ms);
            if said.is_some() || Instant::now() >= deadline {
                return said;
            }
            sleep(POLL).await;
        }
    }

    async fn trusted(&self, dir: &Path) -> bool {
        trust::trusted(dir, self.home()).await
    }

    async fn grant_awm_worktree(&self, dir: &Path, projects_root: &Path) -> Vec<String> {
        trust::grant_awm_worktree(dir, projects_root, self.home()).await
    }

    fn spawn(&self, job: Detached) {
        tokio::spawn(job);
    }
}

#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::fmt::Debug;
    use std::sync::Mutex as StdMutex;

    use serde_json::Map;
    use tempfile::TempDir;

    use super::*;
    use crate::promises::{render_followup, Pending};
    use crate::sender::Confirmed;
    use crate::target::TmuxLane;

    const SELF_JOB: &str = "aaaaaaaa";
    const OTHER_JOB: &str = "bbbbbbbb";
    const DEAD_JOB: &str = "cccccccc";
    // Above the kernel's largest pid, so no process ever holds it.
    const DEAD_PID: u32 = (1 << 22) + 12345;
    const PLAIN: &str = "some output\n? for shortcuts";
    const PROMPT: &str = "Do you want to proceed?\n❯ 1. Yes\n  2. No\nEsc to cancel";
    const OPTIONS: [&str; 3] = ["Show QR code", "Disconnect this session", "Continue"];
    const MODEL_KEY: &str = "model-a";

    fn me() -> u32 {
        std::process::id()
    }

    fn parent() -> u32 {
        std::os::unix::process::parent_id()
    }

    fn proc_start(pid: u32) -> String {
        procfs::start_ticks(pid).expect("a live pid")
    }

    // --- a scripted screen ---

    type OnPress = Arc<dyn Fn(Key) + Send + Sync>;

    #[derive(Default)]
    struct ScreenState {
        frames: VecDeque<String>,
        // The Remote Control modal: a list with a cursor that Down and Up move.
        modal: Option<(Vec<String>, usize)>,
        presses: Vec<Key>,
        closed: bool,
        on_press: Option<OnPress>,
    }

    /// A scripted screen: `frames` are read in turn, the last one repeating.
    #[derive(Clone, Default)]
    struct FakeScreen(Arc<StdMutex<ScreenState>>);

    impl FakeScreen {
        fn new(frames: &[&str]) -> Self {
            let s = Self::default();
            s.0.lock().unwrap().frames = frames.iter().map(|f| f.to_string()).collect();
            s
        }

        fn modal(options: &[&str], cursor: usize) -> Self {
            let s = Self::default();
            s.0.lock().unwrap().modal = Some((options.iter().map(|o| o.to_string()).collect(), cursor));
            s
        }

        fn on_press(&self, f: impl Fn(Key) + Send + Sync + 'static) {
            self.0.lock().unwrap().on_press = Some(Arc::new(f));
        }

        fn presses(&self) -> Vec<Key> {
            self.0.lock().unwrap().presses.clone()
        }

        fn closed(&self) -> bool {
            self.0.lock().unwrap().closed
        }

        fn render(&self) -> String {
            let mut s = self.0.lock().unwrap();
            if let Some((options, cursor)) = &s.modal {
                let rows: Vec<String> = options
                    .iter()
                    .enumerate()
                    .map(|(i, o)| format!("{}{o}", if i == *cursor { "❯ " } else { "  " }))
                    .collect();
                return format!("Remote Control\n{}\nEnter to select", rows.join("\n"));
            }
            if s.frames.len() > 1 {
                s.frames.pop_front().unwrap_or_default()
            } else {
                s.frames.front().cloned().unwrap_or_default()
            }
        }
    }

    impl Lane for FakeScreen {
        fn label(&self) -> String {
            "fake screen".into()
        }
        async fn write(&mut self, _text: &str) -> Result<(), LaneError> {
            Ok(())
        }
        async fn clear(&mut self) -> Result<(), LaneError> {
            Ok(())
        }
        async fn check_not_rejected(&mut self) -> Result<(), LaneError> {
            Ok(())
        }
        async fn commit(&mut self) -> Result<(), LaneError> {
            Ok(())
        }
        async fn read(&mut self) -> Result<String, LaneError> {
            Ok(self.render())
        }
        async fn glance(&mut self) -> Result<String, LaneError> {
            Ok(self.render())
        }
        async fn repaint(&mut self) -> Result<bool, LaneError> {
            Ok(false)
        }
        fn forget(&mut self) {}
        async fn press(&mut self, key: Key) -> Result<(), LaneError> {
            let hook = {
                let mut s = self.0.lock().unwrap();
                s.presses.push(key);
                if let Some((options, cursor)) = &mut s.modal {
                    let n = options.len();
                    *cursor = match key {
                        Key::Down => (*cursor + 1) % n,
                        Key::Up => (*cursor + n - 1) % n,
                        _ => *cursor,
                    };
                }
                s.on_press.clone()
            };
            if let Some(hook) = hook {
                hook(key);
            }
            Ok(())
        }
        async fn close(self) {
            self.0.lock().unwrap().closed = true;
        }
    }

    // --- the world: real records in a temporary home, fakes for the rest ---

    struct State {
        typed: Vec<(String, String, u32)>,
        sent: Vec<SendRequest>,
        screen: FakeScreen,
        status: String,
        settled: bool,
        opened: Vec<Target>,
        spawned: Vec<Detached>,
        // The screen shown once anything is typed.
        after_deliver: Option<FakeScreen>,
        // The record's cwd follows each `/cd` typed.
        follow_cd: bool,
        // Claude Code saves its settings when a command is typed.
        save_on_deliver: Option<fn(&mut Value)>,
        trust_all: bool,
        trusted: HashSet<PathBuf>,
        grant_works: bool,
        grants: Vec<(PathBuf, PathBuf)>,
        projects_root: PathBuf,
        output: Option<String>,
        output_seen: Vec<String>,
        output_forbidden: bool,
    }

    #[derive(Clone)]
    struct World {
        _tmp: Arc<TempDir>,
        root: PathBuf,
        home: ClaudeHome,
        promises: Promises,
        s: Arc<StdMutex<State>>,
    }

    fn settings_doc() -> Value {
        json!({
            "theme": "dark",
            "model": "opus",
            "effortLevel": "medium",
            "modelSettings": {MODEL_KEY: {"effortLevel": "high"}},
            "hooks": {"Stop": []},
        })
    }

    fn settings_text() -> String {
        serde_json::to_string_pretty(&settings_doc()).unwrap() + "\n"
    }

    fn save_settings(path: &Path, edit: fn(&mut Value)) {
        let mut doc: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        edit(&mut doc);
        std::fs::write(path, serde_json::to_string_pretty(&doc).unwrap() + "\n").unwrap();
    }

    fn save_effort(doc: &mut Value) {
        doc["modelSettings"][MODEL_KEY]["effortLevel"] = json!("low");
    }

    fn fake_lane() -> crate::target::Target {
        crate::target::Target::Tmux(TmuxLane {
            pane: "%0".into(),
            session_id: "00000000-0000-4000-8000-000000000001".into(),
            repl_pid: 1,
            name: None,
        })
    }

    impl World {
        fn new() -> Self {
            let tmp = TempDir::new().unwrap();
            let root = tmp.path().canonicalize().unwrap();
            let home = ClaudeHome::at(root.join("claude"), root.join("claude.json"));
            for dir in [home.jobs(), home.sessions(), home.daemon_dir()] {
                std::fs::create_dir_all(dir).unwrap();
            }
            std::fs::write(home.settings(), settings_text()).unwrap();
            let state = State {
                typed: Vec::new(),
                sent: Vec::new(),
                screen: FakeScreen::new(&[PLAIN]),
                status: "idle".into(),
                settled: true,
                opened: Vec::new(),
                spawned: Vec::new(),
                after_deliver: None,
                follow_cd: false,
                save_on_deliver: None,
                trust_all: false,
                trusted: HashSet::new(),
                grant_works: false,
                grants: Vec::new(),
                projects_root: root.join("projects"),
                output: None,
                output_seen: Vec::new(),
                output_forbidden: false,
            };
            let w = Self {
                promises: Promises::new(root.join("pending")),
                _tmp: Arc::new(tmp),
                root,
                home,
                s: Arc::new(StdMutex::new(state)),
            };
            w.add_job(SELF_JOB, "me", me(), true);
            w.add_job(OTHER_JOB, "other one", parent(), true);
            w
        }

        fn s(&self) -> std::sync::MutexGuard<'_, State> {
            self.s.lock().unwrap()
        }

        fn add_job(&self, short: &str, name: &str, pid: u32, alive: bool) {
            let roster = self.home.roster();
            let mut doc: Value =
                std::fs::read(&roster).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or(json!({"workers": {}}));
            doc["workers"][short] = json!({
                "replPid": pid,
                "sessionId": format!("sid-{short}"),
                "replProcStart": if alive { proc_start(pid) } else { "1".into() },
            });
            std::fs::write(&roster, doc.to_string()).unwrap();
            std::fs::create_dir_all(self.home.jobs().join(short)).unwrap();
            std::fs::write(self.home.job_state(short), json!({"name": name}).to_string()).unwrap();
            if alive {
                self.record(pid, "bg", name, json!({"jobId": short}));
            }
        }

        fn record(&self, pid: u32, kind: &str, name: &str, extra: Value) {
            let rec = with(
                json!({"pid": pid, "kind": kind, "name": name, "sessionId": format!("sid-{pid}"), "procStart": proc_start(pid)}),
                extra,
            );
            std::fs::write(self.home.session_record(pid), rec.to_string()).unwrap();
        }

        fn set_bridge(&self, short: &str, bridge: &str) {
            let path = self.home.job_state(short);
            let mut rec: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
            rec["bridgeSessionId"] = json!(bridge);
            std::fs::write(&path, rec.to_string()).unwrap();
        }

        fn set_cwd(&self, pid: u32, cwd: &str) {
            let path = self.home.session_record(pid);
            let mut rec: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
            rec["cwd"] = json!(cwd);
            std::fs::write(&path, rec.to_string()).unwrap();
        }

        fn typed(&self) -> Vec<(String, String, u32)> {
            self.s().typed.clone()
        }

        fn typed_texts(&self) -> Vec<String> {
            self.typed().into_iter().map(|(_, text, _)| text).collect()
        }

        fn sent(&self) -> Vec<SendRequest> {
            self.s().sent.clone()
        }

        fn opened(&self) -> usize {
            self.s().opened.len()
        }

        fn screen(&self) -> FakeScreen {
            self.s().screen.clone()
        }

        fn set_screen(&self, screen: FakeScreen) {
            self.s().screen = screen;
        }

        fn spawned(&self) -> Vec<Detached> {
            std::mem::take(&mut self.s().spawned)
        }

        fn spawned_count(&self) -> usize {
            self.s().spawned.len()
        }

        fn settings(&self) -> String {
            std::fs::read_to_string(self.home.settings()).unwrap()
        }

        /// What Claude Code does on Enter: rewrite settings.json with `edit` applied.
        fn cli_saves(&self, edit: fn(&mut Value)) -> impl Fn(Key) + Send + Sync + 'static {
            let path = self.home.settings();
            move |key| {
                if key == Key::Enter {
                    save_settings(&path, edit);
                }
            }
        }

        /// Show `screen` only once the command has been typed; before that it is clear.
        fn after_typing(&self, screen: &str) -> FakeScreen {
            let shown = FakeScreen::new(&[screen]);
            let mut s = self.s();
            s.after_deliver = Some(shown.clone());
            s.screen = FakeScreen::new(&[PLAIN]);
            shown
        }
    }

    impl Effects for World {
        type Lane = FakeScreen;

        fn home(&self) -> &ClaudeHome {
            &self.home
        }

        fn promises(&self) -> &Promises {
            &self.promises
        }

        fn projects_root(&self) -> PathBuf {
            self.s().projects_root.clone()
        }

        async fn open_screen(&self, t: &Target) -> Result<Screen<FakeScreen>, ControlError> {
            let mut s = self.s();
            s.opened.push(t.clone());
            Ok(Screen::new(s.screen.clone(), Arc::new(Mutex::new(()))))
        }

        async fn deliver(&self, t: &Target, text: &str, _what: &str, attempts: u32) -> Result<Delivery, DeliveryError> {
            let (follow, save) = {
                let mut s = self.s();
                let who = t.job.clone().unwrap_or_else(|| t.pid.to_string());
                s.typed.push((who, text.into(), attempts));
                if let Some(next) = s.after_deliver.clone() {
                    s.screen = next;
                }
                (s.follow_cd, s.save_on_deliver)
            };
            if let Some(dir) = text.strip_prefix("/cd ").filter(|_| follow) {
                self.set_cwd(t.pid, dir);
            }
            if let Some(edit) = save {
                save_settings(&self.home.settings(), edit);
            }
            Ok(Delivery { submitted: true, lane: fake_lane(), confirmed: Confirmed::Record })
        }

        async fn status(&self, _t: &Target) -> Option<String> {
            Some(self.s().status.clone())
        }

        async fn between_turns(&self, _t: &Target) -> bool {
            self.s().settled
        }

        async fn send_self(&self, req: SendRequest) -> Result<Value, SendError> {
            let text = req.text.clone();
            self.s().sent.push(req);
            Ok(json!({"ok": true, "submitted": true, "text": text}))
        }

        async fn await_output(&self, _t: &Target, line: &str, _since_ms: i64) -> Option<String> {
            let mut s = self.s();
            assert!(!s.output_forbidden, "waited");
            s.output_seen.push(line.into());
            s.output.clone()
        }

        async fn trusted(&self, dir: &Path) -> bool {
            let s = self.s();
            s.trust_all || s.trusted.contains(dir)
        }

        async fn grant_awm_worktree(&self, dir: &Path, projects_root: &Path) -> Vec<String> {
            let mut s = self.s();
            s.grants.push((dir.to_path_buf(), projects_root.to_path_buf()));
            if !s.grant_works {
                return Vec::new();
            }
            s.trusted.insert(dir.to_path_buf());
            vec![dir.to_string_lossy().into_owned()]
        }

        fn spawn(&self, job: Detached) {
            self.s().spawned.push(job);
        }
    }

    fn switch_dialog(title: &str, offer: &str, cursor: usize, note: &str) -> String {
        let options = [format!("1. Yes, switch to {offer}"), "2. No, go back".to_string()];
        let rows: Vec<String> = options
            .iter()
            .enumerate()
            .map(|(i, o)| format!("{}{o}", if i == cursor { "❯ " } else { "  " }))
            .collect();
        format!(
            "{title}\n{note}This conversation is cached for the current setting. Switching means the next turn \
             re-reads it uncached.\n{}\nEnter to confirm · Esc to cancel",
            rows.join("\n")
        )
    }

    fn effort_yes() -> String {
        switch_dialog("Change effort level?", "Low", 0, "")
    }

    fn effort_no() -> String {
        switch_dialog("Change effort level?", "Low", 1, "")
    }

    fn model_yes() -> String {
        switch_dialog("Switch model?", "Opus 5.5 (1M context)", 0, "")
    }

    fn hook_asked() -> String {
        switch_dialog("Switch model?", "Opus 5.5", 0, "A PreModelSwitch hook asked you to confirm this change.\n")
    }

    fn me_() -> Option<u32> {
        Some(me())
    }

    fn ok(out: &Value) -> bool {
        out["ok"] == json!(true)
    }

    fn text(out: &Value, key: &str) -> String {
        out[key].as_str().unwrap_or_default().to_string()
    }

    async fn run_spawned(w: &World) {
        for job in w.spawned() {
            job.await;
        }
    }

    // --- resolving `job` ---

    #[tokio::test]
    async fn test_without_job_the_target_is_the_caller() {
        let w = World::new();
        let t = resolve(&w, None, me_()).await.unwrap();
        assert!(t.is_self && t.job.as_deref() == Some(SELF_JOB) && t.kind == SessionKind::Background);
    }

    #[tokio::test]
    async fn test_without_job_or_caller_there_is_nothing_to_act_on() {
        let w = World::new();
        let err = resolve(&w, None, None).await.unwrap_err();
        assert!(matches!(err, ControlError::Resolve(_)) && err.to_string().contains("which session"));
    }

    #[tokio::test]
    async fn test_job_resolves_by_id_and_by_name() {
        let w = World::new();
        assert_eq!(resolve(&w, Some(OTHER_JOB), me_()).await.unwrap().pid, parent());
        let t = resolve(&w, Some("other one"), me_()).await.unwrap();
        assert!(t.job.as_deref() == Some(OTHER_JOB) && !t.is_self && t.alive);
        assert_eq!(resolve(&w, Some("OTHER ONE"), me_()).await.unwrap().job.as_deref(), Some(OTHER_JOB));
    }

    #[tokio::test]
    async fn test_a_job_naming_the_callers_own_session_is_marked_self() {
        let w = World::new();
        assert!(resolve(&w, Some(SELF_JOB), me_()).await.unwrap().is_self);
        assert!(resolve(&w, Some("me"), me_()).await.unwrap().is_self);
        assert!(!resolve(&w, Some("me"), None).await.unwrap().is_self);
    }

    #[tokio::test]
    async fn test_an_ambiguous_name_is_refused_with_the_candidates() {
        let w = World::new();
        w.add_job(DEAD_JOB, "other one", DEAD_PID, false);
        let err = resolve(&w, Some("other one"), me_()).await.unwrap_err();
        let said = err.to_string();
        assert!(matches!(err, ControlError::Resolve(_)));
        assert!(said.contains(OTHER_JOB) && said.contains(DEAD_JOB) && said.contains("job id"), "{said}");
    }

    #[tokio::test]
    async fn test_an_unknown_name_is_refused() {
        let w = World::new();
        let err = resolve(&w, Some("nobody"), me_()).await.unwrap_err();
        assert!(err.to_string().contains("no session"));
        assert_eq!(err, ControlError::Unknown(UnknownSession { unknown_ref: "nobody".into() }));
    }

    #[tokio::test]
    async fn test_an_interactive_session_resolves_by_name_and_has_no_job() {
        let w = World::new();
        w.record(1, "interactive", "terminal one", json!({}));
        let t = resolve(&w, Some("terminal one"), me_()).await.unwrap();
        assert!(t.kind == SessionKind::Interactive && t.job.is_none() && t.pid == 1);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_dead_session_resolves_but_cannot_be_typed_into() {
        let w = World::new();
        w.add_job(DEAD_JOB, "gone", DEAD_PID, false);
        assert!(!resolve(&w, Some("gone"), me_()).await.unwrap().alive);
        let err = model(&w, "opus", Some("gone"), me_()).await.unwrap_err();
        assert!(matches!(err, ControlError::Resolve(_)) && err.to_string().contains("not running"));
        assert!(w.typed().is_empty());
    }

    // --- link ---

    #[tokio::test]
    async fn test_link_is_the_app_url_of_the_bridge_id() {
        let w = World::new();
        w.set_bridge(OTHER_JOB, "cse_01Abc");
        let out = link(&w, Some("other one"), me_()).await.unwrap();
        assert!(ok(&out) && out["remote_control"] == json!(true));
        assert_eq!(out["link"], json!("https://claude.ai/code/session_01Abc"));
        assert_eq!(out["job"], json!(OTHER_JOB));
        assert!(w.typed().is_empty() && w.opened() == 0);
    }

    #[tokio::test]
    async fn test_link_is_null_with_a_reason_when_remote_control_is_off() {
        let w = World::new();
        let out = link(&w, None, me_()).await.unwrap();
        assert!(out["remote_control"] == json!(false) && out["link"].is_null());
        assert!(text(&out, "link_reason").contains("off") && out["self"] == json!(true));
    }

    #[tokio::test]
    async fn test_link_reads_an_interactive_sessions_own_record() {
        let w = World::new();
        w.record(1, "interactive", "term", json!({"bridgeSessionId": "cse_01T"}));
        let out = link(&w, Some("term"), me_()).await.unwrap();
        assert_eq!(out["link"], json!("https://claude.ai/code/session_01T"));
    }

    // --- model, effort, rename ---

    #[tokio::test(start_paused = true)]
    async fn test_a_slash_verb_types_its_command_into_the_named_session() {
        for (verb, arg, typed) in [("model", "opus", "/model opus"), ("effort", "medium", "/effort medium"), ("rename", "  new name ", "/rename new name")] {
            let w = World::new();
            let job = Some(OTHER_JOB);
            let out = match verb {
                "model" => model(&w, arg, job, me_()).await,
                "effort" => effort(&w, arg, job, me_()).await,
                _ => rename(&w, arg, job, me_()).await,
            }
            .unwrap();
            assert!(ok(&out) && out["submitted"] == json!(true) && text(&out, "text") == typed, "{out}");
            assert_eq!(w.typed(), [(OTHER_JOB.to_string(), typed.to_string(), sender::ATTEMPTS)]);
            assert!(w.sent().is_empty());
        }
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_slash_verb_without_job_goes_through_the_self_sender() {
        let w = World::new();
        let out = model(&w, "sonnet[1m]", None, me_()).await.unwrap();
        assert!(ok(&out) && out["self"] == json!(true));
        let sent = w.sent();
        assert_eq!(sent[0].text, "/model sonnet[1m]");
        assert!(sent[0].caller_pid == me_() && w.typed().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_bad_argument_types_nothing() {
        type Call = fn(&World) -> Pin<Box<dyn Future<Output = Result<Value, ControlError>> + Send + '_>>;
        let calls: [Call; 7] = [
            |w| Box::pin(model(w, "--evil", Some(OTHER_JOB), me_())),
            |w| Box::pin(model(w, "a b", Some(OTHER_JOB), me_())),
            |w| Box::pin(effort(w, "ludicrous", Some(OTHER_JOB), me_())),
            |w| Box::pin(rename(w, "", Some(OTHER_JOB), me_())),
            |w| Box::pin(rename(w, "two\nlines", Some(OTHER_JOB), me_())),
            |w| Box::pin(rename(w, "-x", Some(OTHER_JOB), me_())),
            |w| Box::pin(rename(w, "<warm x>", Some(OTHER_JOB), me_())),
        ];
        for call in calls {
            let w = World::new();
            assert_eq!(call(&w).await.unwrap()["ok"], json!(false));
            assert!(w.typed().is_empty() && w.sent().is_empty() && w.opened() == 0);
        }
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_modal_or_prompt_stops_every_typing_verb() {
        for (status, screen) in [("waiting", PLAIN.to_string()), ("idle", PROMPT.to_string()), ("idle", effort_yes()), ("idle", model_yes())] {
            let w = World::new();
            w.s().status = status.into();
            w.set_screen(FakeScreen::new(&[&screen]));
            let job = Some(OTHER_JOB);
            let outs = [
                model(&w, "opus", job, me_()).await.unwrap(),
                effort(&w, "low", job, me_()).await.unwrap(),
                rename(&w, "x", job, me_()).await.unwrap(),
                message(&w, job, "hello", me_(), false).await.unwrap(),
                interrupt(&w, job, me_()).await.unwrap(),
                remote_control(&w, true, None, job, me_()).await.unwrap(),
            ];
            for out in outs {
                assert!(out["ok"] == json!(false) && out["refused"] == json!(true) && out["kind"] == json!("modal"), "{out}");
            }
            assert!(w.typed().is_empty() && w.screen().presses().is_empty() && w.sent().is_empty());
            assert!(w.screen().closed());
        }
    }

    // --- interrupt ---

    #[tokio::test]
    async fn test_interrupt_presses_escape_once() {
        let w = World::new();
        w.s().status = "busy".into();
        let out = interrupt(&w, Some("other one"), me_()).await.unwrap();
        assert!(ok(&out) && out["status_before"] == json!("busy"));
        assert!(w.screen().presses() == [Key::Escape] && w.typed().is_empty());
        assert!(w.screen().closed());
    }

    // --- message ---

    #[tokio::test]
    async fn test_message_types_the_text_into_the_other_session() {
        let w = World::new();
        let out = message(&w, Some("other one"), "please look at X", me_(), false).await.unwrap();
        assert!(ok(&out));
        assert_eq!(w.typed(), [(OTHER_JOB.to_string(), "please look at X".to_string(), sender::ATTEMPTS)]);
    }

    #[tokio::test]
    async fn test_message_has_the_guards_message_self_has() {
        let w = World::new();
        let destructive = message(&w, Some(OTHER_JOB), "/clear", me_(), false).await.unwrap();
        assert!(destructive["refused"] == json!(true) && text(&destructive, "reason").contains("confirm"));
        let modal = message(&w, Some(OTHER_JOB), "/mcp", me_(), false).await.unwrap();
        assert!(modal["refused"] == json!(true) && modal["kind"] == json!("interactive"));
        assert!(w.typed().is_empty());
        assert!(ok(&message(&w, Some(OTHER_JOB), "/clear", me_(), true).await.unwrap()));
    }

    #[tokio::test]
    async fn test_message_needs_a_target_and_text() {
        let w = World::new();
        assert_eq!(message(&w, None, "x", me_(), false).await.unwrap()["ok"], json!(false));
        let err = message(&w, Some(OTHER_JOB), "  ", me_(), false).await.unwrap_err();
        assert_eq!(err, ControlError::Empty(EmptyText));
    }

    #[tokio::test]
    async fn test_message_to_ones_own_session_is_a_self_send() {
        let w = World::new();
        let out = message(&w, Some("me"), "note to self", me_(), false).await.unwrap();
        assert!(ok(&out) && w.sent()[0].text == "note to self" && w.typed().is_empty());
    }

    // --- stop ---

    #[derive(Clone)]
    struct FakeRemover {
        calls: Arc<StdMutex<Vec<String>>>,
        removed: bool,
    }

    impl FakeRemover {
        fn new(removed: bool) -> Self {
            Self { calls: Arc::default(), removed }
        }

        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }

    impl Remover for FakeRemover {
        async fn remove(&self, job: &JobId) -> StopOutcome {
            self.calls.lock().unwrap().push(job.to_string());
            let detail = if self.removed { "" } else { "no such job" };
            StopOutcome { stopped: true, removed: self.removed, kept: false, detail: detail.into() }
        }
    }

    #[tokio::test]
    async fn test_stop_removes_another_session() {
        let w = World::new();
        let rm = FakeRemover::new(true);
        let out = stop(&w, Some("other one"), me_(), false, rm.clone()).await.unwrap();
        assert!(ok(&out) && out["job"] == json!(OTHER_JOB) && rm.calls() == [OTHER_JOB]);
    }

    #[tokio::test]
    async fn test_stop_reaches_a_session_start_other_did_not_create() {
        let w = World::new();
        assert!(ok(&stop(&w, Some(OTHER_JOB), me_(), false, FakeRemover::new(true)).await.unwrap()));
    }

    #[tokio::test]
    async fn test_stop_refuses_the_callers_own_session_without_confirm() {
        let w = World::new();
        let rm = FakeRemover::new(true);
        let out = stop(&w, Some("me"), me_(), false, rm.clone()).await.unwrap();
        assert!(out["ok"] == json!(false) && out["refused"] == json!(true) && out["kind"] == json!("self"));
        assert!(rm.calls().is_empty() && w.spawned_count() == 0);
    }

    #[tokio::test(start_paused = true)]
    async fn test_stop_of_own_session_with_confirm_runs_after_the_call_returns() {
        let w = World::new();
        let rm = FakeRemover::new(true);
        let out = stop(&w, Some(SELF_JOB), me_(), true, rm.clone()).await.unwrap();
        assert!(ok(&out) && out["stopping"] == json!(true) && rm.calls().is_empty());
        run_spawned(&w).await;
        assert_eq!(rm.calls(), [SELF_JOB]);
    }

    type Log = Arc<StdMutex<Vec<String>>>;

    // A subscriber that keeps each event as "LEVEL message".
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
            self.0.lock().unwrap().push(format!("{} {}", event.metadata().level(), message.0));
        }
        fn enter(&self, _span: &tracing::span::Id) {}
        fn exit(&self, _span: &tracing::span::Id) {}
    }

    #[tokio::test(start_paused = true)]
    async fn test_stop_of_own_session_logs_whether_the_job_went() {
        for (removed, level, said) in [(true, "INFO", "and removed its job"), (false, "WARN", "was not removed: no such job")] {
            let w = World::new();
            stop(&w, Some(SELF_JOB), me_(), true, FakeRemover::new(removed)).await.unwrap();
            let lines: Log = Arc::default();
            let guard = tracing::subscriber::set_default(Capture(lines.clone()));
            run_spawned(&w).await;
            drop(guard);
            let lines = lines.lock().unwrap().clone();
            let line = lines.iter().find(|l| l.contains(SELF_JOB)).expect("a line naming the job");
            assert!(line.starts_with(level) && line.contains(said), "{line}");
        }
    }

    #[tokio::test]
    async fn test_stop_reports_a_failed_removal() {
        let w = World::new();
        let out = stop(&w, Some(OTHER_JOB), me_(), false, FakeRemover::new(false)).await.unwrap();
        assert!(out["ok"] == json!(false) && out["reason"] == json!("no such job"));
    }

    #[tokio::test]
    async fn test_stop_removes_a_job_that_is_already_dead() {
        let w = World::new();
        w.add_job(DEAD_JOB, "gone", DEAD_PID, false);
        let rm = FakeRemover::new(true);
        assert!(ok(&stop(&w, Some("gone"), me_(), false, rm.clone()).await.unwrap()));
        assert_eq!(rm.calls(), [DEAD_JOB]);
    }

    #[tokio::test]
    async fn test_stop_removes_an_exited_job_that_no_live_row_names() {
        let w = World::new();
        std::fs::create_dir(w.home.jobs().join("dddddddd")).unwrap();
        let rm = FakeRemover::new(true);
        let out = stop(&w, Some("dddddddd"), me_(), false, rm.clone()).await.unwrap();
        assert!(ok(&out) && out["exited"] == json!(true) && rm.calls() == ["dddddddd"]);
    }

    #[tokio::test]
    async fn test_stop_of_an_unknown_job_still_refuses() {
        let w = World::new();
        let rm = FakeRemover::new(true);
        let err = stop(&w, Some("eeeeeeee"), me_(), false, rm.clone()).await.unwrap_err();
        assert!(matches!(err, ControlError::Unknown(_)));
        assert!(rm.calls().is_empty());
    }

    #[tokio::test]
    async fn test_stop_refuses_a_terminal_session_and_a_missing_job() {
        let w = World::new();
        w.record(1, "interactive", "term", json!({}));
        let rm = FakeRemover::new(true);
        assert_eq!(stop(&w, Some("term"), me_(), false, rm.clone()).await.unwrap()["refused"], json!(true));
        assert_eq!(stop(&w, None, me_(), false, rm.clone()).await.unwrap()["ok"], json!(false));
        assert!(rm.calls().is_empty());
    }

    #[tokio::test]
    async fn test_a_terminal_session_retires_itself_and_is_left_open() {
        // The caller's own session, a terminal one named "term".
        let w = World::new();
        w.record(me(), "interactive", "term", json!({}));
        let rm = FakeRemover::new(true);
        let out = stop(&w, Some("term"), me_(), true, rm.clone()).await.unwrap();
        assert!(ok(&out) && out["terminal"] == json!(true) && out["stopped"] == json!(false), "{out}");
        assert!(rm.calls().is_empty());
    }

    // --- Remote Control: on ---

    #[tokio::test(start_paused = true)]
    async fn test_on_types_rc_with_the_name_and_returns_the_link_when_it_appears() {
        let w = World::new();
        let later = w.clone();
        // The bridge id reaches the record between the second and third look.
        tokio::spawn(async move {
            sleep(Duration::from_millis(750)).await;
            later.set_bridge(OTHER_JOB, "cse_01New");
        });
        let out = remote_control(&w, true, Some("phone-1"), Some("other one"), me_()).await.unwrap();
        assert!(ok(&out) && out["link"] == json!("https://claude.ai/code/session_01New"), "{out}");
        assert_eq!(w.typed(), [(OTHER_JOB.to_string(), "/rc phone-1".to_string(), 1)], "one attempt, no retry");
        assert!(out.get("link_reason").is_none());
    }

    #[tokio::test(start_paused = true)]
    async fn test_on_without_a_name_types_a_bare_rc() {
        let w = World::new();
        remote_control(&w, true, None, Some(OTHER_JOB), me_()).await.unwrap();
        assert_eq!(w.typed()[0].1, "/rc");
    }

    #[tokio::test(start_paused = true)]
    async fn test_on_returns_a_null_link_with_a_reason_when_the_bridge_never_appears() {
        let w = World::new();
        let start = Instant::now();
        let out = remote_control(&w, true, None, Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out["link"].is_null() && out["remote_control"] == json!(false));
        assert!(text(&out, "link_reason").contains("no bridge id"));
        assert!(start.elapsed() >= LINK_WAIT);
    }

    #[tokio::test]
    async fn test_on_when_already_on_types_nothing_and_returns_the_link() {
        let w = World::new();
        w.set_bridge(OTHER_JOB, "cse_01On");
        let out = remote_control(&w, true, None, Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out["changed"] == json!(false));
        assert_eq!(out["link"], json!("https://claude.ai/code/session_01On"));
        assert!(w.typed().is_empty());
    }

    #[tokio::test]
    async fn test_on_rejects_a_name_that_could_be_a_flag() {
        let w = World::new();
        let out = remote_control(&w, true, Some("--x"), Some(OTHER_JOB), me_()).await.unwrap();
        assert_eq!(out["ok"], json!(false));
        assert!(w.typed().is_empty());
    }

    #[tokio::test]
    async fn test_on_for_the_callers_own_session_queues_and_resumes_with_a_pointer_to_link() {
        let w = World::new();
        let out = remote_control(&w, true, Some("me-rc"), None, me_()).await.unwrap();
        let req = &w.sent()[0];
        assert!(req.text == "/rc me-rc" && req.allow_modal);
        assert!(req.change.as_ref().unwrap().kind == "rc_on" && req.followup.is_none());
        assert!(out["link"].is_null() && text(&out, "link_reason").contains("turn ends"));
    }

    // --- Remote Control: off ---

    fn connected() -> World {
        let w = World::new();
        w.set_bridge(OTHER_JOB, "cse_01On");
        w
    }

    #[tokio::test]
    async fn test_off_when_already_off_types_nothing() {
        let w = World::new();
        let out = remote_control(&w, false, None, Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out["changed"] == json!(false) && w.typed().is_empty());
    }

    /// Run `off` with the pre-check clean and `modal` appearing after `/rc`.
    async fn drive(w: &World, modal: &FakeScreen, then_clear: bool) -> Value {
        w.set_screen(FakeScreen::new(&[PLAIN]));
        w.s().after_deliver = Some(modal.clone());
        let at = w.clone();
        modal.on_press(move |key| {
            if key == Key::Enter && then_clear {
                at.set_bridge(OTHER_JOB, "");
            }
        });
        remote_control(w, false, None, Some(OTHER_JOB), me_()).await.unwrap()
    }

    #[tokio::test(start_paused = true)]
    async fn test_off_types_rc_and_selects_disconnect_in_the_modal() {
        let w = connected();
        let modal = FakeScreen::modal(&OPTIONS, 0);
        let out = drive(&w, &modal, true).await;
        assert!(ok(&out) && out["bridge_cleared"] == json!(true) && out["remote_control"] == json!(false), "{out}");
        assert_eq!(w.typed(), [(OTHER_JOB.to_string(), "/rc".to_string(), 1)]);
        assert_eq!(modal.presses(), [Key::Down, Key::Enter], "Show QR -> Disconnect");
    }

    #[tokio::test(start_paused = true)]
    async fn test_off_walks_up_when_the_cursor_is_below_the_option() {
        let w = connected();
        let modal = FakeScreen::modal(&["Disconnect this session", "Show QR code", "Continue"], 2);
        let out = drive(&w, &modal, true).await;
        assert!(ok(&out));
        let presses = modal.presses();
        assert_eq!(presses.last(), Some(&Key::Enter));
        assert!(presses[..presses.len() - 1].iter().all(|k| *k == Key::Up));
    }

    #[tokio::test(start_paused = true)]
    async fn test_off_presses_enter_at_once_when_disconnect_is_already_selected() {
        let w = connected();
        let modal = FakeScreen::modal(&OPTIONS, 1);
        let out = drive(&w, &modal, true).await;
        assert!(ok(&out) && modal.presses() == [Key::Enter]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_off_reports_when_the_bridge_id_does_not_clear() {
        let w = connected();
        let modal = FakeScreen::modal(&OPTIONS, 1);
        let start = Instant::now();
        let out = drive(&w, &modal, false).await;
        assert!(out["ok"] == json!(false) && out["bridge_cleared"] == json!(false));
        assert_eq!(out["remote_control"], json!(true));
        assert!(start.elapsed() >= CLEAR_WAIT);
    }

    #[tokio::test(start_paused = true)]
    async fn test_off_refuses_a_modal_that_is_not_the_expected_one() {
        let w = connected();
        let modal = FakeScreen::modal(&["Yes", "No"], 0);
        let out = drive(&w, &modal, true).await;
        assert!(out["ok"] == json!(false) && out["kind"] == json!("unexpected_modal"));
        assert_eq!(modal.presses(), [Key::Escape], "closed, and Enter never pressed");
    }

    #[tokio::test(start_paused = true)]
    async fn test_off_refuses_a_modal_with_no_cursor_to_steer_by() {
        let w = connected();
        let modal = FakeScreen::new(&["Remote Control\n  Show QR code\n  Disconnect this session\nEnter to select"]);
        let out = drive(&w, &modal, true).await;
        assert!(out["ok"] == json!(false) && modal.presses() == [Key::Escape]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_off_gives_up_without_pressing_when_no_modal_appears() {
        let w = connected();
        let start = Instant::now();
        let out = drive(&w, &FakeScreen::new(&[PLAIN]), true).await;
        assert!(out["ok"] == json!(false) && text(&out, "reason").contains("did not appear"));
        assert!(w.screen().presses().is_empty());
        assert!(start.elapsed() >= MODAL_WAIT);
    }

    #[tokio::test]
    async fn test_off_refuses_a_session_that_is_mid_turn() {
        let w = connected();
        w.s().settled = false;
        let out = remote_control(&w, false, None, Some(OTHER_JOB), me_()).await.unwrap();
        assert!(out["ok"] == json!(false) && out["refused"] == json!(true) && text(&out, "reason").contains("mid-turn"));
        assert!(w.typed().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn test_off_for_the_callers_own_session_queues_rc_and_finishes_later() {
        let w = connected();
        w.set_bridge(SELF_JOB, "cse_01Me");
        let out = remote_control(&w, false, None, None, me_()).await.unwrap();
        assert!(ok(&out) && out["remote_control"] == json!(true));
        assert!(w.typed() == [(SELF_JOB.to_string(), "/rc".to_string(), 1)] && w.spawned_count() == 1);
        let modal = FakeScreen::modal(&OPTIONS, 1);
        w.set_screen(modal.clone());
        let at = w.clone();
        modal.on_press(move |key| {
            if key == Key::Enter {
                at.set_bridge(SELF_JOB, "");
            }
        });
        run_spawned(&w).await;
        assert_eq!(modal.presses(), [Key::Enter]);
        assert_eq!(
            w.typed().last().unwrap(),
            &(SELF_JOB.to_string(), crate::resume::from_cch(SELF_OFF_FOLLOWUP), sender::ATTEMPTS)
        );
    }

    #[tokio::test(start_paused = true)]
    async fn the_prompt_after_remote_control_off_carries_the_cch_prefix() {
        let w = connected();
        w.set_bridge(SELF_JOB, "cse_01Me");
        remote_control(&w, false, None, None, me_()).await.unwrap();
        let modal = FakeScreen::modal(&OPTIONS, 1);
        w.set_screen(modal.clone());
        let at = w.clone();
        modal.on_press(move |key| {
            if key == Key::Enter {
                at.set_bridge(SELF_JOB, "");
            }
        });
        run_spawned(&w).await;
        let prefix = format!("from cch@{}: ", awm_plugin_sdk::config::node_name());
        let (_, typed, _) = w.typed().last().cloned().unwrap();
        assert!(typed.starts_with(&prefix) && typed.ends_with(SELF_OFF_FOLLOWUP), "{typed}");
    }

    // --- Remote Control on a busy session: applies at turn end ---

    #[tokio::test(start_paused = true)]
    async fn rc_on_for_a_busy_session_queues_and_answers_at_once() {
        let w = World::new();
        w.s().status = "busy".into();
        let start = Instant::now();
        let out = remote_control(&w, true, Some("phone-1"), Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out["applies"] == json!("turn_end") && out["link"].is_null(), "{out}");
        assert!(out["session"] == json!("other one") && out["job"] == json!(OTHER_JOB) && out["self"] == json!(false));
        assert!(text(&out, "link_reason").contains("mid-turn"));
        assert_eq!(w.typed(), [(OTHER_JOB.to_string(), "/rc phone-1".to_string(), 1)], "one attempt, no retry");
        assert!(start.elapsed() < POLL, "the link timeout is not waited out");
    }

    #[tokio::test(start_paused = true)]
    async fn rc_on_for_a_session_running_a_shell_command_queues_too() {
        let w = World::new();
        w.s().status = "shell".into();
        let out = remote_control(&w, true, None, Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out["applies"] == json!("turn_end") && out["link"].is_null());
    }

    #[tokio::test(start_paused = true)]
    async fn rc_off_for_a_busy_session_queues_and_disconnects_at_turn_end_without_a_resume_prompt() {
        let w = connected();
        w.s().status = "busy".into();
        w.s().settled = false;
        let out = remote_control(&w, false, None, Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out["applies"] == json!("turn_end") && out["remote_control"] == json!(true), "{out}");
        assert!(w.typed() == [(OTHER_JOB.to_string(), "/rc".to_string(), 1)] && w.spawned_count() == 1);
        let modal = FakeScreen::modal(&OPTIONS, 0);
        w.set_screen(modal.clone());
        let at = w.clone();
        modal.on_press(move |key| {
            if key == Key::Enter {
                at.set_bridge(OTHER_JOB, "");
            }
        });
        run_spawned(&w).await;
        assert_eq!(modal.presses(), [Key::Down, Key::Enter]);
        assert_eq!(w.typed_texts(), ["/rc"], "no follow-up prompt for another session");
    }

    #[tokio::test]
    async fn rc_off_for_a_busy_session_showing_a_modal_types_nothing() {
        let w = connected();
        w.s().status = "busy".into();
        w.set_screen(FakeScreen::new(&[PROMPT]));
        let out = remote_control(&w, false, None, Some(OTHER_JOB), me_()).await.unwrap();
        assert!(out["refused"] == json!(true) && out["kind"] == json!("modal"), "{out}");
        assert!(w.typed().is_empty() && w.spawned_count() == 0);
    }

    // --- the effort / model confirm dialog ---

    #[tokio::test(start_paused = true)]
    async fn test_an_effort_dialog_with_the_cursor_on_yes_is_answered_with_enter() {
        let w = World::new();
        let shown = w.after_typing(&effort_yes());
        let out = effort(&w, "low", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out["switch_dialog"]["answered"] == json!(true), "{out}");
        assert!(shown.presses() == [Key::Enter] && shown.closed());
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_model_dialog_with_the_cursor_on_yes_is_answered_with_enter() {
        let w = World::new();
        let shown = w.after_typing(&model_yes());
        let out = model(&w, "opus", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && shown.presses() == [Key::Enter]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_an_alias_with_a_context_suffix_still_names_the_offered_model() {
        let w = World::new();
        let shown = w.after_typing(&switch_dialog("Switch model?", "Sonnet 5.5 (1M context)", 0, ""));
        assert!(ok(&model(&w, "sonnet[1m]", Some(OTHER_JOB), me_()).await.unwrap()));
        assert_eq!(shown.presses(), [Key::Enter]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_dialog_with_the_cursor_on_no_is_closed_with_escape() {
        let w = World::new();
        let shown = w.after_typing(&effort_no());
        let out = effort(&w, "low", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(out["ok"] == json!(false) && text(&out, "reason").contains("No, go back"), "{out}");
        assert_eq!(shown.presses(), [Key::Escape]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_dialog_a_hook_raised_is_left_alone() {
        let w = World::new();
        let shown = w.after_typing(&hook_asked());
        let out = model(&w, "opus", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(out["ok"] == json!(false) && text(&out, "reason").contains("hook"));
        assert!(shown.presses().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn test_no_dialog_is_success_because_a_fresh_conversation_does_not_ask() {
        let w = World::new();
        let shown = w.after_typing(PLAIN);
        let start = Instant::now();
        let out = effort(&w, "low", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out["switch_dialog"]["dialog"] == json!(false));
        assert!(shown.presses().is_empty());
        assert!(start.elapsed() >= MODAL_WAIT);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_yes_for_another_effort_is_not_taken_for_the_one_asked() {
        // `high` is a prefix of `xhigh`; the dialog must offer exactly the asked level.
        let w = World::new();
        let shown = w.after_typing(&switch_dialog("Change effort level?", "xhigh", 0, ""));
        let start = Instant::now();
        let out = effort(&w, "high", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(out["ok"] == json!(false) && text(&out, "reason").contains("xhigh"));
        assert_eq!(shown.presses(), [Key::Escape], "closed only after the grace, never answered");
        assert!(start.elapsed() >= WRONG_OFFER_GRACE);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_different_prompt_instead_of_the_dialog_is_not_touched() {
        let w = World::new();
        let shown = w.after_typing(PROMPT);
        let out = effort(&w, "low", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(out["ok"] == json!(false) && text(&out, "reason").contains("different prompt"));
        assert!(shown.presses().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn test_another_job_that_is_mid_turn_is_answered_from_a_thread() {
        let w = World::new();
        w.s().settled = false;
        let shown = w.after_typing(&effort_yes());
        let out = effort(&w, "low", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out["switch_dialog"]["pending"] == json!(true));
        assert!(shown.presses().is_empty() && w.spawned_count() == 1);
        run_spawned(&w).await;
        assert_eq!(shown.presses(), [Key::Enter]);
    }

    /// Write the promise `send` would have left for the caller's own session.
    async fn own_promise(w: &World) -> Change {
        let req = w.sent().pop().unwrap();
        let change = req.change.clone().unwrap();
        let promise = Pending { changes: vec![change.clone()], ..Pending::new(me(), "1", "sid", &req.text, "x", 1) };
        w.promises.record(&promise).await;
        change
    }

    #[tokio::test(start_paused = true)]
    async fn test_the_callers_own_session_is_answered_by_the_spawned_thread() {
        let w = World::new();
        let out = effort(&w, "low", None, me_()).await.unwrap();
        assert!(out["self"] == json!(true) && out["switch_dialog"]["pending"] == json!(true));
        let change = own_promise(&w).await;
        w.set_screen(FakeScreen::new(&[&effort_yes()]));
        assert!(w.screen().presses().is_empty() && w.spawned_count() == 1);
        run_spawned(&w).await;
        assert_eq!(w.screen().presses(), [Key::Enter]);
        let changes = w.promises.read(me()).await.unwrap().changes;
        assert!(changes.len() == 1 && changes[0].key == change.key && changes[0].outcome == APPLIED);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_refused_dialog_is_written_onto_the_promise_with_its_reason() {
        let w = World::new();
        model(&w, "opus", None, me_()).await.unwrap();
        own_promise(&w).await;
        w.set_screen(FakeScreen::new(&[&switch_dialog("Switch model?", "Opus 5.5", 1, "")]));
        run_spawned(&w).await;
        assert_eq!(w.screen().presses(), [Key::Escape]);
        let changes = w.promises.read(me()).await.unwrap().changes;
        assert!(changes.len() == 1 && changes[0].outcome == FAILED && changes[0].detail.contains("No, go back"));
    }

    #[tokio::test(start_paused = true)]
    async fn test_the_thread_stops_waiting_once_the_resume_has_gone_in() {
        let w = World::new();
        effort(&w, "low", None, me_()).await.unwrap();
        let change = own_promise(&w).await;
        let mut gone = PromiseGone::new(me(), &change);
        assert!(!gone.check(&w.promises).await);
        w.promises.clear(me()).await;
        assert!(gone.check(&w.promises).await);
    }

    #[tokio::test]
    async fn test_a_promise_that_was_never_written_does_not_end_the_wait() {
        let w = World::new();
        let mut gone = PromiseGone::new(me(), &Change::new("effort", "low", "k"));
        assert!(!gone.check(&w.promises).await && !gone.check(&w.promises).await);
    }

    #[tokio::test]
    async fn test_a_rename_asks_no_dialog() {
        let w = World::new();
        let out = rename(&w, "new", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out.get("switch_dialog").is_none() && w.screen().presses().is_empty());
    }

    // --- the user's saved defaults ---

    #[tokio::test(start_paused = true)]
    async fn test_effort_puts_the_saved_default_back_after_the_cli_wrote_it() {
        let w = World::new();
        let shown = w.after_typing(&effort_yes());
        shown.on_press(w.cli_saves(save_effort));
        let out = effort(&w, "low", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && shown.presses() == [Key::Enter]);
        assert_eq!(out["switch_dialog"]["defaults_restored"], json!([format!("modelSettings.{MODEL_KEY}.effortLevel")]));
        assert_eq!(w.settings(), settings_text(), "byte for byte, formatting too");
    }

    #[tokio::test(start_paused = true)]
    async fn test_model_puts_the_saved_default_back_after_the_cli_wrote_it() {
        let w = World::new();
        let shown = w.after_typing(&switch_dialog("Switch model?", "Sonnet 5.5", 0, ""));
        shown.on_press(w.cli_saves(|doc| doc["model"] = json!("sonnet")));
        let out = model(&w, "sonnet", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out["switch_dialog"]["defaults_restored"] == json!(["model"]), "{out}");
        let doc: Value = serde_json::from_str(&w.settings()).unwrap();
        assert_eq!(doc["model"], json!("opus"));
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_change_to_other_keys_made_meanwhile_is_kept() {
        let w = World::new();
        let shown = w.after_typing(&effort_yes());
        shown.on_press(w.cli_saves(|doc| {
            save_effort(doc);
            doc["theme"] = json!("light");
            doc["newKey"] = json!({"a": 1});
        }));
        effort(&w, "low", Some(OTHER_JOB), me_()).await.unwrap();
        let doc: Map<String, Value> = serde_json::from_str(&w.settings()).unwrap();
        assert!(doc["theme"] == json!("light") && doc["newKey"] == json!({"a": 1}));
        assert_eq!(doc["modelSettings"][MODEL_KEY]["effortLevel"], json!("high"));
        let mut keys: Vec<String> = settings_doc().as_object().unwrap().keys().cloned().collect();
        keys.push("newKey".into());
        assert_eq!(doc.keys().cloned().collect::<Vec<_>>(), keys, "order kept");
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_dialog_that_was_declined_leaves_the_file_alone() {
        let w = World::new();
        let shown = w.after_typing(&effort_no());
        shown.on_press(w.cli_saves(save_effort));
        let out = effort(&w, "low", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(out["ok"] == json!(false) && shown.presses() == [Key::Escape]);
        assert_eq!(w.settings(), settings_text());
    }

    #[tokio::test]
    async fn test_effort_above_high_is_refused_on_any_model() {
        let w = World::new();
        for level in ["xhigh", "max"] {
            let out = effort(&w, level, Some(OTHER_JOB), me_()).await.unwrap();
            assert!(out["ok"] == json!(false) && text(&out, "reason").contains("upper limit"));
        }
    }

    #[tokio::test]
    async fn test_a_defaults_guard_covers_effort_and_model() {
        let w = World::new();
        assert!(DefaultsGuard::new(Some(Switch::Effort), w.home.settings()).await.on());
        assert!(DefaultsGuard::new(Some(Switch::Model), w.home.settings()).await.on());
        assert!(!DefaultsGuard::new(None, w.home.settings()).await.on());
    }

    #[tokio::test(start_paused = true)]
    async fn test_no_dialog_still_puts_back_what_the_command_saved() {
        // A conversation without output switches without asking, and still saves.
        let w = World::new();
        w.after_typing(PLAIN);
        w.s().save_on_deliver = Some(save_effort);
        let out = effort(&w, "low", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out["switch_dialog"]["dialog"] == json!(false));
        assert_eq!(w.settings(), settings_text());
    }

    #[tokio::test(start_paused = true)]
    async fn test_the_callers_own_session_snapshots_again_just_before_the_answer() {
        let w = World::new();
        model(&w, "sonnet", None, me_()).await.unwrap();
        // The user changes their default while the turn is still running.
        std::fs::write(w.home.settings(), w.settings().replace("\"opus\"", "\"haiku\"")).unwrap();
        let shown = FakeScreen::new(&[&switch_dialog("Switch model?", "Sonnet", 0, "")]);
        shown.on_press(w.cli_saves(|doc| doc["model"] = json!("sonnet")));
        w.set_screen(shown.clone());
        run_spawned(&w).await;
        assert_eq!(shown.presses(), [Key::Enter]);
        let doc: Value = serde_json::from_str(&w.settings()).unwrap();
        assert_eq!(doc["model"], json!("haiku"), "put back to what it was when the dialog opened, not to the stale value");
    }

    // --- what is on screen now ---

    const COMPOSER: &str = "╭────────────╮\n│ >          │\n╰────────────╯\n  ? for shortcuts";
    const ERASE_LINE: &str = "\x1b[2K\x1b[1A";

    /// Ink's way of painting a region: lines, with the cursor left on the last.
    fn inline_frame(text: &str) -> String {
        text.split('\n').collect::<Vec<_>>().join("\r\n")
    }

    /// Erase the `lines`-line region above the cursor, as Ink does before a repaint.
    fn inline_close(lines: usize) -> String {
        ERASE_LINE.repeat(lines - 1) + "\x1b[2K\x1b[G"
    }

    fn line_count(text: &str) -> usize {
        text.matches('\n').count() + 1
    }

    /// The stream of a session whose effort dialog was answered a moment ago.
    fn stale_stream() -> String {
        let dialog = effort_yes();
        inline_frame(COMPOSER)
            + "\r\n"
            + &inline_close(line_count(COMPOSER) + 1)
            + &inline_frame(&dialog)
            + &inline_close(line_count(&dialog))
            + "Set effort level to low (saved as your default for new sessions)\r\n"
            + &inline_frame(COMPOSER)
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_typing_verb_is_not_refused_after_the_dialog_was_answered() {
        let w = World::new();
        w.set_screen(FakeScreen::new(&[&stale_stream()]));
        let out = model(&w, "sonnet", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out.get("refused").is_none());
        assert_eq!(w.typed(), [(OTHER_JOB.to_string(), "/model sonnet".to_string(), sender::ATTEMPTS)]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_the_answering_thread_does_not_press_for_a_dialog_that_is_gone() {
        let w = World::new();
        let shown = w.after_typing(&stale_stream());
        let out = effort(&w, "low", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && shown.presses().is_empty());
    }

    // --- the keys on the wire ---

    #[tokio::test]
    async fn test_a_tmux_press_is_a_send_keys_of_the_named_key() {
        use crate::tmux::testing::{FakePane, PaneState};
        use crate::tmux::Tmux;
        let pane = FakePane::new(PaneState::default());
        let lane = TmuxLane { pane: "%3".into(), session_id: "s".into(), repl_pid: 1, name: None };
        let mut sess = Tmux::with_runner(pane.clone()).session(&lane);
        sess.press(Key::Escape).await.unwrap();
        sess.press(Key::Down).await.unwrap();
        let tails: Vec<Vec<String>> = pane.state().calls.iter().map(|c| c[c.len() - 4..].to_vec()).collect();
        assert_eq!(tails, [["send-keys", "-t", "%3", "Escape"], ["send-keys", "-t", "%3", "Down"]]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_a_daemon_press_sends_the_raw_key_bytes() {
        use crate::lane::tests::{daemon_session, daemon_target, hello};
        use awm_harness::claude::pty::RAW;
        let (mut sess, host) = daemon_session(daemon_target(), hello()).await;
        for key in [Key::Escape, Key::Down, Key::Up, Key::Enter] {
            sess.press(key).await.unwrap();
        }
        sess.close().await;
        let sent: Vec<Vec<u8>> = host.await.unwrap().into_iter().filter(|(k, _)| *k == RAW).map(|(_, p)| p).collect();
        assert_eq!(sent, [b"\x1b".to_vec(), b"\x1b[B".to_vec(), b"\x1b[A".to_vec(), b"\r".to_vec()]);
    }

    // --- cd ---

    /// A trusted target directory, and a record whose cwd follows each `/cd` typed.
    fn moves(w: &World) -> PathBuf {
        let there = w.root.join("elsewhere").join("scope");
        std::fs::create_dir_all(&there).unwrap();
        {
            let mut s = w.s();
            s.trust_all = true;
            s.follow_cd = true;
        }
        w.set_cwd(parent(), &w.root.to_string_lossy());
        there
    }

    fn shown(p: &Path) -> String {
        p.to_string_lossy().into_owned()
    }

    #[tokio::test(start_paused = true)]
    async fn test_cd_types_the_resolved_path_and_sees_the_session_arrive() {
        let w = World::new();
        let there = moves(&w);
        let out = cd(&w, &format!("{}/", shown(&there)), Some("other one"), me_(), "").await.unwrap();
        assert!(ok(&out) && out["moved"] == json!(true) && out["cwd"] == json!(shown(&there)), "{out}");
        assert_eq!(out["mcp_reconnected"], json!(true));
        assert_eq!(
            w.typed(),
            [
                (OTHER_JOB.to_string(), format!("/cd {}", shown(&there)), sender::ATTEMPTS),
                (OTHER_JOB.to_string(), RECONNECT_AFTER_MOVE.to_string(), sender::ATTEMPTS),
            ]
        );
    }

    #[tokio::test(start_paused = true)]
    async fn test_cd_of_another_session_sends_it_the_brief_after_the_reconnect() {
        let w = World::new();
        let there = moves(&w);
        let out = cd(&w, &shown(&there), Some("other one"), me_(), "The brief of p/s: For x.").await.unwrap();
        assert_eq!(out["brief_sent"], json!(true));
        assert_eq!(
            w.typed_texts(),
            [format!("/cd {}", shown(&there)), RECONNECT_AFTER_MOVE.into(), "The brief of p/s: For x.".into()]
        );
    }

    #[tokio::test(start_paused = true)]
    async fn test_cd_follows_a_symlink_to_the_directory_it_names() {
        let w = World::new();
        let there = moves(&w);
        let link = w.root.join("shortcut");
        std::os::unix::fs::symlink(&there, &link).unwrap();
        let out = cd(&w, &shown(&link), Some(OTHER_JOB), me_(), "").await.unwrap();
        assert!(ok(&out) && w.typed()[0].1 == format!("/cd {}", shown(&there)));
    }

    #[tokio::test(start_paused = true)]
    async fn test_cd_keeps_a_path_with_spaces_whole() {
        let w = World::new();
        moves(&w);
        let spaced = w.root.join("two words");
        std::fs::create_dir(&spaced).unwrap();
        let out = cd(&w, &shown(&spaced), Some(OTHER_JOB), me_(), "").await.unwrap();
        assert!(ok(&out) && w.typed()[0].1 == format!("/cd {}", shown(&spaced)));
    }

    #[tokio::test]
    async fn test_cd_refuses_a_path_that_is_not_an_absolute_directory() {
        for path in ["", "relative/dir", "/no/such/dir", "/tmp\n/etc"] {
            let w = World::new();
            moves(&w);
            let out = cd(&w, path, Some(OTHER_JOB), me_(), "").await.unwrap();
            assert_eq!(out["ok"], json!(false));
            assert!(w.typed().is_empty() && w.sent().is_empty() && w.opened() == 0);
        }
    }

    #[tokio::test]
    async fn test_cd_refuses_a_file() {
        let w = World::new();
        moves(&w);
        let file = w.root.join("a-file");
        std::fs::write(&file, "x").unwrap();
        let out = cd(&w, &shown(&file), Some(OTHER_JOB), me_(), "").await.unwrap();
        assert!(out["ok"] == json!(false) && text(&out, "reason").contains("not a directory"));
    }

    #[tokio::test]
    async fn test_cd_refuses_where_the_trust_dialog_would_open() {
        let w = World::new();
        let there = moves(&w);
        w.s().trust_all = false;
        let out = cd(&w, &shown(&there), Some(OTHER_JOB), me_(), "").await.unwrap();
        assert!(out["ok"] == json!(false) && text(&out, "reason").contains("trust dialog"));
        assert!(w.typed().is_empty() && w.opened() == 0);
    }

    #[tokio::test(start_paused = true)]
    async fn test_cd_trusts_an_awm_worktree_before_moving_into_it() {
        // A background session has nobody to answer the dialog, and awm made the worktree, so
        // `cd` records the trust a person would have given.
        let w = World::new();
        let there = moves(&w);
        {
            let mut s = w.s();
            s.trust_all = false;
            s.grant_works = true;
            s.projects_root = w.root.clone();
        }
        let out = cd(&w, &shown(&there), Some(OTHER_JOB), me_(), "").await.unwrap();
        assert_eq!(w.s().grants, [(there.clone(), w.root.clone())]);
        assert!(ok(&out) && w.typed()[0].1 == format!("/cd {}", shown(&there)));
    }

    #[tokio::test]
    async fn test_cd_to_where_the_session_already_is_types_nothing() {
        let w = World::new();
        let there = moves(&w);
        w.set_cwd(parent(), &shown(&there));
        let out = cd(&w, &shown(&there), Some(OTHER_JOB), me_(), "").await.unwrap();
        assert!(ok(&out) && out["changed"] == json!(false) && w.typed().is_empty());
    }

    #[tokio::test]
    async fn test_cd_into_a_modal_is_refused() {
        let w = World::new();
        let there = moves(&w);
        w.set_screen(FakeScreen::new(&[PROMPT]));
        let out = cd(&w, &shown(&there), Some(OTHER_JOB), me_(), "").await.unwrap();
        assert!(out["refused"] == json!(true) && out["kind"] == json!("modal") && w.typed().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn test_cd_mid_turn_reconnects_awm_once_the_move_has_run() {
        let w = World::new();
        let there = moves(&w);
        {
            let mut s = w.s();
            s.settled = false;
            s.follow_cd = false;
        }
        let out = cd(&w, &shown(&there), Some(OTHER_JOB), me_(), "").await.unwrap();
        assert!(ok(&out) && out["moved"] == json!(false) && text(&out, "detail").contains("mid-turn"));
        assert_eq!(w.typed_texts(), [format!("/cd {}", shown(&there))]);
        w.set_cwd(parent(), &shown(&there));
        run_spawned(&w).await;
        assert_eq!(w.typed_texts().last().unwrap(), RECONNECT_AFTER_MOVE);
    }

    #[tokio::test(start_paused = true)]
    async fn test_cd_mid_turn_that_never_lands_reconnects_nothing() {
        let w = World::new();
        let there = moves(&w);
        {
            let mut s = w.s();
            s.settled = false;
            s.follow_cd = false;
        }
        cd(&w, &shown(&there), Some(OTHER_JOB), me_(), "").await.unwrap();
        run_spawned(&w).await;
        assert_eq!(w.typed_texts(), [format!("/cd {}", shown(&there))]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_cd_that_never_lands_says_so_after_the_wait() {
        let w = World::new();
        let there = moves(&w);
        w.s().follow_cd = false;
        let start = Instant::now();
        let out = cd(&w, &shown(&there), Some(OTHER_JOB), me_(), "").await.unwrap();
        assert!(ok(&out) && out["moved"] == json!(false) && text(&out, "detail").contains("list_other"));
        assert!(start.elapsed() >= MOVE_WAIT);
        assert!(out["mcp_reconnected"] == json!(false) && w.typed().len() == 1);
    }

    #[tokio::test]
    async fn test_cd_on_your_own_session_goes_through_the_self_sender() {
        let w = World::new();
        let there = moves(&w);
        let out = cd(&w, &shown(&there), None, me_(), "").await.unwrap();
        assert!(ok(&out) && out["self"] == json!(true) && w.typed().is_empty());
        let req = &w.sent()[0];
        let change = req.change.clone().unwrap();
        assert!(req.text == format!("/cd {}", shown(&there)) && change.kind == "cd");
        assert!(render_followup(&[change], None, "").starts_with(&format!("The move to {} succeeded.", shown(&there))));
    }

    #[tokio::test]
    async fn test_the_move_follow_up_carries_the_brief() {
        let w = World::new();
        let there = moves(&w);
        cd(&w, &shown(&there), None, me_(), "The brief of p/s: For x.").await.unwrap();
        let change = w.sent()[0].change.clone().unwrap();
        assert_eq!(
            render_followup(&[change], None, "go on"),
            format!("The move to {} succeeded. The brief of p/s: For x. go on", shown(&there))
        );
        let failed = Change { note: "The brief".into(), ..Change::new("cd", "/x", "").ended(FAILED, "") };
        assert!(!render_followup(&[failed], None, "").contains("The brief"));
    }

    // --- mcp ---

    #[tokio::test]
    async fn test_message_points_an_mcp_line_at_the_mcp_verb() {
        let w = World::new();
        for line in ["/mcp", "/mcp reconnect awm"] {
            let out = message(&w, Some(OTHER_JOB), line, me_(), false).await.unwrap();
            assert!(out["refused"] == json!(true) && text(&out, "reason").contains("verb=\"mcp\""));
        }
        assert!(w.typed().is_empty());
    }

    #[tokio::test]
    async fn test_mcp_types_the_panel_free_line_and_reads_what_it_printed() {
        let w = World::new();
        w.s().output = Some("Successfully reconnected to awm".into());
        let out = mcp(&w, "reconnect", "awm", Some("other one"), me_()).await.unwrap();
        assert!(ok(&out) && out["output"] == json!("Successfully reconnected to awm"));
        assert_eq!(w.typed(), [(OTHER_JOB.to_string(), "/mcp reconnect awm".to_string(), sender::ATTEMPTS)]);
        assert_eq!(w.s().output_seen, ["/mcp reconnect awm"]);
    }

    #[tokio::test]
    async fn test_mcp_into_a_session_mid_turn_does_not_wait_for_the_output() {
        let w = World::new();
        {
            let mut s = w.s();
            s.settled = false;
            s.output_forbidden = true;
        }
        let out = mcp(&w, "disable", "all", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(ok(&out) && out["output"].is_null() && text(&out, "detail").contains("mid-turn"));
        assert_eq!(w.typed(), [(OTHER_JOB.to_string(), "/mcp disable all".to_string(), sender::ATTEMPTS)]);
    }

    #[tokio::test]
    async fn test_mcp_on_ones_own_session_is_a_self_send_with_a_report() {
        let w = World::new();
        let out = mcp(&w, "enable", "awm", None, me_()).await.unwrap();
        let req = &w.sent()[0];
        assert!(ok(&out) && out["self"] == json!(true) && req.text == "/mcp enable awm");
        assert!(req.allow_modal);
        let change = req.change.clone().unwrap();
        assert_eq!((change.kind.as_str(), change.value.as_str()), ("mcp", "enable awm"));
        assert!(w.typed().is_empty());
    }

    #[tokio::test]
    async fn test_mcp_refuses_a_line_it_cannot_build() {
        for (action, server) in [("list", "awm"), ("", "awm"), ("reconnect", ""), ("reconnect", "a b"), ("reconnect", "awm\n/clear")] {
            let w = World::new();
            let out = mcp(&w, action, server, Some(OTHER_JOB), me_()).await.unwrap();
            assert!(out["ok"] == json!(false) && w.typed().is_empty() && w.sent().is_empty());
        }
    }

    #[tokio::test]
    async fn test_mcp_refuses_a_session_showing_a_modal() {
        let w = World::new();
        w.set_screen(FakeScreen::new(&[PROMPT]));
        let out = mcp(&w, "reconnect", "awm", Some(OTHER_JOB), me_()).await.unwrap();
        assert!(out["refused"] == json!(true) && out["kind"] == json!("modal") && w.typed().is_empty());
    }

    // --- the hand-coded lookahead ---

    #[test]
    fn an_effort_offer_must_end_where_the_level_ends() {
        assert!(offer_names(Switch::Effort, "high", "High"));
        assert!(offer_names(Switch::Effort, "high", "high (recommended)"));
        assert!(!offer_names(Switch::Effort, "high", "higher"));
        assert!(!offer_names(Switch::Effort, "high", "xhigh"));
    }
}
