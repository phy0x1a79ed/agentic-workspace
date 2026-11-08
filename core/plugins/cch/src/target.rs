//! Resolve a caller's REPL pid to the one lane it lives in, or refuse.
//!
//! The pid is one the gateway observed, never one the model supplied. There is no fallback that
//! picks a plausible session: guessing here means typing into a stranger's prompt.

use std::collections::HashMap;
use std::future::Future;
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;

use awm_harness::claude::pty::DaemonLane;
use awm_harness::claude::records::{self, Object, SessionRecordError};
use awm_harness::claude::ClaudeHome;
use awm_plugin_sdk::procfs;
use serde_json::Value;

use crate::lane::{Host, LaneError};
use crate::tmux::Runner;

/// The caller could not be identified, so there is nothing safe to target.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ResolveError(pub String);

/// An interactive session living in a tmux pane.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TmuxLane {
    pub pane: String,
    pub session_id: String,
    pub repl_pid: u32,
    pub name: Option<String>,
}

/// Where a session lives. The caller chooses neither: which one comes back is a fact about how the
/// session is hosted right now.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Target {
    Tmux(TmuxLane),
    Daemon(DaemonLane),
}

impl Target {
    /// The transport, for logs and dispatch: `tmux` or `daemon`.
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Tmux(_) => "tmux",
            Self::Daemon(l) => l.kind(),
        }
    }

    /// The word the API uses for the transport: `tmux` or `background`.
    pub fn hosting(&self) -> &'static str {
        match self {
            Self::Tmux(_) => "tmux",
            Self::Daemon(l) => l.hosting(),
        }
    }

    pub fn session_id(&self) -> &str {
        match self {
            Self::Tmux(l) => &l.session_id,
            Self::Daemon(l) => &l.session_id,
        }
    }

    pub fn repl_pid(&self) -> u32 {
        match self {
            Self::Tmux(l) => l.repl_pid,
            Self::Daemon(l) => l.repl_pid,
        }
    }

    pub fn name(&self) -> Option<&str> {
        match self {
            Self::Tmux(l) => l.name.as_deref(),
            Self::Daemon(l) => l.name.as_deref(),
        }
    }

    /// The session's name, else its pane (tmux) or session id (daemon).
    pub fn label(&self) -> String {
        let fallback = match self {
            Self::Tmux(l) => &l.pane,
            Self::Daemon(l) => &l.session_id,
        };
        self.name().filter(|n| !n.is_empty()).unwrap_or(fallback).to_string()
    }
}

/// What resolution reads of other processes. The live one is `/proc`; tests pass a fake.
pub trait ProcTable: Clone + Send + Sync + 'static {
    /// The process's start time in clock ticks, or None when it is gone.
    fn start_ticks(&self, pid: u32) -> impl Future<Output = Option<String>> + Send;
    /// Every process's children, keyed by parent pid.
    fn children(&self) -> impl Future<Output = HashMap<u32, Vec<u32>>> + Send;
    /// The command line, arguments joined by spaces, empty when unreadable.
    fn cmdline(&self, pid: u32) -> impl Future<Output = String> + Send;
    /// The (cols, rows) of the terminal the process reads from (see [`tty_size`]).
    fn tty_size(&self, pid: u32) -> impl Future<Output = Option<(u16, u16)>> + Send;
}

/// The live process table.
#[derive(Debug, Clone, Copy, Default)]
pub struct LiveProcs;

async fn blocking<T: Default + Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    tokio::task::spawn_blocking(f).await.unwrap_or_default()
}

impl ProcTable for LiveProcs {
    async fn start_ticks(&self, pid: u32) -> Option<String> {
        blocking(move || procfs::start_ticks(pid)).await
    }

    async fn children(&self) -> HashMap<u32, Vec<u32>> {
        blocking(procfs::children).await
    }

    async fn cmdline(&self, pid: u32) -> String {
        blocking(move || procfs::cmdline(pid).map(|a| a.join(" ")).unwrap_or_default()).await
    }

    async fn tty_size(&self, pid: u32) -> Option<(u16, u16)> {
        blocking(move || tty_size(pid)).await
    }
}

