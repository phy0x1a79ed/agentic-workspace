//! Reports each session's start, stop, rename and Remote Control change to reflection
//! `harness_event` as it happens: inotify on Claude Code's records, one pidfd per live session and
//! `docker events` for isolated sessions. Nothing polls.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::ffi::CString;
use std::future::Future;
use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use awm_harness::claude::home::ClaudeHome;
use awm_harness::claude::records::{read_json, Object};
use awm_harness::claude::roster::bridge_link;
use awm_plugin_sdk::procfs;
use serde_json::{json, Value};
use tokio::io::unix::AsyncFd;
use tokio::io::{AsyncBufReadExt, BufReader, Interest};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio::time::{sleep, sleep_until, Instant};

use crate::gateway::{Call, Gateway};
use crate::isolated;
use crate::lineage;
use crate::paths::Paths;
use crate::resume::r9;

/// How long a session id may be gone before it counts as stopped. The daemon swaps an outdated
/// worker mid-command: the same session comes back under a new pid, and that is neither a stop
/// nor a start.
pub const SWAP_GRACE: Duration = Duration::from_secs(3);
/// The `harness_event` param that marks a reflection taking these events. Python reflection's
/// `harness_event` has none and writes any kind but `clear` into its ledger, so nothing is sent
/// until reflection's describe lists it.
pub const GATE_PARAM: &str = "session_kind";
/// How long one `harness_event` or `describe` call may take.
pub const SEND_TIMEOUT: Duration = Duration::from_secs(5);
/// The longest wait before `docker events` is started again after it ends.
pub const DOCKER_RETRY_MAX: Duration = Duration::from_secs(300);

/// One thing that happened, as an effect source reports it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Change {
    /// `sessions/<pid>.json` was written or appeared.
    Session(u32),
    /// `jobs/<short>/state.json` was written or appeared.
    Job(String),
    /// A pid passed to [`Effects::watch_pid`] has exited.
    Exited(u32),
    /// An isolated session's container started (`up`) or died.
    Container { claude_session_id: String, up: bool },
    /// Events were lost: read every record again.
    Rescan,
}

/// The watcher's effects: its change sources and its pid watches. [`Live`] is the real one.
pub trait Effects: Send {
    /// The next change, or None once every source has ended.
    fn next(&mut self) -> impl Future<Output = Option<Change>> + Send;
    /// Report `pid`'s exit as [`Change::Exited`], if `pid` is the process that started at
    /// `proc_start`. False, holding nothing, when it is not.
    fn watch_pid(&mut self, pid: u32, proc_start: &str) -> bool;
    /// Stop watching `pid` and let go of whatever held it.
    fn unwatch_pid(&mut self, pid: u32);
}

/// What an event says about a session, besides its id.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Facts {
    job: Option<String>,
    name: Option<String>,
    cwd: Option<String>,
    bridge: Option<String>,
    /// The `kind` of the session's `list` row.
    row_kind: &'static str,
}

/// One session record as read from disk.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Record {
    claude_session_id: String,
    proc_start: String,
    facts: Facts,
}

struct Tracked {
    facts: Facts,
    /// The live pids whose record names this session. Two overlap while the daemon swaps.
    pids: BTreeSet<u32>,
    /// When the last pid went: the session stops at `departing + SWAP_GRACE` unless it comes back.
    departing: Option<Instant>,
}

fn text(v: Option<&Value>) -> Option<String> {
    v.and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_string)
}

fn proc_start_of(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        _ => String::new(),
    }
}

/// `pid`'s session record, joined to its job's own record for a background session: the job
/// record is the only one that follows a rename, and the one Remote Control writes for a job.
fn read_record(home: &ClaudeHome, pid: u32) -> Option<Record> {
    let rec = read_json(&home.session_record(pid));
    let claude_session_id = text(rec.get("sessionId"))?;
    if rec.get("pid").is_some_and(|p| p.as_u64() != Some(u64::from(pid))) {
        return None;
    }
    let bg = rec.get("kind").and_then(Value::as_str) == Some("bg");
    let job = text(rec.get("jobId")).filter(|_| bg);
    let facts = match &job {
        Some(short) => {
            let st: Object = read_json(&home.job_state(short));
            Facts {
                name: text(st.get("name")).or_else(|| text(rec.get("name"))),
                cwd: text(rec.get("cwd")).or_else(|| text(st.get("cwd"))),
                bridge: text(st.get("bridgeSessionId")),
                job: job.clone(),
                row_kind: "bg",
            }
        }
        None => Facts {
            job: None,
            name: text(rec.get("name")),
            cwd: text(rec.get("cwd")),
            bridge: text(rec.get("bridgeSessionId")),
            row_kind: "interactive",
        },
    };
    Some(Record { claude_session_id, proc_start: proc_start_of(rec.get("procStart")), facts })
}

/// The pids of the session records under `home`, in order.
fn record_pids(home: &ClaudeHome) -> Vec<u32> {
    let Ok(dir) = std::fs::read_dir(home.sessions()) else { return Vec::new() };
    let mut pids: Vec<u32> = dir.flatten().filter_map(|e| pid_of_file(e.file_name().to_str()?)).collect();
    pids.sort_unstable();
    pids
}

fn pid_of_file(name: &str) -> Option<u32> {
    let stem = name.strip_suffix(".json")?;
    if stem.is_empty() || !stem.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    stem.parse().ok()
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> Option<T> {
    tokio::task::spawn_blocking(f).await.ok()
}

fn event(kind: &str, claude_session_id: &str, f: &Facts, task_id: Option<String>) -> Value {
    let mut out = json!({
        "kind": kind,
        "session_id": r9(claude_session_id),
        "job": f.job,
        "name": f.name,
        "cwd": f.cwd,
        "session_kind": f.row_kind,
    });
    if let Some(task) = task_id {
        out["task_id"] = json!(task);
    }
    let rc_url = bridge_link(f.bridge.as_deref());
    if kind == "remote_control" || rc_url.is_some() {
        out["rc_url"] = json!(rc_url);
    }
    out
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

async fn gate<G: Gateway>(gw: &G) -> bool {
    let args = json!({"verbs": ["harness_event"]});
    match gw.call(Call::new("reflection", "describe", args).timeout(SEND_TIMEOUT)).await {
        Ok(answer) => takes_session_events(&answer),
        Err(e) => {
            tracing::debug!("cch: session events: reflection describe failed: {e}");
            false
        }
    }
}

/// Sends each event in order while the gate is open. A failed send is dropped, never retried.
async fn report<G: Gateway>(gw: Arc<G>, mut events: mpsc::UnboundedReceiver<Value>) {
    let mut open = gate(&*gw).await;
    while let Some(ev) = events.recv().await {
        // A closed gate is read again before each event, else a reflection that gains the param
        // after cch booted would never be sent to.
        if !open {
            open = gate(&*gw).await;
            if !open {
                continue;
            }
        }
        let kind = ev.get("kind").and_then(Value::as_str).unwrap_or_default().to_string();
        let failed = match gw.call(Call::new("reflection", "harness_event", ev).timeout(SEND_TIMEOUT)).await {
            Ok(answer) if answer.get("ok") == Some(&json!(false)) => Some(answer.to_string()),
            Ok(_) => None,
            Err(e) => Some(e),
        };
        if let Some(why) = failed {
            tracing::debug!("cch: session event {kind} dropped: {why}");
            open = gate(&*gw).await;
        }
    }
}

struct Watcher<F> {
    paths: Arc<Paths>,
    fx: F,
    sessions: BTreeMap<String, Tracked>,
    /// Each watched pid: the session its record names, and the process start it was opened for.
    by_pid: HashMap<u32, (String, String)>,
    out: mpsc::UnboundedSender<Value>,
    quiet: bool,
}

impl<F: Effects> Watcher<F> {
    async fn emit(&self, kind: &str, claude_session_id: &str, f: &Facts) {
        if self.quiet {
            return;
        }
        let task_id = match f.job.clone() {
            Some(job) => {
                let paths = self.paths.clone();
                blocking(move || lineage::read_lineage(&paths, &job).and_then(|r| r.task_id)).await.flatten()
            }
            None => None,
        };
        let _ = self.out.send(event(kind, claude_session_id, f, task_id));
    }

    /// Take `pid` off the session it was counted for; the session departs when no pid is left.
    fn detach(&mut self, pid: u32) {
        let Some((sid, _)) = self.by_pid.remove(&pid) else { return };
        if let Some(t) = self.sessions.get_mut(&sid) {
            t.pids.remove(&pid);
            if t.pids.is_empty() {
                t.departing = Some(Instant::now());
            }
        }
    }

    async fn session(&mut self, pid: u32) {
        let home = self.paths.home.clone();
        // A record that is gone or unreadable says nothing: the pidfd reports the exit.
        let Some(rec) = blocking(move || read_record(&home, pid)).await.flatten() else { return };
        let sid = rec.claude_session_id.clone();
        match self.by_pid.get(&pid).cloned() {
            Some((held, start)) if held == sid && start == rec.proc_start => {}
            Some((_, start)) if start == rec.proc_start => {
                // The same process now names another session, as `/clear` does.
                self.detach(pid);
                self.by_pid.insert(pid, (sid.clone(), rec.proc_start.clone()));
            }
            held => {
                if held.is_some() {
                    // The pid was recycled before its exit reached us.
                    self.detach(pid);
                    self.fx.unwatch_pid(pid);
                }
                if !self.fx.watch_pid(pid, &rec.proc_start) {
                    return;
                }
                self.by_pid.insert(pid, (sid.clone(), rec.proc_start.clone()));
            }
        }
        let Some(t) = self.sessions.get_mut(&sid) else {
            let facts = rec.facts.clone();
            self.sessions.insert(sid.clone(), Tracked { facts, pids: BTreeSet::from([pid]), departing: None });
            self.emit("started", &sid, &rec.facts).await;
            return;
        };
        t.pids.insert(pid);
        t.departing = None;
        let before = std::mem::replace(&mut t.facts, rec.facts.clone());
        if before.name != rec.facts.name {
            self.emit("renamed", &sid, &rec.facts).await;
        }
        if before.bridge != rec.facts.bridge {
            self.emit("remote_control", &sid, &rec.facts).await;
        }
    }

    async fn job(&mut self, short: &str) {
        let pids: Vec<u32> = self
            .sessions
            .values()
            .filter(|t| t.facts.job.as_deref() == Some(short))
            .flat_map(|t| t.pids.iter().copied())
            .collect();
        for pid in pids {
            self.session(pid).await;
        }
    }

    fn exited(&mut self, pid: u32) {
        if self.by_pid.contains_key(&pid) {
            self.detach(pid);
            self.fx.unwatch_pid(pid);
        }
    }

    async fn container(&mut self, claude_session_id: String, up: bool) {
        let root = self.paths.isolated.dir.clone();
        let sid = claude_session_id.clone();
        let rec = blocking(move || isolated::find(&root, &sid)).await.flatten();
        let facts = Facts {
            job: None,
            name: rec.as_ref().and_then(|r| r.name.clone()),
            cwd: rec.as_ref().and_then(|r| r.cwd.clone()),
            bridge: None,
            row_kind: "isolated",
        };
        let task_id = rec.as_ref().and_then(|r| text(r.extra.get("task_id")));
        if !self.quiet {
            let _ = self.out.send(event(if up { "started" } else { "stopped" }, &claude_session_id, &facts, task_id));
        }
    }

    async fn rescan(&mut self) {
        let home = self.paths.home.clone();
        for pid in blocking(move || record_pids(&home)).await.unwrap_or_default() {
            self.session(pid).await;
        }
    }

    fn deadline(&self) -> Option<Instant> {
        self.sessions.values().filter_map(|t| t.departing).min().map(|at| at + SWAP_GRACE)
    }

    async fn expire(&mut self) {
        let now = Instant::now();
        let gone: Vec<String> = self
            .sessions
            .iter()
            .filter(|(_, t)| t.departing.is_some_and(|at| at + SWAP_GRACE <= now))
            .map(|(sid, _)| sid.clone())
            .collect();
        for sid in gone {
            if let Some(t) = self.sessions.remove(&sid) {
                self.emit("stopped", &sid, &t.facts).await;
            }
        }
    }

    async fn apply(&mut self, change: Change) {
        match change {
            Change::Session(pid) => self.session(pid).await,
            Change::Job(short) => self.job(&short).await,
            Change::Exited(pid) => self.exited(pid),
            Change::Container { claude_session_id, up } => self.container(claude_session_id, up).await,
            Change::Rescan => self.rescan().await,
        }
    }
}

/// Watch with `fx` and report through `gw` until `fx` runs out of changes. The state on disk at
/// the start is read silently: a restart sends nothing.
pub async fn watch<G: Gateway + 'static, F: Effects>(paths: Arc<Paths>, gw: Arc<G>, fx: F) {
    let (out, events) = mpsc::unbounded_channel();
    let reporter = tokio::spawn(report(gw, events));
    let mut w = Watcher { paths, fx, sessions: BTreeMap::new(), by_pid: HashMap::new(), out, quiet: true };
    w.rescan().await;
    w.quiet = false;
    loop {
        let deadline = w.deadline();
        tokio::select! {
            biased;
            change = w.fx.next() => match change {
                Some(change) => w.apply(change).await,
                None => break,
            },
            () = sleep_until(deadline.unwrap_or_else(Instant::now)), if deadline.is_some() => w.expire().await,
        }
    }
    drop(w);
    let _ = reporter.await;
}

/// Report this node's session events to reflection for as long as cch runs. Never returns.
pub async fn run<G: Gateway + 'static>(paths: Arc<Paths>, gw: Arc<G>) {
    match Live::start(&paths) {
        Ok(fx) => watch(paths, gw, fx).await,
        Err(e) => tracing::warn!("cch: session events are off: {e}"),
    }
    std::future::pending::<()>().await;
}