/// Resolves a caller's REPL pid to its [`Target`].
pub trait Resolve: Send + Sync {
    /// The one lane `repl_pid` lives in, or a refusal saying why there is none to act on.
    ///
    /// `expect_session` is the session the caller believes it is, and it can only refuse: the
    /// pid still decides, and a mismatch errors rather than redirects. It guards callers whose
    /// pid comes from an ancestry walk (a hook), which a missing record would let climb past its
    /// own session to whoever launched it.
    fn resolve(&self, repl_pid: u32, expect_session: Option<&str>)
        -> impl Future<Output = Result<Target, LaneError>> + Send;
}

/// Claude Code's session record for `repl_pid`, refusing one that is missing, unreadable, or
/// describes a process that has since handed the pid on.
pub async fn read_session_record(home: &ClaudeHome, repl_pid: u32) -> Result<Object, ResolveError> {
    let home = home.clone();
    let read = tokio::task::spawn_blocking(move || records::read_session_record(&home, repl_pid))
        .await
        .map_err(|e| ResolveError(format!("could not read session record for pid {repl_pid}: {e}")))?;
    read.map_err(|e| {
        ResolveError(match e {
            SessionRecordError::Missing => format!(
                "no Claude Code session record for pid {repl_pid}; the caller does not look like a Claude \
                 Code session, so there is nothing to inject into"
            ),
            SessionRecordError::Unreadable(u) => format!("could not read session record for pid {repl_pid}: {}", u.why),
            SessionRecordError::Gone => format!("calling process {repl_pid} is gone"),
            SessionRecordError::Stale { claimed, live } => format!(
                "session record for pid {repl_pid} is stale (it describes a process started at {claimed}, but \
                 pid {repl_pid} started at {live} — the pid was recycled); refusing to inject"
            ),
        })
    })
}

/// `(pid, procStart)` of the live process other than `old_pid` that now holds `session_id`: the
/// daemon swaps an outdated background worker for a new process in place, same job, same session.
pub async fn successor(home: &ClaudeHome, session_id: &str, old_pid: Option<u32>) -> Option<(u32, String)> {
    let (home, session_id) = (home.clone(), session_id.to_string());
    blocking(move || records::holder_of(&home, &session_id, old_pid)).await
}

/// The (cols, rows) of the terminal `pid` reads from, or None. A job claimed from the daemon's
/// warm spares records no size at dispatch, and without one a walk cannot ask it to repaint.
pub fn tty_size(pid: u32) -> Option<(u16, u16)> {
    let fd = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOCTTY | libc::O_NONBLOCK)
        .open(format!("/proc/{pid}/fd/0"))
        .ok()?;
    // SAFETY: TIOCGWINSZ writes one winsize into `ws`, which outlives the call.
    let mut ws: libc::winsize = unsafe { std::mem::zeroed() };
    if unsafe { libc::ioctl(fd.as_raw_fd(), libc::TIOCGWINSZ, &mut ws) } < 0 {
        return None;
    }
    (ws.ws_col > 0 && ws.ws_row > 1).then_some((ws.ws_col, ws.ws_row))
}

fn text(record: &Object, key: &str) -> String {
    record.get(key).and_then(Value::as_str).unwrap_or_default().to_string()
}

fn or_unset(s: &str) -> &str {
    if s.is_empty() {
        "<unset>"
    } else {
        s
    }
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

fn py_repr(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => format!("'{s}'"),
        Some(Value::Bool(true)) => "True".into(),
        Some(Value::Bool(false)) => "False".into(),
        Some(v) => v.to_string(),
    }
}

fn py_str(v: Option<&Value>) -> String {
    match v {
        Some(Value::String(s)) => s.clone(),
        other => py_repr(other),
    }
}

fn as_pid(v: Option<&Value>) -> Option<u32> {
    v?.as_u64().and_then(|n| u32::try_from(n).ok())
}

impl<R: Runner, P: ProcTable> Host<R, P> {
    // The roster entry hosting the background session `record`. Keyed on `jobId`, fixed for the
    // job's life; `sessionId` is not stable (clearing a conversation rewrites it while the roster
    // keeps the dispatch id), so it is only a fallback for records older than `jobId`. Whatever
    // matched is checked against ancestry: the roster's pid is the bg-pty-host, so the caller must
    // sit in its subtree, and a live host that does not contain it belongs to somebody else.
    async fn roster_worker(&self, record: &Object, repl_pid: u32) -> Result<Object, ResolveError> {
        let path = self.home.roster();
        let roster: Value = match tokio::fs::read(&path).await {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .map_err(|e| ResolveError(format!("could not read the daemon roster: {e}")))?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Err(ResolveError(
                    "the Claude Code daemon roster does not exist, so this background session's PTY cannot be \
                     located"
                        .into(),
                ))
            }
            Err(e) => return Err(ResolveError(format!("could not read the daemon roster: {e}"))),
        };
        let workers: Vec<(&String, &Object)> = roster
            .get("workers")
            .and_then(Value::as_object)
            .map(|w| w.iter().filter_map(|(k, v)| Some((k, v.as_object()?))).collect())
            .unwrap_or_default();
        let job_id = py_truthy_text(record, "jobId");
        let session_id = py_truthy_text(record, "sessionId");
        let mut kids: Option<HashMap<u32, Vec<u32>>> = None;

        let mut worker = (!job_id.is_empty())
            .then(|| workers.iter().find(|(k, _)| **k == job_id).map(|(_, w)| *w))
            .flatten();
        if worker.is_none() && !session_id.is_empty() {
            worker = workers
                .iter()
                .find(|(_, w)| w.get("sessionId").and_then(Value::as_str) == Some(&session_id))
                .map(|(_, w)| *w);
        }
        if worker.is_none() {
            // Last, because the daemon supervisor is an ancestor of every background session: this
            // asks "which host contains me", not "which of my ancestors is listed".
            let k = kids.insert(self.procs.children().await);
            worker = workers
                .iter()
                .find(|(_, w)| as_pid(w.get("pid")).is_some_and(|host| procfs::subtree_contains(host, repl_pid, k)))
                .map(|(_, w)| *w);
        }
        let Some(worker) = worker else {
            return Err(ResolveError(format!(
                "no daemon roster entry for job {} (session {}), and no listed PTY host contains pid {repl_pid}; \
                 the Claude Code daemon does not list this session",
                or_unset(&job_id),
                or_unset(&session_id)
            )));
        };

        if let Some(host) = as_pid(worker.get("pid")) {
            if self.procs.start_ticks(host).await.is_some() {
                let kids = match kids {
                    Some(k) => k,
                    None => self.procs.children().await,
                };
                if !procfs::subtree_contains(host, repl_pid, &kids) {
                    return Err(ResolveError(format!(
                        "the daemon roster entry for job {} names PTY host {host}, which is running but does not \
                         contain pid {repl_pid}; that entry describes a different session, so reflection will not \
                         inject into it",
                        or_unset(&job_id)
                    )));
                }
            }
        }
        Ok(worker.clone())
    }

    async fn daemon_lane(&self, record: &Object, repl_pid: u32, session_id: &str, name: Option<String>) -> Result<DaemonLane, ResolveError> {
        let worker = self.roster_worker(record, repl_pid).await?;
        let sock = text(&worker, "ptySock");
        let auth = text(&worker, "ptyAuth");
        if sock.is_empty() || auth.is_empty() {
            return Err(ResolveError(format!(
                "the daemon roster entry for session {session_id} has no PTY socket or input token; reflection \
                 cannot reach it"
            )));
        }
        let modes = worker
            .get("decModes")
            .and_then(Value::as_array)
            .map(|m| m.iter().filter_map(|v| v.as_u64().and_then(|n| u32::try_from(n).ok())).collect())
            .unwrap_or_default();
        let dispatch = worker.get("dispatch");
        let dim = |key: &str| dispatch.and_then(|d| d.get(key)).and_then(Value::as_u64).and_then(|n| u16::try_from(n).ok());
        let geometry = match (dim("cols"), dim("rows")) {
            (Some(cols), Some(rows)) if cols > 0 && rows > 1 => Some((cols, rows)),
            _ => self.procs.tty_size(repl_pid).await,
        };
        let cli_version = worker.get("cliVersion").and_then(Value::as_str).map(str::to_string);
        Ok(DaemonLane::from_parts(sock, auth, session_id, repl_pid, modes)
            .with_name(name)
            .with_cli_version(cli_version)
            .with_geometry(geometry))
    }
}

fn py_truthy_text(record: &Object, key: &str) -> String {
    let v = record.get(key);
    if truthy(v) {
        py_str(v)
    } else {
        String::new()
    }
}