// --- the live effects ---------------------------------------------------------

/// The real effects: inotify, pidfds and `docker events`, each feeding one channel.
pub struct Live {
    changes: mpsc::UnboundedReceiver<Change>,
    tx: mpsc::UnboundedSender<Change>,
    pids: HashMap<u32, JoinHandle<()>>,
}

impl Live {
    /// Set the inotify watches up now, so nothing written after this is missed, and start
    /// `docker events` when this node's Docker host is a rootless one.
    pub fn start(paths: &Paths) -> io::Result<Self> {
        let (tx, changes) = mpsc::unbounded_channel();
        let notify = Inotify::new(paths.home.clone())?;
        tokio::spawn(notify.run(tx.clone()));
        // SAFETY: getuid cannot fail.
        let uid = unsafe { libc::getuid() };
        match isolated::docker_host(paths.isolated.docker_host.as_deref(), uid) {
            Ok(host) => {
                tokio::spawn(docker_events(host, tx.clone()));
            }
            Err(e) => tracing::info!("cch: no isolated session events: {e}"),
        }
        Ok(Self { changes, tx, pids: HashMap::new() })
    }
}

fn pidfd_open(pid: u32) -> Option<OwnedFd> {
    let pid = libc::pid_t::try_from(pid).ok()?;
    // SAFETY: pidfd_open takes a pid and flags and returns a new fd or -1.
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
    let fd = i32::try_from(fd).ok().filter(|fd| *fd >= 0)?;
    // SAFETY: the kernel just returned this fd and nothing else owns it.
    Some(unsafe { OwnedFd::from_raw_fd(fd) })
}