impl<R: Runner, P: ProcTable> Resolve for Host<R, P> {
    async fn resolve(&self, repl_pid: u32, expect_session: Option<&str>) -> Result<Target, LaneError> {
        if repl_pid == 0 {
            return Err(ResolveError(format!("invalid caller pid {repl_pid}")).into());
        }
        let record = read_session_record(&self.home, repl_pid).await?;
        let session_id = py_truthy_text(&record, "sessionId");
        let name = record.get("name").and_then(Value::as_str).map(str::to_string);
        let who = name.clone().filter(|n| !n.is_empty()).unwrap_or_else(|| session_id.clone());

        if let Some(expected) = expect_session.filter(|e| !e.is_empty()) {
            if session_id != expected {
                return Err(ResolveError(format!(
                    "pid {repl_pid} resolves to session {}, but the caller expected session {expected}; that pid \
                     belongs to a different conversation, so reflection will not act on it",
                    or_unset(&session_id)
                ))
                .into());
            }
        }

        // A parked launcher used to hold a conversation and handed it to a background job. Its pane
        // now shows the fleet view, whose new-session box accepts a paste nobody asked for. Refused
        // by name rather than followed: the job has its own REPL, and reflection acts on the caller.
        if truthy(record.get("parkedJobId")) {
            let parked = py_str(record.get("parkedJobId"));
            return Err(ResolveError(format!(
                "session {who} (pid {repl_pid}) has parked its conversation as background job {parked}; that \
                 terminal is now showing the fleet view, and typing into it would land in the new-session box \
                 rather than in any conversation. Reflection acts only on the caller, so call it from the parked \
                 job itself"
            ))
            .into());
        }

        match record.get("kind").and_then(Value::as_str) {
            Some("interactive") => {
                let Some(pane) = self.tmux.pane_for_pid(repl_pid, &self.procs).await? else {
                    return Err(ResolveError(format!(
                        "session {who} (pid {repl_pid}) reports itself as an interactive terminal session, but no \
                         tmux pane on this server contains it; reflection can only reach terminal sessions that run \
                         under tmux"
                    ))
                    .into());
                };
                Ok(Target::Tmux(TmuxLane { pane, session_id, repl_pid, name }))
            }
            Some("bg") => Ok(Target::Daemon(self.daemon_lane(&record, repl_pid, &session_id, name).await?)),
            _ => Err(ResolveError(format!(
                "session {who} (pid {repl_pid}) has an unrecognised hosting kind {}; reflection knows how to reach \
                 interactive tmux sessions and background daemon sessions",
                py_repr(record.get("kind"))
            ))
            .into()),
        }
    }
}

/// A process table held in memory, for tests.
pub mod testing {
    use std::collections::{HashMap, HashSet};

    use super::ProcTable;

    /// Every pid is alive with start ticks `111` unless listed in `dead`.
    #[derive(Debug, Clone)]
    pub struct FakeProcs {
        pub children: HashMap<u32, Vec<u32>>,
        pub dead: HashSet<u32>,
        /// Whether every command line reads as claude.
        pub agent: bool,
        pub tty: Option<(u16, u16)>,
    }

    impl FakeProcs {
        /// No children, nobody dead, every process an agent, no terminal.
        pub fn new() -> Self {
            Self { children: HashMap::new(), dead: HashSet::new(), agent: true, tty: None }
        }
    }

    impl Default for FakeProcs {
        fn default() -> Self {
            Self::new()
        }
    }

    impl ProcTable for FakeProcs {
        async fn start_ticks(&self, pid: u32) -> Option<String> {
            (!self.dead.contains(&pid)).then(|| "111".to_string())
        }

        async fn children(&self) -> HashMap<u32, Vec<u32>> {
            self.children.clone()
        }

        async fn cmdline(&self, _pid: u32) -> String {
            if self.agent { "claude" } else { "bash" }.into()
        }

        async fn tty_size(&self, _pid: u32) -> Option<(u16, u16)> {
            self.tty
        }
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::testing::FakeProcs;
    use super::*;
    use crate::tmux::testing::{FakePane, PaneState};
    use crate::tmux::Tmux;
    use serde_json::json;

    // The record's pid-reuse check reads the real /proc, so records name this test process.
    fn me() -> u32 {
        std::process::id()
    }

    fn my_start() -> String {
        procfs::start_ticks(me()).unwrap()
    }

    // pid_max is at most 2^22, so this pid never exists.
    const NO_SUCH_PID: u32 = (1 << 22) + 1;

    struct Fixture {
        _dir: tempfile::TempDir,
        host: Host<FakePane, FakeProcs>,
    }

    fn fixture(procs: FakeProcs, panes: Vec<[String; 4]>) -> Fixture {
        let dir = tempfile::tempdir().unwrap();
        let home = ClaudeHome::at(dir.path().join(".claude"), dir.path().join(".claude.json"));
        std::fs::create_dir_all(home.sessions()).unwrap();
        std::fs::create_dir_all(home.daemon_dir()).unwrap();
        let pane = FakePane::new(PaneState { list_panes: panes, ..PaneState::default() });
        Fixture { _dir: dir, host: Host { home, tmux: Tmux::with_runner(pane), procs } }
    }

    fn plain() -> Fixture {
        fixture(FakeProcs::new(), Vec::new())
    }

    // A pane whose process is this test process, so `pane_for_pid(me())` answers `%3`.
    fn with_my_pane() -> Fixture {
        fixture(FakeProcs::new(), vec![["%3".into(), me().to_string(), "claude".into(), "s".into()]])
    }

    fn write_session(home: &ClaudeHome, pid: u32, fields: Value) {
        let mut record = json!({"pid": pid, "sessionId": format!("sid-{pid}"), "kind": "interactive",
                                "name": format!("s{pid}")});
        for (k, v) in fields.as_object().unwrap() {
            record[k] = v.clone();
        }
        std::fs::write(home.session_record(pid), record.to_string()).unwrap();
    }

    fn write_roster(home: &ClaudeHome, workers: Value) {
        std::fs::write(home.roster(), json!({"workers": workers}).to_string()).unwrap();
    }

    async fn resolve_err(b: &Fixture, pid: u32, expect: Option<&str>) -> String {
        b.host.resolve(pid, expect).await.unwrap_err().to_string()
    }

    async fn daemon(b: &Fixture, pid: u32) -> DaemonLane {
        match b.host.resolve(pid, None).await.unwrap() {
            Target::Daemon(lane) => lane,
            other => panic!("expected a daemon lane, got {other:?}"),
        }
    }

    // The roster's pid is the bg-pty-host and the REPL is its child: host 49641 holds this process.
    fn bg_box(children: Option<HashMap<u32, Vec<u32>>>, dead: &[u32]) -> Fixture {
        let procs = FakeProcs {
            children: children.unwrap_or_else(|| HashMap::from([(49641, vec![me()])])),
            dead: dead.iter().copied().collect(),
            ..FakeProcs::new()
        };
        fixture(procs, Vec::new())
    }

    #[tokio::test]
    async fn test_missing_session_record_refuses() {
        let b = plain();
        assert!(resolve_err(&b, 1234, None).await.contains("no Claude Code session"));
    }

    #[tokio::test]
    async fn test_recycled_pid_refuses() {
        // Records are keyed by pid and pids get reused: a mismatched procStart is somebody else.
        let b = plain();
        write_session(&b.host.home, me(), json!({"procStart": "1"}));
        assert!(resolve_err(&b, me(), None).await.contains("stale"));
    }

    #[tokio::test]
    async fn test_dead_pid_refuses() {
        let b = plain();
        write_session(&b.host.home, NO_SUCH_PID, json!({"procStart": "111"}));
        assert!(resolve_err(&b, NO_SUCH_PID, None).await.contains("is gone"));
    }