impl Effects for Live {
    async fn next(&mut self) -> Option<Change> {
        self.changes.recv().await
    }

    fn watch_pid(&mut self, pid: u32, proc_start: &str) -> bool {
        if proc_start.is_empty() {
            return false;
        }
        if let Some(h) = self.pids.remove(&pid) {
            h.abort();
        }
        let Some(fd) = pidfd_open(pid) else { return false };
        // Opened first, checked second: the fd pins whichever process held the pid, so a start
        // time that still matches proves it is the recorded one and not a recycled pid.
        if procfs::start_ticks(pid).as_deref() != Some(proc_start) {
            return false;
        }
        // SAFETY: an OwnedFd keeps its fd open and returns the same fd until it is dropped.
        let Ok(fd) = (unsafe { AsyncFd::register_with_interest(fd, Interest::READABLE) }) else { return false };
        let tx = self.tx.clone();
        let task = tokio::spawn(async move {
            // A pidfd turns readable when its process exits, a `kill -9` included.
            let _ = fd.readable().await;
            let _ = tx.send(Change::Exited(pid));
        });
        self.pids.insert(pid, task);
        true
    }

    fn unwatch_pid(&mut self, pid: u32) {
        if let Some(h) = self.pids.remove(&pid) {
            h.abort();
        }
    }
}

#[derive(Debug, Clone)]
enum Dir {
    Root,
    Sessions,
    Jobs,
    Job(String),
}

const FILE_MASK: u32 = libc::IN_CLOSE_WRITE | libc::IN_MOVED_TO;
const DIR_MASK: u32 = libc::IN_CREATE | libc::IN_MOVED_TO;

/// inotify on the sessions dir, the jobs dir and each job's folder. inotify is not recursive, so
/// each job folder gets its own watch as it appears.
struct Inotify {
    fd: AsyncFd<OwnedFd>,
    dirs: HashMap<i32, Dir>,
    home: ClaudeHome,
}

impl Inotify {
    fn new(home: ClaudeHome) -> io::Result<Self> {
        // SAFETY: inotify_init1 takes flags and returns a new fd or -1.
        let raw = unsafe { libc::inotify_init1(libc::IN_NONBLOCK | libc::IN_CLOEXEC) };
        if raw < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: the kernel just returned this fd and nothing else owns it; an OwnedFd keeps it
        // open and returns the same fd until it is dropped.
        let fd = unsafe { AsyncFd::register_with_interest(OwnedFd::from_raw_fd(raw), Interest::READABLE)? };
        let mut me = Self { fd, dirs: HashMap::new(), home };
        if !me.home.sessions().is_dir() || !me.home.jobs().is_dir() {
            let root = me.home.root.clone();
            me.add(&root, DIR_MASK | libc::IN_ONLYDIR, Dir::Root);
        }
        me.add_sessions();
        me.add_jobs();
        Ok(me)
    }