    #[tokio::test]
    async fn test_unknown_hosting_kind_refuses() {
        let b = plain();
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "kind": "something-new"}));
        let err = resolve_err(&b, me(), None).await;
        assert!(err.contains("unrecognised hosting kind 'something-new'"), "{err}");
    }

    #[tokio::test]
    async fn test_expect_session_refuses_a_pid_that_resolved_elsewhere() {
        // An ancestry walk past a session with no record lands on whoever launched it.
        let b = with_my_pane();
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "sessionId": "the-launcher"}));
        assert!(resolve_err(&b, me(), Some("mine")).await.contains("different conversation"));
    }

    #[tokio::test]
    async fn test_expect_session_can_only_refuse_never_redirect() {
        // Naming a session that exists does not fetch it: the pid still decides.
        let b = with_my_pane();
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "sessionId": "mine"}));
        write_session(&b.host.home, 5678, json!({"procStart": "111", "sessionId": "theirs"}));
        assert_eq!(b.host.resolve(me(), Some("mine")).await.unwrap().session_id(), "mine");
        assert!(resolve_err(&b, me(), Some("theirs")).await.contains("different conversation"));
    }

    #[tokio::test]
    async fn test_no_expect_session_leaves_resolution_as_it_was() {
        let b = with_my_pane();
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "sessionId": "whatever"}));
        let target = b.host.resolve(me(), None).await.unwrap();
        assert_eq!(target.session_id(), "whatever");
        assert_eq!(target, Target::Tmux(TmuxLane {
            pane: "%3".into(),
            session_id: "whatever".into(),
            repl_pid: me(),
            name: Some(format!("s{}", me())),
        }));
    }

    #[tokio::test]
    async fn test_background_session_joins_the_roster_on_job_id() {
        // jobId names the worker directly and is fixed for the job's life; the roster's own pid is
        // the bg-pty-host, which the match is checked against, never keyed on.
        let b = bg_box(None, &[]);
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "kind": "bg",
            "sessionId": "00000000-0000-4000-8000-000000000001", "jobId": "f0000001"}));
        write_roster(&b.host.home, json!({
            "unrelated": {"sessionId": "other", "pid": 777, "ptySock": "/tmp/wrong.sock", "ptyAuth": "no"},
            "f0000001": {"sessionId": "00000000-0000-4000-8000-000000000001", "pid": 49641,
                         "ptySock": "/tmp/right.sock", "ptyAuth": "yes", "decModes": [2004]},
        }));
        let lane = daemon(&b, me()).await;
        assert_eq!((lane.sock.as_path(), lane.auth.as_str()), (Path::new("/tmp/right.sock"), "yes"));
        assert_eq!(lane.dec_modes, [2004]);
    }

    #[tokio::test]
    async fn test_background_session_survives_a_session_id_that_drifted() {
        // Clearing a conversation rewrites the record's sessionId; the roster keeps the dispatch one.
        let b = bg_box(None, &[]);
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "kind": "bg",
            "sessionId": "00000000-0000-4000-8000-0000000000a2", "jobId": "f0000001"}));
        write_roster(&b.host.home, json!({
            "f0000001": {"sessionId": "00000000-0000-4000-8000-0000000000a1", "pid": 49641,
                         "ptySock": "/tmp/right.sock", "ptyAuth": "yes"},
        }));
        assert_eq!(daemon(&b, me()).await.sock, Path::new("/tmp/right.sock"));
    }

    #[tokio::test]
    async fn test_background_session_without_a_job_id_still_joins_on_session_id() {
        let b = bg_box(None, &[]);
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "kind": "bg",
            "sessionId": "00000000-0000-4000-8000-000000000001"}));
        write_roster(&b.host.home, json!({
            "f0000001": {"sessionId": "00000000-0000-4000-8000-000000000001", "pid": 49641,
                         "ptySock": "/tmp/right.sock", "ptyAuth": "yes"},
        }));
        assert_eq!(daemon(&b, me()).await.sock, Path::new("/tmp/right.sock"));
    }

    #[tokio::test]
    async fn test_background_session_falls_back_to_the_host_that_contains_it() {
        // Neither key matches, but exactly one listed host has this REPL in its subtree.
        let b = bg_box(None, &[]);
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "kind": "bg",
            "sessionId": "unknown", "jobId": "unknown"}));
        write_roster(&b.host.home, json!({
            "elsewhere": {"sessionId": "other", "pid": 777, "ptySock": "/tmp/wrong.sock", "ptyAuth": "no"},
            "f0000001": {"sessionId": "stale", "pid": 49641, "ptySock": "/tmp/right.sock", "ptyAuth": "yes"},
        }));
        assert_eq!(daemon(&b, me()).await.sock, Path::new("/tmp/right.sock"));
    }

    #[tokio::test]
    async fn test_background_session_refuses_a_live_host_that_does_not_contain_it() {
        // The join key matched but the process tree says the entry is somebody else's session.
        let b = bg_box(Some(HashMap::from([(49641, vec![5555])])), &[]);
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "kind": "bg",
            "sessionId": "00000000-0000-4000-8000-000000000001", "jobId": "f0000001"}));
        write_roster(&b.host.home, json!({
            "f0000001": {"sessionId": "00000000-0000-4000-8000-000000000001", "pid": 49641,
                         "ptySock": "/tmp/right.sock", "ptyAuth": "yes"},
        }));
        let err = resolve_err(&b, me(), None).await;
        assert!(err.contains(&format!("does not contain pid {}", me())), "{err}");
    }

    #[tokio::test]
    async fn test_background_session_tolerates_a_host_that_is_gone() {
        // A dead host cannot be asked what it contains; the socket connect fails accurately later.
        let b = bg_box(Some(HashMap::new()), &[49641]);
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "kind": "bg", "jobId": "f0000001"}));
        write_roster(&b.host.home, json!({
            "f0000001": {"sessionId": "whatever", "pid": 49641, "ptySock": "/tmp/right.sock", "ptyAuth": "yes"},
        }));
        assert_eq!(daemon(&b, me()).await.sock, Path::new("/tmp/right.sock"));
    }

    #[tokio::test]
    async fn test_background_session_with_no_roster_entry_refuses() {
        let b = bg_box(Some(HashMap::new()), &[]);
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "kind": "bg",
            "sessionId": "gone", "jobId": "also-gone"}));
        write_roster(&b.host.home, json!({}));
        assert!(resolve_err(&b, me(), None).await.contains("no daemon roster entry"));
    }

    #[tokio::test]
    async fn test_no_roster_entry_does_not_blame_the_pty_host() {
        // An earlier message said the host had probably exited, which sent readers to a live process.
        let b = bg_box(Some(HashMap::new()), &[]);
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "kind": "bg", "jobId": "f0000001"}));
        write_roster(&b.host.home, json!({}));
        let err = resolve_err(&b, me(), None).await;
        assert!(!err.contains("may have exited"));
        assert!(err.contains("f0000001"));
    }

    #[test]
    fn test_the_size_of_a_spare_claimed_job_is_read_from_its_terminal() {
        // A job claimed from a warm spare records no size at dispatch.
        use std::os::fd::{FromRawFd, OwnedFd};
        let (mut master, mut slave) = (0, 0);
        let ws = libc::winsize { ws_row: 50, ws_col: 200, ws_xpixel: 0, ws_ypixel: 0 };
        // SAFETY: openpty writes two fds; the null name, termios and winsize pointers are allowed.
        let rc = unsafe {
            libc::openpty(&mut master, &mut slave, std::ptr::null_mut(), std::ptr::null(), &ws)
        };
        assert_eq!(rc, 0);
        // SAFETY: openpty just returned these fds and nothing else owns them.
        let (master, slave) = unsafe { (OwnedFd::from_raw_fd(master), OwnedFd::from_raw_fd(slave)) };
        let mut child = std::process::Command::new("sleep").arg("30").stdin(slave).spawn().unwrap();
        let size = tty_size(child.id());
        child.kill().unwrap();
        child.wait().unwrap();
        drop(master);
        assert_eq!(size, Some((200, 50)));
        assert_eq!(tty_size(NO_SUCH_PID), None);
    }

    #[tokio::test]
    async fn a_parked_launcher_is_refused() {
        let b = with_my_pane();
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "parkedJobId": "f0000009"}));
        let err = resolve_err(&b, me(), None).await;
        assert!(err.contains("parked its conversation as background job f0000009"), "{err}");
    }

    #[tokio::test]
    async fn a_dispatch_geometry_is_kept_and_a_missing_one_is_read_from_the_terminal() {
        let mut b = bg_box(None, &[]);
        b.host.procs.tty = Some((80, 24));
        write_session(&b.host.home, me(), json!({"procStart": my_start(), "kind": "bg", "jobId": "f0000001"}));
        write_roster(&b.host.home, json!({
            "f0000001": {"pid": 49641, "ptySock": "/s", "ptyAuth": "a", "dispatch": {"cols": 209, "rows": 57}},
        }));
        assert_eq!(daemon(&b, me()).await.geometry, Some((209, 57)));
        write_roster(&b.host.home, json!({"f0000001": {"pid": 49641, "ptySock": "/s", "ptyAuth": "a"}}));
        assert_eq!(daemon(&b, me()).await.geometry, Some((80, 24)));
    }

    #[tokio::test]
    async fn successor_finds_the_live_process_now_holding_the_session() {
        let b = plain();
        let sid = "00000000-0000-4000-8000-000000000003";
        write_session(&b.host.home, me(), json!({"sessionId": sid}));
        assert_eq!(successor(&b.host.home, sid, None).await, Some((me(), my_start())));
        assert_eq!(successor(&b.host.home, sid, Some(me())).await, None);
        assert_eq!(successor(&b.host.home, "", None).await, None);
    }
}