    fn add(&mut self, path: &Path, mask: u32, dir: Dir) -> bool {
        let Ok(c) = CString::new(path.as_os_str().as_bytes()) else { return false };
        // SAFETY: the fd is ours and `c` is a valid NUL-terminated path.
        let wd = unsafe { libc::inotify_add_watch(self.fd.as_raw_fd(), c.as_ptr(), mask) };
        if wd < 0 {
            tracing::debug!("cch: session events cannot watch {}: {}", path.display(), io::Error::last_os_error());
            return false;
        }
        self.dirs.insert(wd, dir);
        true
    }

    fn add_sessions(&mut self) {
        let dir = self.home.sessions();
        self.add(&dir, FILE_MASK | libc::IN_ONLYDIR, Dir::Sessions);
    }

    fn add_jobs(&mut self) {
        let jobs = self.home.jobs();
        if !self.add(&jobs, DIR_MASK | libc::IN_ONLYDIR, Dir::Jobs) {
            return;
        }
        let Ok(entries) = std::fs::read_dir(&jobs) else { return };
        for e in entries.flatten() {
            if let Some(short) = e.file_name().to_str().filter(|s| lineage::is_job(s)) {
                self.add(&e.path(), FILE_MASK | libc::IN_ONLYDIR, Dir::Job(short.to_string()));
            }
        }
    }

    fn on(&mut self, wd: i32, mask: u32, name: &str) -> Vec<Change> {
        if mask & libc::IN_Q_OVERFLOW != 0 {
            return vec![Change::Rescan];
        }
        if mask & libc::IN_IGNORED != 0 {
            self.dirs.remove(&wd);
            return Vec::new();
        }
        let is_dir = mask & libc::IN_ISDIR != 0;
        match self.dirs.get(&wd).cloned() {
            Some(Dir::Root) if is_dir && name == "sessions" => {
                self.add_sessions();
                vec![Change::Rescan]
            }
            Some(Dir::Root) if is_dir && name == "jobs" => {
                self.add_jobs();
                vec![Change::Rescan]
            }
            Some(Dir::Sessions) => pid_of_file(name).map(Change::Session).into_iter().collect(),
            Some(Dir::Jobs) if is_dir && lineage::is_job(name) => {
                let path = self.home.jobs().join(name);
                self.add(&path, FILE_MASK | libc::IN_ONLYDIR, Dir::Job(name.to_string()));
                vec![Change::Job(name.to_string())]
            }
            Some(Dir::Job(short)) if name == "state.json" => vec![Change::Job(short)],
            _ => Vec::new(),
        }
    }

    async fn run(mut self, tx: mpsc::UnboundedSender<Change>) {
        let mut buf = vec![0u8; 64 * 1024];
        loop {
            let read = {
                let Ok(mut guard) = self.fd.readable().await else { return };
                match guard.try_io(|fd| {
                    // SAFETY: `buf` is valid for `buf.len()` bytes and the fd is ours.
                    let n = unsafe { libc::read(fd.as_raw_fd(), buf.as_mut_ptr().cast(), buf.len()) };
                    if n < 0 { Err(io::Error::last_os_error()) } else { Ok(n as usize) }
                }) {
                    Ok(Ok(n)) => n,
                    Ok(Err(e)) => {
                        tracing::warn!("cch: session events: inotify read failed: {e}");
                        return;
                    }
                    Err(_) => continue,
                }
            };
            for (wd, mask, name) in parse_inotify(&buf[..read]) {
                for change in self.on(wd, mask, &name) {
                    if tx.send(change).is_err() {
                        return;
                    }
                }
            }
        }
    }
}

/// The `(wd, mask, name)` of each `inotify_event` in `buf`.
fn parse_inotify(buf: &[u8]) -> Vec<(i32, u32, String)> {
    const HEAD: usize = std::mem::size_of::<libc::inotify_event>();
    let mut out = Vec::new();
    let mut at = 0;
    while at + HEAD <= buf.len() {
        // SAFETY: HEAD bytes are in bounds; read_unaligned copes with any alignment.
        let ev: libc::inotify_event = unsafe { std::ptr::read_unaligned(buf[at..].as_ptr().cast()) };
        let len = ev.len as usize;
        let end = (at + HEAD + len).min(buf.len());
        let raw = &buf[at + HEAD..end];
        let name = String::from_utf8_lossy(raw.split(|b| *b == 0).next().unwrap_or_default()).into_owned();
        out.push((ev.wd, ev.mask, name));
        at += HEAD + len;
    }
    out
}

/// The change one `docker events --format '{{json .}}'` line reports, if any.
pub fn parse_docker_event(line: &str) -> Option<Change> {
    let v: Value = serde_json::from_str(line).ok()?;
    let action = text(v.get("Action")).or_else(|| text(v.get("status")))?;
    let up = match action.as_str() {
        "start" => true,
        "die" => false,
        _ => return None,
    };
    let claude_session_id = text(v.get("Actor")?.get("Attributes")?.get(isolated::LABEL))?;
    Some(Change::Container { claude_session_id, up })
}

/// Follow `docker events` for cch's containers on `host`, starting it again with a growing wait
/// whenever it ends, as it does while the Docker daemon is down.
async fn docker_events(host: String, tx: mpsc::UnboundedSender<Change>) {
    let mut wait = Duration::from_secs(1);
    loop {
        match follow_docker(&host, &tx).await {
            Ok(true) => wait = Duration::from_secs(1),
            Ok(false) => {}
            Err(e) => tracing::debug!("cch: docker events: {e}"),
        }
        if tx.is_closed() {
            return;
        }
        sleep(wait).await;
        wait = (wait * 2).min(DOCKER_RETRY_MAX);
    }
}

/// One run of `docker events`; true when it reported anything.
async fn follow_docker(host: &str, tx: &mpsc::UnboundedSender<Change>) -> io::Result<bool> {
    let label = format!("label={}", isolated::LABEL);
    let mut child = tokio::process::Command::new("docker")
        .args(["events", "--filter", "type=container", "--filter", &label])
        .args(["--filter", "event=start", "--filter", "event=die", "--format", "{{json .}}"])
        .env_remove("SSH_AUTH_SOCK")
        .env("DOCKER_HOST", host)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true)
        .spawn()?;
    let stdout = child.stdout.take().ok_or_else(|| io::Error::other("docker events has no stdout"))?;
    let mut lines = BufReader::new(stdout).lines();
    let mut any = false;
    while let Some(line) = lines.next_line().await? {
        if let Some(change) = parse_docker_event(&line) {
            any = true;
            if tx.send(change).is_err() {
                break;
            }
        }
    }
    let _ = child.kill().await;
    Ok(any)
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;
    use std::sync::Mutex;

    use tempfile::TempDir;

    use super::*;
    use crate::gateway::testing::FakeGateway;

    const SID_A: &str = "00000000-0000-4000-8000-000000000001";
    const SID_B: &str = "00000000-0000-4000-8000-000000000002";
    const JOB: &str = "a1b2c3d4";
    const TASK: &str = "task-0001";

    /// Changes the test pushes, and the pids the test says are alive.
    struct FakeFx {
        changes: mpsc::UnboundedReceiver<Change>,
        alive: Arc<Mutex<HashSet<u32>>>,
        watched: Arc<Mutex<HashSet<u32>>>,
        // Fired on the first `next`, which the watcher reaches only once its boot read is done.
        booted: Option<tokio::sync::oneshot::Sender<()>>,
    }

    impl Effects for FakeFx {
        async fn next(&mut self) -> Option<Change> {
            if let Some(booted) = self.booted.take() {
                let _ = booted.send(());
            }
            self.changes.recv().await
        }
        fn watch_pid(&mut self, pid: u32, proc_start: &str) -> bool {
            let ok = !proc_start.is_empty() && self.alive.lock().unwrap().contains(&pid);
            if ok {
                self.watched.lock().unwrap().insert(pid);
            }
            ok
        }
        fn unwatch_pid(&mut self, pid: u32) {
            self.watched.lock().unwrap().remove(&pid);
        }
    }

    struct World {
        _tmp: TempDir,
        paths: Arc<Paths>,
        gw: Arc<FakeGateway>,
        tx: Option<mpsc::UnboundedSender<Change>>,
        alive: Arc<Mutex<HashSet<u32>>>,
        watched: Arc<Mutex<HashSet<u32>>>,
        task: Option<JoinHandle<()>>,
    }

    impl World {
        fn new() -> Self {
            let tmp = TempDir::new().unwrap();
            let paths = Paths::under(tmp.path());
            for dir in [paths.home.sessions(), paths.home.jobs(), paths.starts_dir(), paths.isolated.dir.clone()] {
                std::fs::create_dir_all(dir).unwrap();
            }
            Self {
                _tmp: tmp,
                paths: Arc::new(paths),
                gw: Arc::new(FakeGateway::default()),
                tx: None,
                alive: Arc::default(),
                watched: Arc::default(),
                task: None,
            }
        }

        /// A world whose reflection takes session events.
        fn open() -> Self {
            let w = Self::new();
            w.gw.answer("reflection", "describe", Ok(describe_with(&["session_id", "kind", GATE_PARAM])));
            w
        }

        fn interactive(&self, pid: u32, sid: &str, name: &str, bridge: Option<&str>) {
            self.alive.lock().unwrap().insert(pid);
            let mut rec = json!({"pid": pid, "sessionId": sid, "cwd": "/work/alpha", "kind": "interactive",
                                 "name": name, "procStart": format!("{pid}000")});
            if let Some(b) = bridge {
                rec["bridgeSessionId"] = json!(b);
            }
            std::fs::write(self.paths.home.session_record(pid), rec.to_string()).unwrap();
        }

        fn background(&self, pid: u32, sid: &str, job: &str) {
            self.alive.lock().unwrap().insert(pid);
            let rec = json!({"pid": pid, "sessionId": sid, "cwd": "/work/beta", "kind": "bg", "jobId": job,
                             "name": "seed name", "procStart": format!("{pid}000")});
            std::fs::write(self.paths.home.session_record(pid), rec.to_string()).unwrap();
        }

        fn job_state(&self, job: &str, name: &str, bridge: Option<&str>) {
            std::fs::create_dir_all(self.paths.home.jobs().join(job)).unwrap();
            let mut st = json!({"name": name, "cwd": "/work/beta"});
            if let Some(b) = bridge {
                st["bridgeSessionId"] = json!(b);
            }
            std::fs::write(self.paths.home.job_state(job), st.to_string()).unwrap();
        }

        fn kill(&self, pid: u32) {
            self.alive.lock().unwrap().remove(&pid);
            let _ = std::fs::remove_file(self.paths.home.session_record(pid));
            self.push(Change::Exited(pid));
        }

        /// Start the watcher and wait until it has read the state on disk.
        async fn start(&mut self) {
            let (tx, changes) = mpsc::unbounded_channel();
            let (booted, boot_done) = tokio::sync::oneshot::channel();
            let fx = FakeFx { changes, alive: self.alive.clone(), watched: self.watched.clone(), booted: Some(booted) };
            self.tx = Some(tx);
            self.task = Some(tokio::spawn(watch(self.paths.clone(), self.gw.clone(), fx)));
            boot_done.await.unwrap();
        }

        fn push(&self, change: Change) {
            self.tx.as_ref().expect("started").send(change).unwrap();
        }

        /// End the watcher once every pushed change is handled, and return what reached reflection.
        async fn finish(&mut self) -> Vec<Value> {
            self.tx = None;
            self.task.take().unwrap().await.unwrap();
            self.sent()
        }

        fn sent(&self) -> Vec<Value> {
            let calls = self.gw.calls.lock().unwrap();
            calls.iter().filter(|c| c.verb == "harness_event").map(|c| c.args.clone()).collect()
        }
    }

    /// A reflection describe answer whose `harness_event` takes `params`.
    fn describe_with(params: &[&str]) -> Value {
        let params: Vec<Value> = params.iter().map(|p| json!({"name": p, "required": false})).collect();
        json!({"tool": "reflection", "plugin": "reflection", "verbs": [
            {"verb": "harness_event", "description": "A provider reports an event.", "params": params},
        ]})
    }

    fn kinds(events: &[Value]) -> Vec<&str> {
        events.iter().map(|e| e["kind"].as_str().unwrap()).collect()
    }

    #[tokio::test(start_paused = true)]
    async fn the_state_on_disk_at_boot_sends_nothing() {
        let mut w = World::open();
        w.interactive(101, SID_A, "first", Some("cse_01Boot"));
        w.background(102, SID_B, JOB);
        w.job_state(JOB, "worker", None);
        w.start().await;
        sleep(Duration::from_secs(10)).await;
        assert!(w.finish().await.is_empty());
        assert_eq!(*w.watched.lock().unwrap(), HashSet::from([101, 102]), "a pidfd per live session");
    }

    #[tokio::test(start_paused = true)]
    async fn a_new_session_file_is_started() {
        let mut w = World::open();
        w.start().await;
        w.interactive(101, SID_A, "first", None);
        w.push(Change::Session(101));
        let sent = w.finish().await;
        assert_eq!(kinds(&sent), ["started"]);
        let ev = &sent[0];
        assert_eq!(ev["session_id"], json!(r9(SID_A)));
        assert!(ev["name"] == json!("first") && ev["cwd"] == json!("/work/alpha") && ev["job"].is_null());
        assert_eq!(ev["session_kind"], json!("interactive"));
        assert!(ev.get("rc_url").is_none() && ev.get("task_id").is_none());
    }

    #[tokio::test(start_paused = true)]
    async fn a_background_start_carries_its_job_name_and_task_from_the_lineage_record() {
        let mut w = World::open();
        w.start().await;
        w.job_state(JOB, "worker", Some("cse_01Job"));
        std::fs::write(w.paths.lineage_path(JOB), json!({"job": JOB, "task_id": TASK}).to_string()).unwrap();
        w.background(102, SID_B, JOB);
        w.push(Change::Session(102));
        let sent = w.finish().await;
        assert_eq!(kinds(&sent), ["started"]);
        let ev = &sent[0];
        assert!(ev["job"] == json!(JOB) && ev["name"] == json!("worker") && ev["session_kind"] == json!("bg"), "{ev}");
        assert_eq!(ev["task_id"], json!(TASK));
        assert_eq!(ev["rc_url"], json!("https://claude.ai/code/session_01Job"));
    }

    #[tokio::test(start_paused = true)]
    async fn a_worker_swap_inside_the_grace_window_is_neither_stopped_nor_started() {
        let mut w = World::open();
        w.background(102, SID_B, JOB);
        w.job_state(JOB, "worker", None);
        w.start().await;
        w.kill(102);
        sleep(Duration::from_secs(1)).await;
        w.background(202, SID_B, JOB);
        w.push(Change::Session(202));
        sleep(SWAP_GRACE * 2).await;
        assert!(w.finish().await.is_empty());
        assert_eq!(*w.watched.lock().unwrap(), HashSet::from([202]));
    }

    #[tokio::test(start_paused = true)]
    async fn a_clear_stops_the_old_id_and_starts_the_new_one_on_the_same_pid() {
        let mut w = World::open();
        w.interactive(101, SID_A, "first", None);
        w.start().await;
        w.interactive(101, SID_B, "first", None);
        w.push(Change::Session(101));
        sleep(SWAP_GRACE * 2).await;
        let sent = w.finish().await;
        assert_eq!(kinds(&sent), ["started", "stopped"]);
        assert!(sent[0]["session_id"] == json!(r9(SID_B)) && sent[1]["session_id"] == json!(r9(SID_A)));
        assert_eq!(*w.watched.lock().unwrap(), HashSet::from([101]));
    }

    #[test]
    fn inotify_records_parse_with_their_names() {
        let mut buf = Vec::new();
        for (wd, mask, name) in [(1i32, libc::IN_CLOSE_WRITE, "123.json"), (2, libc::IN_MOVED_TO, "state.json")] {
            let padded = 16usize;
            buf.extend_from_slice(&wd.to_ne_bytes());
            buf.extend_from_slice(&mask.to_ne_bytes());
            buf.extend_from_slice(&0u32.to_ne_bytes());
            buf.extend_from_slice(&(padded as u32).to_ne_bytes());
            let mut n = name.as_bytes().to_vec();
            n.resize(padded, 0);
            buf.extend_from_slice(&n);
        }
        let got = parse_inotify(&buf);
        assert_eq!(got, [(1, libc::IN_CLOSE_WRITE, "123.json".to_string()), (2, libc::IN_MOVED_TO, "state.json".to_string())]);
        assert_eq!(pid_of_file("123.json"), Some(123));
        assert_eq!(pid_of_file("x.json"), None);
    }

    #[tokio::test]
    async fn a_pidfd_reports_the_exit_of_a_process_it_was_opened_for() {
        let mut child = tokio::process::Command::new("sleep").arg("30").kill_on_drop(true).spawn().unwrap();
        let pid = child.id().unwrap();
        let start = procfs::start_ticks(pid).unwrap();
        let (tx, changes) = mpsc::unbounded_channel();
        let mut live = Live { changes, tx, pids: HashMap::new() };
        assert!(!live.watch_pid(pid, "1"), "a start time that is not the process's own holds nothing");
        assert!(live.pids.is_empty());
        assert!(live.watch_pid(pid, &start));
        child.kill().await.unwrap();
        let _ = child.wait().await;
        assert_eq!(live.next().await, Some(Change::Exited(pid)));
    }
}
