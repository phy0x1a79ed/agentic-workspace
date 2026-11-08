//! Starting, resuming, adopting and stopping the background sessions other agents ask for, their
//! lineage records, and the rows of `list`. A pending record is written before every launch and
//! `parent` comes only from the caller pid the gateway stamped, so every session has a declared
//! mode and no model can claim a parent.

use std::collections::{BTreeMap, BTreeSet};
use std::future::Future;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use awm_harness::claude::cli::{self, JobProbe, Probe, ProcessRunner, Runner};
use awm_harness::claude::launch::{self, LaunchCommand, LaunchError, LaunchSpec, RespawnSpec, Spawner, SystemHost};
use awm_harness::claude::records::{read_json, read_json_strict, Object};
use awm_harness::claude::roster::{self, Job, JobId};
use awm_harness::claude::{kept, pins, trust};
use awm_plugin_sdk::config::{self, EdgeList};
use awm_plugin_sdk::ids;
use regex::Regex;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::sync::Mutex;
use tokio::time::{sleep, timeout, Instant};

use crate::gateway::{Call, Gateway};
use crate::lineage::{self, Mode, Processes, Procfs};
use crate::paths::{Paths, SKIP_PERMISSION_FLAGS};
use crate::sessions::{Session, Sessions};
use crate::verbs::py_repr;

pub const PERMISSION_MODES: [&str; 6] = ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"];
/// High is the ceiling for any session this service starts or brings back.
pub const EFFORTS: [&str; 3] = ["low", "medium", "high"];
pub const OVER_CEILING: [&str; 2] = ["xhigh", "max"];
/// The mode of a started session. A gated mode keys on its own name; every other session is a
/// plain worker.
pub const DEFAULT_MODE: &str = "worker";
pub const TASK_ROLES: [&str; 2] = ["main", "worker"];

/// How long `claude stop` may run: awm-harness's CLI timeout.
pub const STOP_TIMEOUT: Duration = cli::TIMEOUT;
/// How long a start that asked for Remote Control waits for the bridge id to appear in the job
/// record, and how often it looks.
pub const LINK_WAIT: Duration = Duration::from_secs(8);
pub const LINK_POLL: Duration = Duration::from_millis(500);
pub const SCOPE_CREATE_TIMEOUT: Duration = Duration::from_secs(1700);
pub const TASK_ATTACH_TIMEOUT: Duration = Duration::from_secs(60);
/// A lineage record younger than this is never pruned, so a record written the instant a job
/// registers cannot be collected before the roster shows it.
pub const PRUNE_GRACE_S: f64 = 120.0;
/// The auto-compact window the CLI accepts, in tokens.
pub const AUTOCOMPACT_MIN: i64 = 100_000;
pub const AUTOCOMPACT_MAX: i64 = 1_000_000;
/// How long a pending record waits for its late job before it is dropped.
pub const PENDING_TTL_S: f64 = 300.0;
/// How long a start or resume waits on the keeper to bring a daemon up.
pub const ENSURE_TIMEOUT: Duration = Duration::from_secs(30);
/// The same wait for a stop.
pub const STOP_ENSURE_TIMEOUT: Duration = Duration::from_secs(10);
/// How much of the replaced session's conversation a fresh one starts with.
pub const TAIL_TURNS: usize = 8;
/// The argument the verb table puts the gateway-stamped caller pid under.
pub const CALLER_PID: &str = "_caller_pid";
/// The `code` of a refusal because no daemon runs.
pub const DAEMON_DOWN: &str = "daemon_down";
/// Why an isolated start without a scope is refused: it would have no worktree to mount.
pub const ISOLATED_NEEDS_SCOPE: &str =
    "an isolated session needs a project and a scope: it cannot start in the lobby";

/// Serialises the name check and the launch, so two starts of one name cannot both pass the
/// check. [`adopt_pending`] and the reconcile tick leave a start in flight alone by it.
pub static START_LOCK: Mutex<()> = Mutex::const_new(());

/// Appended to the system prompt of every session `start` launches unpinned, so it outlives every
/// compaction. A kept seat carries its keeper's brief instead.
pub const BRIEF: &str = r#"You are a worker session, started by {started_by}. {where}

{route}

**Name yourself for the task.** Rename yourself to `<task>/<descriptor>` with `reflection(verb="rename")`, under about 40 characters, such as `redux-build/sessions`. Every session of one task shares the `<task>` prefix. The name is for Tony to read. awm tracks you by session id and task id, never by name. {task}

**Ask Tony through the board.** When you need Tony, call `operations(verb="request_human", args={{kind:"blocked", reason:…}})`. That also moves your card to Blocked.

**Keep to your task.** Your task is siloed. Do not message other sessions, and do not answer a session that messages you unless it started you. Send one message, the final report below, to the session that started you. Keep a todo list of your steps with `TaskCreate`, and mark each step done as it lands.

**Start other sessions when the work calls for it.** When the task splits into substantial parallel concerns, plan the work in waves with the `parallel-work` skill. Start a top-level session with `reflection(verb="start_other")` when the plan calls for one, when a discovery needs a new concern handled, or when Tony asks. Pass your `task_id` so the session joins your card. Pass `model` and `effort` that fit the task: a smaller model and lower effort for mechanical work, a larger model and high effort for design or debugging. High is the upper limit. Ask Tony before you pick Fable. When you are not sure a start is fine, ask Tony first. Keep subagents for exploring and for parallel work inside one scope, because a subagent lacks the awm tools. Your scope's worktree is your isolation: do not call `EnterWorktree` and do not create worktrees under `.claude/worktrees/`, whatever another instruction says.

**Close the session when the work is done.** Deploy your work as its project's protocol says. Skip the deploy only when no deploy is possible, as for a design draft. When a coordinator started you for one part of a larger task, leave your branch to the coordinator, which deploys. The work is done when three things hold: what you were asked for is landed and deployed, it is verified, and nothing waits on you. You close on your own: Tony does not accept the work first. Do not retire while a land ticket of yours is unsettled: wait until `scripts/land.sh wait <ticket>` returns `landed` and your live check passes. When you find follow-up issues, stay live and raise each one with `request_human`. To sign off, run the `retire` skill. Where it is missing, sign off by hand:
1. Run the `debrief` skill. Its journal is your report, and it cites the journals that shaped your work (`journal(verb="reads")` lists the candidates).
2. Report what you did {report_to}.
3. Close with `reflection(verb="stop", args={{job:<your job>, confirm:true}})`. `reflection(verb="whoami")` names your job."#;

/// Wrap the first prompt of every briefed start, whoever starts it: a session, a routine's fixer,
/// a deploy's refresh or a board button.
pub const PROMPT_PREFIX: &str = "Your task, from {started_by}:\n\n";
pub const PROMPT_SUFFIX: &str = "\n\nWhen this task is done, landed and verified, sign off with the `retire` skill: debrief, report {report_to}, then stop. Do not wait idle for more work. Keep to this task and message no other session.";

pub const WHERE_SCOPE: &str = "You started in {project}/{scope}, the scope your starter chose.";
pub const WHERE_LOBBY: &str =
    "You started in the lobby, a folder outside every project. You hold no scope there and cannot commit.";

pub const ROUTE_SCOPE: &str = "**Work in the right scope.** Work that changes files belongs in a scope made for it. A project's `dev` and `release` worktrees are for merging, so do no feature work there. When the work does not fit {project}/{scope}, find or create the right scope.{move}";
pub const ROUTE_LOBBY: &str = r#"**Find your home first.** Before any other work, pick the scope the work belongs in. {hint}Search with `scope(verb="search")`. When none fits, create one with `scope(verb="create")`, named as `PROTOCOLS.md` § Scope naming says. When no project fits, create the project with `new_project:true`.{move} Questions and logistics that change no files need no scope."#;
pub const MOVE: &str = r#" Move with `reflection(verb="cd", args={{path:<its worktree>}})`. The move runs when your turn ends, and a follow-up prompt confirms it, so end the turn after that call. When another session holds the scope, cd names it. Then ask Tony through the board."#;
pub const HINT_SUGGEST: &str =
    "Your starter suggests {project}/{scope}. Take it when the work fits there. Choose another scope when it does not. ";
pub const HINT_PROJECT: &str = "Your starter suggests the project {project}. ";

pub const TASK_JOINED: &str = "You joined task `{task_id}` as its {role}: your journals post to its card.";
pub const TASK_OWN: &str = r#"`reflection(verb="whoami")` names your task id once your card exists. When Tony's work turns out to belong to a task that already runs, join it with `operations(verb="task_join")`."#;

pub const REPORT_TO_PARENT: &str = r#"to the session that started you: `SendMessage` it when `ListAgents` lists it, else `reflection(verb="message", args={{job:"{target}", text:…}})`"#;
pub const REPORT_TO_PROMPT: &str =
    "where your first prompt asks. When it names no place, your debrief journal is the report";

/// The lobby's CLAUDE.md. It loads the workspace orientation, which a folder outside the
/// workspace does not inherit.
pub const LOBBY_CLAUDE_MD: &str = "# Lobby\n\nThis folder is the lobby: no project, no scope, no git. Route yourself as your brief says, then move with `reflection cd`.\n\n@{agents}\n";

const MODEL_EFFORT_REQUIRED: &str = "model and effort are required, with no default. Pick both to fit the task: a smaller model and lower effort for mechanical work, a larger model and high effort for design or debugging. Fable needs Tony's consent first.";

static PATH_PART: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._-]*$").unwrap());
/// A tool name, or a tool name with a rule in parentheses. A comma would split the rule inside
/// the comma-joined flag, and a leading `-` would read as a flag.
static TOOL: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z][A-Za-z0-9_*.-]{0,127}(\([^,()\n]{1,200}\))?$").unwrap());
static MODEL: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._\[\]:-]*$").unwrap());
static RC_NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$").unwrap());
static RC_UNSAFE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^A-Za-z0-9._-]+").unwrap());
static UUID: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$").unwrap());
/// A task id (sessions.md R58): the first 8 characters of a urlsafe base64 UUID.
static TASK_ID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_-]{8}$").unwrap());
static SESSION_ID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9A-Za-z-]+$").unwrap());

// --- refusals ----------------------------------------------------------------

/// A start, resume or stop that must not proceed; `reason` says why.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{reason}")]
pub struct Refused {
    pub reason: String,
    /// [`DAEMON_DOWN`] when no daemon runs and the keeper could not bring one up.
    pub code: Option<&'static str>,
}

impl Refused {
    pub fn new(reason: impl Into<String>) -> Self {
        Self { reason: reason.into(), code: None }
    }

    /// No daemon runs, and the keeper could not bring one up.
    pub fn daemon_down(reason: impl Into<String>) -> Self {
        Self { reason: reason.into(), code: Some(DAEMON_DOWN) }
    }

    /// The verb's answer: `{ok: false, reason}`, with `code` when there is one.
    pub fn answer(&self) -> Value {
        refusal(self)
    }
}

/// `{ok: false, reason}`, with `code` when the refusal has one.
pub fn refusal(r: &Refused) -> Value {
    let mut out = json!({"ok": false, "reason": r.reason});
    if let Some(code) = r.code {
        out["code"] = json!(code);
    }
    out
}

/// Why a verb did not complete: a refusal, or a filesystem error where Python let its `OSError`
/// rise to the verb, which answers `<verb> failed: <error>`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum Failure {
    #[error(transparent)]
    Refused(#[from] Refused),
    #[error("{0}")]
    Os(String),
}

impl Failure {
    fn answer(&self, verb: &str) -> Value {
        match self {
            Failure::Refused(r) => {
                tracing::info!("cx: {verb} refused — {r}");
                r.answer()
            }
            Failure::Os(e) => {
                tracing::warn!("cx: {verb} failed — {e}");
                json!({"ok": false, "reason": format!("{verb} failed: {e}")})
            }
        }
    }
}

fn os(e: std::io::Error) -> Failure {
    Failure::Os(e.to_string())
}

// --- the seams ---------------------------------------------------------------

/// The keeper of the Claude Code daemon, as a start, resume or stop needs it.
pub trait DaemonGate: Send + Sync {
    /// The pid of the running daemon, or None when there is none.
    fn pid(&self) -> impl Future<Output = Option<u32>> + Send;
    /// Ask the keeper to bring a daemon up within `timeout`. The answer is the keeper's
    /// `daemon_ensure` result (`ok`, `action`, `reason`, `error`); an error is a keeper that
    /// could not be asked.
    fn ensure(&self, timeout: Duration) -> impl Future<Output = Result<Value, String>> + Send;
}

/// Which new roster job a launch counts as the one it started.
pub type Accept = Arc<dyn Fn(&Job) -> bool + Send + Sync>;

/// Everything lifecycle does beyond its own files under [`Paths`]. [`Live`] is the real one; the
/// tests fake it.
pub trait Effects: Clone + Send + Sync + 'static {
    type Daemon: DaemonGate;
    type Gateway: Gateway;
    type Runner: Runner + Send + Sync;
    type Probe: Probe + Send + Sync;

    fn paths(&self) -> &Paths;
    fn procs(&self) -> &(dyn Processes + Sync);
    fn daemon(&self) -> &Self::Daemon;
    fn gateway(&self) -> &Self::Gateway;
    /// Runs `claude stop` and `claude rm`.
    fn runner(&self) -> &Self::Runner;
    /// Watches a job leave after `claude rm`.
    fn probe(&self) -> &Self::Probe;
    /// Start one background session; the job the daemon registered that `accept` takes.
    fn launch(&self, spec: LaunchSpec, accept: Accept) -> impl Future<Output = Result<Job, LaunchError>> + Send;
    /// `claude respawn <job>`; the job once it is back.
    fn respawn(&self, job: &str, spec: RespawnSpec) -> impl Future<Output = Result<Job, LaunchError>> + Send;
    /// Pane pid to tmux session name, for the default tmux server.
    fn tmux_panes(&self) -> impl Future<Output = BTreeMap<u32, String>> + Send;
    fn node_name(&self) -> String;
    /// The fleet edge list. Reads a file: call it off the executor.
    fn edges(&self) -> anyhow::Result<EdgeList>;
    /// The wall clock, in epoch seconds.
    fn now(&self) -> f64 {
        epoch_now()
    }
    /// How long a start with Remote Control waits for its link.
    fn link_wait(&self) -> Duration {
        LINK_WAIT
    }
    /// The Remote Control session id in `job`'s own record. Reads a file: call it off the executor.
    fn job_bridge_id(&self, job: &str) -> Option<String> {
        roster::job_bridge_id(&self.paths().home, job)
    }
    /// The lock a start holds from its name check to its lineage record.
    fn start_lock(&self) -> &Mutex<()> {
        &START_LOCK
    }
}

/// Runs a launcher in a session of its own. With no user manager `claude --bg` runs straight from
/// cch, and a session started in cch's own process group would die with cch's next restart.
#[derive(Debug, Clone, Copy, Default)]
pub struct SetsidSpawner;

impl Spawner for SetsidSpawner {
    async fn run(&self, cmd: &LaunchCommand, limit: Duration) -> std::io::Result<()> {
        let Some((program, args)) = cmd.argv.split_first() else {
            return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, "empty argv"));
        };
        let mut command = tokio::process::Command::new(program);
        command
            .args(args)
            .current_dir(&cmd.cwd)
            .envs(cmd.env.iter().map(|(k, v)| (k, v)))
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .kill_on_drop(true);
        // SAFETY: setsid is async-signal-safe and touches no memory of the parent.
        unsafe {
            command.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        let mut child = command.spawn()?;
        if timeout(limit, child.wait()).await.is_err() {
            let _ = child.kill().await;
        }
        Ok(())
    }
}

/// The real [`Effects`] over this host, given the keeper and the gateway the service holds.
pub struct Live<D, G> {
    inner: Arc<LiveInner<D, G>>,
}

struct LiveInner<D, G> {
    paths: Paths,
    daemon: D,
    gateway: G,
    probe: JobProbe,
}

impl<D, G> Clone for Live<D, G> {
    fn clone(&self) -> Self {
        Self { inner: self.inner.clone() }
    }
}

impl<D, G> Live<D, G> {
    pub fn new(paths: Paths, daemon: D, gateway: G) -> Self {
        let probe = JobProbe { home: paths.home.clone() };
        Self { inner: Arc::new(LiveInner { paths, daemon, gateway, probe }) }
    }
}

impl<D: DaemonGate + 'static, G: Gateway + 'static> Effects for Live<D, G> {
    type Daemon = D;
    type Gateway = G;
    type Runner = ProcessRunner;
    type Probe = JobProbe;

    fn paths(&self) -> &Paths {
        &self.inner.paths
    }

    fn procs(&self) -> &(dyn Processes + Sync) {
        &Procfs
    }

    fn daemon(&self) -> &D {
        &self.inner.daemon
    }

    fn gateway(&self) -> &G {
        &self.inner.gateway
    }

    fn runner(&self) -> &ProcessRunner {
        &ProcessRunner
    }

    fn probe(&self) -> &JobProbe {
        &self.inner.probe
    }

    async fn launch(&self, spec: LaunchSpec, accept: Accept) -> Result<Job, LaunchError> {
        launch::launch(&self.inner.paths.home, &spec, move |j: &Job| accept(j), &SetsidSpawner, &SystemHost).await
    }

    async fn respawn(&self, job: &str, spec: RespawnSpec) -> Result<Job, LaunchError> {
        launch::respawn(&self.inner.paths.home, job, &spec, &SetsidSpawner, &SystemHost).await
    }

    async fn tmux_panes(&self) -> BTreeMap<u32, String> {
        let run = tokio::process::Command::new("tmux")
            .args(["list-panes", "-a", "-F", "#{pane_pid}\t#{session_name}"])
            .stdin(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .kill_on_drop(true)
            .output();
        match timeout(Duration::from_secs(3), run).await {
            Ok(Ok(out)) => parse_panes(&String::from_utf8_lossy(&out.stdout)),
            _ => BTreeMap::new(),
        }
    }

    fn node_name(&self) -> String {
        config::node_name()
    }

    fn edges(&self) -> anyhow::Result<EdgeList> {
        config::edges()
    }
}

/// `tmux list-panes -F '#{pane_pid}\t#{session_name}'` output as pane pid to session name.
fn parse_panes(text: &str) -> BTreeMap<u32, String> {
    text.lines()
        .filter_map(|line| {
            let (pid, name) = line.split_once('\t')?;
            let pid = pid.bytes().all(|b| b.is_ascii_digit()).then(|| pid.parse().ok()).flatten()?;
            (!name.is_empty()).then(|| (pid, name.to_string()))
        })
        .collect()
}

async fn blocking<E: Effects, T: Send + 'static>(fx: &E, f: impl FnOnce(&E) -> T + Send + 'static) -> T {
    let fx = fx.clone();
    match tokio::task::spawn_blocking(move || f(&fx)).await {
        Ok(v) => v,
        Err(e) => std::panic::resume_unwind(e.into_panic()),
    }
}

// --- who is asking -------------------------------------------------------------

/// Why this node runs no sessions for others, or None when it does. `edges` is the fleet edge
/// list as loading it answered, `node` this node's name.
pub fn station_refusal(edges: anyhow::Result<EdgeList>, node: &str) -> Option<String> {
    match edges {
        Err(e) => Some(format!("the fleet edge list is invalid: {e:#}")),
        Ok(edges) => edges
            .nodes
            .get(&config::label(node))
            .is_some_and(|n| n.role == "station")
            .then(|| "this node is a station and does not run sessions for others".to_string()),
    }
}

/// The station refusal, else the peer-edge refusal of `as_`. Reads the edge list.
pub fn who_may_refusal<E: Effects>(fx: &E, as_: Option<&str>) -> Option<String> {
    station_refusal(fx.edges(), &fx.node_name()).or_else(|| crate::app::caller_refusal(as_, || fx.edges()))
}

fn caller_name(as_: Option<&str>) -> String {
    config::caller_peer(as_).or(as_.filter(|a| !a.is_empty())).unwrap_or("local").to_string()
}

/// The calling session's own record, checked against the live process so a stale record cannot
/// name a parent. `pid` is the gateway-stamped caller pid as the verb received it.
pub fn caller_record<E: Effects>(fx: &E, pid: &Value) -> Option<Object> {
    let pid = pid.as_i64().filter(|p| *p > 0).and_then(|p| u32::try_from(p).ok())?;
    let rec = read_object(&fx.paths().home.session_record(pid))?;
    let live = fx.procs().start(pid)?;
    let claimed = rec.get("procStart").map(py_str).unwrap_or_default();
    (claimed.is_empty() || claimed == live).then_some(rec)
}

/// The session a record names: a background job by its job id, else by its session id.
pub fn session_ref(rec: Option<&Object>) -> Option<String> {
    let rec = rec?;
    if text(rec.get("kind")) == Some("bg") && truthy(rec.get("jobId")) {
        return rec.get("jobId").map(py_str);
    }
    rec.get("sessionId").filter(|v| truthy(Some(v))).map(py_str)
}

/// The calling session: its job id when a background job, else its session id.
pub fn parent_of<E: Effects>(fx: &E, pid: &Value) -> Option<String> {
    session_ref(caller_record(fx, pid).as_ref())
}

fn refuse_unknown_caller<E: Effects>(fx: &E, args: &Value) -> Result<(), Refused> {
    // No caller pid means an operator or a service (the front door), which carries no mode.
    let pid = args.get(CALLER_PID).filter(|v| !v.is_null());
    match pid {
        Some(pid) if lineage::mode_of_arg(fx.paths(), fx.procs(), pid) == Mode::Unknown => {
            Err(Refused::new("the caller's mode could not be determined, so it may not start sessions"))
        }
        _ => Ok(()),
    }
}

/// Why the session at `caller_pid` may not `verb` the job `rec` records, or None when it may.
///
/// A session with a cx mode acts only on the jobs it started. No caller pid, or a pid that is
/// positively not a cx session, is an operator or a service and may act on any job cx started.
pub fn caller_refusal_for<E: Effects>(fx: &E, caller_pid: &Value, rec: &Object, verb: &str) -> Option<String> {
    if caller_pid.is_null() {
        return None;
    }
    match lineage::mode_of_arg(fx.paths(), fx.procs(), caller_pid) {
        Mode::Ungated => return None,
        Mode::Unknown => return Some(format!("the caller's mode could not be determined, so it may not {verb} sessions")),
        Mode::Declared(_) => {}
    }
    let parent = parent_of(fx, caller_pid);
    if parent.is_some() && parent.as_deref() == text(rec.get("parent")) {
        return None;
    }
    Some(format!("a session may {verb} only the jobs it started"))
}

/// Return once a daemon runs, asking the keeper first; else refuse with [`DAEMON_DOWN`].
pub async fn ensure_daemon(gate: &impl DaemonGate, action: &str, timeout: Duration) -> Result<(), Refused> {
    if gate.pid().await.is_some() {
        return Ok(());
    }
    let detail = match gate.ensure(timeout).await {
        Ok(out) if out.is_object() && !truthy(out.get("ok")) => {
            let why: Vec<String> =
                ["reason", "error"].iter().filter_map(|k| out.get(*k).filter(|v| truthy(Some(v))).map(py_str)).collect();
            let action = out.get("action").filter(|v| truthy(Some(v))).map(py_str).unwrap_or_else(|| "down".into());
            format!(" ({action}: {})", why.join("; "))
        }
        Ok(_) => String::new(),
        // No keeper is the old behaviour: refuse as before.
        Err(e) => format!(" (keep: {e})"),
    };
    if gate.pid().await.is_none() {
        return Err(Refused::daemon_down(format!(
            "no claude code daemon is running — a session {action} now would land inside awm's control group{detail}"
        )));
    }
    Ok(())
}

// --- parsing -------------------------------------------------------------------

/// A start's arguments, checked, with cx's defaults filled in.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Spec {
    pub project: Option<String>,
    pub scope: Option<String>,
    pub name: String,
    /// No scope, or one only suggested: the session starts in the lobby.
    pub lobby: bool,
    pub suggest: bool,
    pub task_id: Option<String>,
    pub role: Option<String>,
    pub prompt: Option<String>,
    pub system_prompt: Option<String>,
    pub autocompact: Option<i64>,
    pub model: String,
    pub effort: String,
    pub permission: Option<String>,
    pub mode: String,
    pub disallowed_tools: Vec<String>,
    pub allowed_tools: Vec<String>,
    pub tools: Vec<String>,
    pub restricted: bool,
    pub strict_mcp: bool,
    /// The Remote Control name, or None when it is off.
    pub remote_control: Option<String>,
    pub pin: bool,
    pub brief: bool,
    pub started_by: Option<String>,
    pub replaces: Option<String>,
    pub tags: Option<Vec<String>>,
    /// The awm-only MCP config a strict start loads.
    pub mcp_config: Option<PathBuf>,
    /// The MCP config a lobby start loads, since the lobby is outside the workspace.
    pub lobby_mcp: Option<PathBuf>,
    pub brief_text: Option<String>,
}

/// Refuse an effort that is unknown or above the ceiling.
pub fn check_effort(effort: &Value) -> Result<(), Refused> {
    match effort.as_str() {
        Some(e) if OVER_CEILING.contains(&e) => {
            Err(Refused::new(format!("effort {e} is above the limit. High is the upper limit for any model.")))
        }
        Some(e) if EFFORTS.contains(&e) => Ok(()),
        _ => Err(Refused::new(format!("effort must be one of {}", EFFORTS.join(", ")))),
    }
}

/// Check a start's arguments and fill in cx's defaults. `args` is the verb's argument object.
pub fn parse(paths: &Paths, args: &Value) -> Result<Spec, Refused> {
    let empty = Object::new();
    let a = args.as_object().unwrap_or(&empty);
    let given = |k: &str| a.get(k).filter(|v| !v.is_null());
    let set = |k: &str| a.get(k).filter(|v| truthy(Some(v)));
    let refuse = |why: &str| Err(Refused::new(why));

    let project = match set("project") {
        None => None,
        Some(Value::String(p)) if py_match(&PATH_PART, p) => Some(p.clone()),
        Some(_) => return refuse("project must be a plain project name"),
    };
    let scope = match set("scope") {
        None => None,
        Some(Value::String(s)) if s.split('/').all(|part| py_match(&PATH_PART, part)) => Some(s.clone()),
        Some(_) => return refuse("scope must be a scope name (nested scopes use '/')"),
    };
    if scope.is_some() && project.is_none() {
        return refuse("a scope needs its project");
    }
    let suggest = flag(given("suggest"), "suggest")?;
    let lobby = scope.is_none() || suggest;
    let task_id = match set("task_id") {
        None => None,
        Some(Value::String(t)) if py_match(&TASK_ID, t) => Some(t.clone()),
        Some(_) => return refuse("task_id must be a task id, as reflection whoami reports it"),
    };
    let role = match set("role") {
        None => None,
        Some(Value::String(r)) if TASK_ROLES.contains(&r.as_str()) => Some(r.clone()),
        Some(_) => return Err(Refused::new(format!("role must be one of {}", TASK_ROLES.join(", ")))),
    };
    if role.is_some() && task_id.is_none() {
        return refuse("role needs a task_id");
    }
    let name = match set("name") {
        Some(Value::String(n)) => n.clone(),
        Some(_) => return refuse("name must be a string"),
        None if lobby => format!("lobby-{}", token_hex(2)),
        None => scope.clone().unwrap_or_default(),
    };
    let name = py_strip(&name).to_string();
    if name.is_empty() || name.chars().count() > 80 || !py_printable(&name) || name.starts_with('-') {
        return refuse("name must be a short printable string that does not start with '-'");
    }
    let lower = name.to_lowercase();
    let pool_marks = ["<warm".to_string(), py_strip(&paths.name_prefix).to_lowercase()];
    if pool_marks.iter().any(|mark| !mark.is_empty() && lower.contains(mark.as_str())) {
        return refuse("a name may not carry the pool's prefix");
    }
    let prompt = optional_text(given("prompt"), "prompt must be a string")?;
    let system_prompt = optional_text(given("system_prompt"), "system_prompt must be a string")?;
    let (model, effort) = (a.get("model"), a.get("effort"));
    if !truthy(model) || !truthy(effort) {
        return refuse(MODEL_EFFORT_REQUIRED);
    }
    let model = match model {
        Some(Value::String(m)) if py_match(&MODEL, m) => m.clone(),
        _ => return refuse("model is not a model name"),
    };
    let effort = effort.unwrap_or(&Value::Null);
    check_effort(effort)?;
    let effort = effort.as_str().unwrap_or_default().to_string();
    let permission = match given("permission") {
        None => None,
        Some(Value::String(p)) if PERMISSION_MODES.contains(&p.as_str()) => Some(p.clone()),
        Some(_) => return Err(Refused::new(format!("permission must be one of {}", PERMISSION_MODES.join(", ")))),
    };
    let mode = match set("mode") {
        None => DEFAULT_MODE.to_string(),
        Some(Value::String(m)) if lineage::is_mode(m) => m.clone(),
        Some(_) => return refuse("mode must be a short label"),
    };
    let started_by = match given("started_by") {
        None => None,
        Some(Value::String(s)) if !py_strip(s).is_empty() && s.chars().count() <= 120 && py_printable(s) => {
            Some(py_strip(s).to_string())
        }
        Some(_) => return refuse("started_by must be a short printable string"),
    };
    let brief = match given("brief") {
        None => None,
        Some(Value::Bool(b)) => Some(*b),
        Some(_) => return refuse("brief must be true or false"),
    };
    let replaces = match given("replaces") {
        None => None,
        Some(Value::String(r)) if lineage::is_job(r) => Some(r.clone()),
        Some(_) => return refuse("replaces must be a job id"),
    };
    let autocompact = autocompact(given("autocompact"))?;
    let disallowed_tools = tool_list(given("disallowed_tools"), "disallowed_tools")?;
    let allowed_tools = tool_list(given("allowed_tools"), "allowed_tools")?;
    let tools = tool_list(given("tools"), "tools")?;
    let restricted = flag(given("restricted"), "restricted")?;
    let strict_mcp = flag(given("strict_mcp"), "strict_mcp")?;
    let remote_control = remote_control(given("remote_control"), &name)?;
    let pin = flag(given("pin"), "pin")?;
    let tags = tag_list(given("tags"))?;
    Ok(Spec {
        project,
        scope,
        name,
        lobby,
        suggest,
        task_id,
        role,
        prompt,
        system_prompt,
        autocompact,
        model,
        effort,
        permission,
        mode,
        disallowed_tools,
        allowed_tools,
        tools,
        restricted,
        strict_mcp,
        remote_control,
        pin,
        brief: brief != Some(false) && !pin,
        started_by,
        replaces,
        tags,
        ..Spec::default()
    })
}

/// The project and scope an isolated start mounts; refused when the spec has no scope, before any
/// worktree is looked up.
pub fn isolated_scope(spec: &Spec) -> Result<(String, String), Refused> {
    match (&spec.project, &spec.scope) {
        (Some(project), Some(scope)) => Ok((project.clone(), scope.clone())),
        _ => Err(Refused::new(ISOLATED_NEEDS_SCOPE)),
    }
}

fn optional_text(value: Option<&Value>, why: &str) -> Result<Option<String>, Refused> {
    match value {
        None => Ok(None),
        Some(Value::String(s)) => Ok((!s.is_empty()).then(|| s.clone())),
        Some(_) => Err(Refused::new(why)),
    }
}

fn flag(value: Option<&Value>, field: &str) -> Result<bool, Refused> {
    match value {
        None => Ok(false),
        Some(Value::Bool(b)) => Ok(*b),
        Some(_) => Err(Refused::new(format!("{field} must be true or false"))),
    }
}

fn string_items(value: &Value) -> Option<Vec<String>> {
    match value {
        Value::String(s) => Some(s.split(',').map(str::to_string).collect()),
        Value::Array(items) => items.iter().map(|t| t.as_str().map(str::to_string)).collect(),
        _ => None,
    }
}

fn tool_list(value: Option<&Value>, field: &str) -> Result<Vec<String>, Refused> {
    let Some(value) = value.filter(|v| v.as_str() != Some("")) else { return Ok(Vec::new()) };
    let Some(items) = string_items(value) else {
        return Err(Refused::new(format!("{field} must be a list of tool names")));
    };
    let tools: Vec<String> = items.iter().map(|t| py_strip(t).to_string()).filter(|t| !t.is_empty()).collect();
    if let Some(bad) = tools.iter().find(|t| !py_match(&TOOL, t)) {
        return Err(Refused::new(format!(
            "{field} holds {}, which is not a tool name or a tool rule such as Read(./**) or mcp__awm__*",
            py_repr(bad)
        )));
    }
    Ok(tools)
}

fn tag_list(value: Option<&Value>) -> Result<Option<Vec<String>>, Refused> {
    let Some(value) = value else { return Ok(None) };
    let Some(items) = string_items(value) else { return Err(Refused::new("tags must be a list of tags")) };
    Ok(Some(items.iter().map(|t| py_strip(t).to_string()).filter(|t| !t.is_empty()).collect()))
}

fn autocompact(value: Option<&Value>) -> Result<Option<i64>, Refused> {
    let Some(value) = value else { return Ok(None) };
    // `as_i64` answers only for a JSON integer, so a float or a boolean is refused.
    match value.as_i64() {
        Some(n) if (AUTOCOMPACT_MIN..=AUTOCOMPACT_MAX).contains(&n) => Ok(Some(n)),
        _ => Err(Refused::new(format!("autocompact must be a token count from {AUTOCOMPACT_MIN} to {AUTOCOMPACT_MAX}"))),
    }
}

/// The Remote Control name to use, or None when it is off (sessions.md R146).
///
/// Remote Control is on unless the caller passes false. The flag takes an optional value, and the
/// CLI reads a value that starts with `-` as a new flag, so a name is held to a plain label. The
/// default and `true` derive one from the session name, each `/` and other unsafe run written as `-`.
fn remote_control(value: Option<&Value>, name: &str) -> Result<Option<String>, Refused> {
    match value {
        Some(Value::Bool(false)) => Ok(None),
        None | Some(Value::Bool(true)) => {
            let derived: String =
                RC_UNSAFE.replace_all(name, "-").trim_matches(|c| "-._".contains(c)).chars().take(64).collect();
            if py_match(&RC_NAME, &derived) {
                Ok(Some(derived))
            } else if value.is_none() {
                Ok(None)
            } else {
                Err(Refused::new("remote_control needs a name; pass one explicitly"))
            }
        }
        Some(Value::String(s)) if py_match(&RC_NAME, py_strip(s)) => Ok(Some(py_strip(s).to_string())),
        Some(_) => Err(Refused::new("remote_control must be true, false or a plain label")),
    }
}

/// The claude flags for a start, every valued flag in its `=` form.
///
/// The `=` form keeps a value that starts with `-` from being read as a flag, and it is what
/// [`lineage::looks_cx_started`] keys on: the two change together. The model is a flag, not
/// `ANTHROPIC_MODEL`: the flag wins where the two disagree, and it is the only per-session setting
/// stated in the session's own launch record. A named mode still keeps bypass in the mode cycle,
/// so a plan-mode start returns to bypass on approval. `--restricted` refuses bypass, so a
/// restricted start names its mode alone. Checks whether the restricted settings file exists.
pub fn build_flags(paths: &Paths, spec: &Spec) -> Vec<String> {
    let mut flags: Vec<String> = match &spec.permission {
        None => SKIP_PERMISSION_FLAGS.iter().map(|f| f.to_string()).collect(),
        Some(p) if spec.restricted => vec![format!("--permission-mode={p}")],
        Some(p) => vec![format!("--permission-mode={p}"), "--allow-dangerously-skip-permissions".into()],
    };
    if spec.restricted {
        flags.push("--restricted".into());
        if paths.restricted_settings.exists() {
            flags.push(format!("--settings={}", paths.restricted_settings.display()));
        }
    }
    if !spec.tools.is_empty() {
        flags.push(format!("--tools={}", spec.tools.join(",")));
    }
    if spec.strict_mcp {
        flags.push("--strict-mcp-config".into());
        flags.push(format!("--mcp-config={}", spec.mcp_config.as_deref().unwrap_or(Path::new("")).display()));
    } else if let Some(lobby_mcp) = &spec.lobby_mcp {
        // The lobby is outside the workspace, so its .mcp.json is not loaded.
        flags.push(format!("--mcp-config={}", lobby_mcp.display()));
    }
    if !spec.disallowed_tools.is_empty() {
        flags.push(format!("--disallowedTools={}", spec.disallowed_tools.join(",")));
    }
    if !spec.allowed_tools.is_empty() {
        flags.push(format!("--allowedTools={}", spec.allowed_tools.join(",")));
    }
    if let Some(rc) = &spec.remote_control {
        flags.push(format!("--remote-control={rc}"));
    }
    let system_prompt: Vec<&str> =
        [spec.system_prompt.as_deref(), spec.brief_text.as_deref()].into_iter().flatten().filter(|p| !p.is_empty()).collect();
    if !system_prompt.is_empty() {
        flags.push(format!("--append-system-prompt={}", system_prompt.join("\n\n")));
    }
    if let Some(n) = spec.autocompact.filter(|n| *n != 0) {
        flags.push(format!("--autocompact={n}"));
    }
    flags.push(format!("--effort={}", spec.effort));
    flags.push(format!("--model={}", spec.model));
    flags
}

// --- the brief -----------------------------------------------------------------

/// `template` with each `{name}` replaced in one pass, `{{` and `}}` as single braces: Python's
/// `str.format`, so a value is never itself read as a template.
fn fill(template: &str, vars: &[(&str, &str)]) -> String {
    let mut out = String::with_capacity(template.len());
    let mut rest = template;
    while let Some(i) = rest.find(['{', '}']) {
        out.push_str(&rest[..i]);
        let tail = &rest[i..];
        if let Some(after) = tail.strip_prefix("{{") {
            out.push('{');
            rest = after;
        } else if let Some(after) = tail.strip_prefix("}}") {
            out.push('}');
            rest = after;
        } else if tail.starts_with('{') {
            let end = tail.find('}').expect("a placeholder closes");
            let key = &tail[1..end];
            let value = vars.iter().find(|(k, _)| *k == key).map(|(_, v)| *v).expect("every placeholder is filled");
            out.push_str(value);
            rest = &tail[end + 1..];
        } else {
            out.push('}');
            rest = &tail[1..];
        }
    }
    out.push_str(rest);
    out
}

/// Who started the session, and where it reports.
///
/// A calling session is the parent the session reports to, and outranks a `started_by` the caller
/// supplied. A background parent is addressed by its job id, which a rename does not change; an
/// interactive one by its name.
fn starter(spec: &Spec, caller_rec: Option<&Object>) -> (String, String) {
    if let (Some(parent), Some(rec)) = (session_ref(caller_rec), caller_rec) {
        let name = rec.get("name").filter(|v| truthy(Some(v))).map(py_str).unwrap_or_default();
        let name = py_strip(&name).to_string();
        let started_by =
            if name.is_empty() { format!("the session {parent}") } else { format!("the session {name} ({parent})") };
        let target = if text(rec.get("kind")) == Some("bg") || name.is_empty() { parent } else { name };
        return (started_by, fill(REPORT_TO_PARENT, &[("target", &target)]));
    }
    (spec.started_by.clone().unwrap_or_else(|| "an awm service".into()), REPORT_TO_PROMPT.to_string())
}

/// The first prompt between the prefix and suffix every briefed start shares.
pub fn task_prompt(spec: &Spec, caller_rec: Option<&Object>) -> Option<String> {
    let prompt = spec.prompt.as_deref().filter(|p| !p.is_empty())?;
    let (started_by, report_to) = starter(spec, caller_rec);
    Some(format!(
        "{}{prompt}{}",
        fill(PROMPT_PREFIX, &[("started_by", &started_by)]),
        fill(PROMPT_SUFFIX, &[("report_to", &report_to)])
    ))
}

/// The lifecycle brief for a start, naming who to report to.
pub fn brief_text(spec: &Spec, caller_rec: Option<&Object>) -> String {
    let (started_by, report_to) = starter(spec, caller_rec);
    let project = spec.project.as_deref().unwrap_or("None");
    let scope = spec.scope.as_deref().unwrap_or("None");
    let mv = fill(MOVE, &[]);
    let (where_, route) = if spec.lobby {
        let hint = match (&spec.project, &spec.scope) {
            (_, Some(_)) => fill(HINT_SUGGEST, &[("project", project), ("scope", scope)]),
            (Some(_), None) => fill(HINT_PROJECT, &[("project", project)]),
            (None, None) => String::new(),
        };
        (WHERE_LOBBY.to_string(), fill(ROUTE_LOBBY, &[("hint", &hint), ("move", &mv)]))
    } else {
        (
            fill(WHERE_SCOPE, &[("project", project), ("scope", scope)]),
            fill(ROUTE_SCOPE, &[("project", project), ("scope", scope), ("move", &mv)]),
        )
    };
    let task = match &spec.task_id {
        Some(task_id) => fill(TASK_JOINED, &[("task_id", task_id), ("role", spec.role.as_deref().unwrap_or("worker"))]),
        None => TASK_OWN.to_string(),
    };
    fill(
        BRIEF,
        &[("started_by", &started_by), ("where", &where_), ("route", &route), ("task", &task), ("report_to", &report_to)],
    )
}

// --- start ---------------------------------------------------------------------

/// Create a named background session in a scope's worktree, or in the lobby. `args` are the verb's
/// arguments with the caller pid under [`CALLER_PID`]; `as_` is the gateway's caller stamp.
pub async fn start<E: Effects>(fx: &E, args: &Value, as_: Option<&str>) -> Value {
    start_inner(fx, args, as_).await.unwrap_or_else(|f| f.answer("start"))
}

async fn start_inner<E: Effects>(fx: &E, args: &Value, as_: Option<&str>) -> Result<Value, Failure> {
    let (a, stamp) = (args.clone(), as_.map(str::to_string));
    let mut spec = blocking(fx, move |fx| -> Result<Spec, Refused> {
        if let Some(reason) = who_may_refusal(fx, stamp.as_deref()) {
            return Err(Refused::new(reason));
        }
        let mut spec = parse(fx.paths(), &a)?;
        ensure_starts_dir(fx.paths())?;
        refuse_unknown_caller(fx, &a)?;
        if spec.strict_mcp {
            spec.mcp_config = Some(write_mcp_config(fx.paths())?);
        }
        Ok(spec)
    })
    .await?;
    ensure_daemon(fx.daemon(), "started", ENSURE_TIMEOUT).await?;
    let home = &fx.paths().home;
    let cwd = match (spec.lobby, spec.project.clone(), spec.scope.clone()) {
        (false, Some(project), Some(scope)) => {
            let cwd = resolve_worktree(fx, &project, &scope).await?;
            trust::grant_awm_worktree(&cwd, fx.paths().projects_dir(), home).await;
            cwd
        }
        _ => {
            let cwd = ensure_lobby(fx.paths()).await?;
            spec.lobby_mcp = Some(blocking(fx, |fx| write_mcp_config(fx.paths())).await?);
            trust::grant_plain_dir(&cwd, home).await;
            cwd
        }
    };
    if let Some(untrusted) = trust::start_refusal(&cwd, home).await {
        return Err(Refused::new(untrusted).into());
    }
    let tag_warning = match &spec.tags {
        None => None,
        Some(_) if spec.scope.is_some() => tag_scope(fx, &spec).await,
        Some(_) => Some("the session started in the lobby, so no scope was tagged; tag its scope once it has one".into()),
    };
    let pid = args.get(CALLER_PID).cloned().unwrap_or(Value::Null);
    let caller_rec = blocking(fx, move |fx| caller_record(fx, &pid)).await;
    let parent = session_ref(caller_rec.as_ref());
    let caller = caller_name(as_);
    if spec.task_id.is_some() && spec.role.is_none() {
        // A board or service start opens the task's work; a session's start helps with it.
        spec.role = Some(if parent.is_some() { "worker" } else { "main" }.into());
    }
    if spec.brief {
        spec.brief_text = Some(brief_text(&spec, caller_rec.as_ref()));
        spec.prompt = task_prompt(&spec, caller_rec.as_ref());
    }

    let guard = fx.start_lock().lock().await;
    if let Some(old) = spec.replaces.clone() {
        let (prompt, o) = (spec.prompt.clone(), old.clone());
        spec.prompt = blocking(fx, move |fx| with_tail(fx.paths(), prompt, &o)).await;
        retire(fx, &old).await;
    }
    let name = spec.name.clone();
    if blocking(fx, move |fx| name_taken(fx, &name)).await {
        return Err(Refused::new(format!("a live session is already named {}", py_repr(&spec.name))).into());
    }
    let now = fx.now();
    let mut record = Object::new();
    record.insert("name".into(), json!(spec.name));
    record.insert("project".into(), json!(spec.project));
    record.insert("scope".into(), json!(spec.scope));
    record.insert("cwd".into(), json!(cwd.display().to_string()));
    record.insert("parent".into(), json!(parent));
    record.insert("caller".into(), json!(caller));
    record.insert("mode".into(), json!(spec.mode));
    record.insert("remote_control".into(), json!(spec.remote_control));
    record.insert("model".into(), json!(spec.model));
    record.insert("effort".into(), json!(spec.effort));
    record.insert("started_at".into(), json!(iso(now)));
    record.insert("started_epoch".into(), json!(now));
    record.insert("pinned".into(), json!(spec.pin));
    record.insert("brief".into(), json!(spec.brief));
    record.insert("lobby".into(), json!(spec.lobby));
    if let Some(r9) = caller_rec.as_ref().and_then(|r| ids::to_r9(text(r.get("sessionId")))) {
        record.insert("parent_session".into(), json!(r9));
    }
    if spec.lobby {
        record.insert("project".into(), Value::Null);
        record.insert("scope".into(), Value::Null);
        if spec.scope.is_some() || spec.project.is_some() {
            record.insert("suggested".into(), json!({"project": spec.project, "scope": spec.scope}));
        }
    }
    if let Some(task_id) = &spec.task_id {
        record.insert("task_id".into(), json!(task_id));
        record.insert("task_role".into(), json!(spec.role));
    }
    if let Some(tags) = &spec.tags {
        record.insert("tags".into(), json!(tags));
    }
    if let Some(replaces) = &spec.replaces {
        record.insert("replaces".into(), json!(replaces));
    }
    let pending = record.clone();
    blocking(fx, move |fx| write_pending(fx.paths(), &pending)).await?;

    let flagged = spec.clone();
    let mut launch_spec = LaunchSpec::new(fx.paths().claude_bin.clone(), cwd.clone(), spec.name.clone());
    launch_spec.flags = blocking(fx, move |fx| build_flags(fx.paths(), &flagged)).await;
    launch_spec.prompt = spec.prompt.clone();
    launch_spec.unit_prefix = "awm-cx-start".into();
    let session = match fx.launch(launch_spec, accept_named(&spec.name)).await {
        Ok(session) => session,
        Err(LaunchError::Refused(why)) => {
            let name = spec.name.clone();
            blocking(fx, move |fx| drop_pending(fx.paths(), &name)).await;
            return Err(Refused::new(why).into());
        }
        // The pending record stays: the job may still appear, and until it is adopted the
        // declared mode applies to it by name.
        Err(e) => return Err(Refused::new(format!("the session did not start: {e}")).into()),
    };
    let mut lineage_rec = record.clone();
    lineage_rec.insert("job".into(), json!(session.short));
    lineage_rec.insert("session_id".into(), json!(session.session_id));
    lineage_rec.insert("brief_text".into(), json!(spec.brief_text));
    let (short, name) = (session.short.clone(), spec.name.clone());
    blocking(fx, move |fx| {
        write_lineage(fx.paths(), &short, &lineage_rec).map_err(|e| {
            Refused::new(format!(
                "started {short} but could not record it ({e}); the pending record keeps its mode"
            ))
        })?;
        drop_pending(fx.paths(), &name);
        Ok::<_, Refused>(())
    })
    .await?;
    drop(guard);

    let session_id = ids::to_r9(session.session_id.as_deref());
    let task_warning = match &spec.task_id {
        Some(_) => attach_task(fx, &spec, session_id.as_deref()).await,
        None => None,
    };
    let pinned = spec.pin && pin(fx, &session.short).await;
    tracing::info!(
        "cx: started {} as {} in {} (mode {}{})",
        spec.name,
        session.short,
        cwd.display(),
        spec.mode,
        if pinned { ", pinned" } else { "" }
    );
    let mut out = json!({
        "ok": true, "job": session.short, "session_id": session_id, "name": spec.name,
        "node": fx.node_name(), "project": record["project"], "scope": record["scope"],
        "lobby": spec.lobby, "cwd": cwd.display().to_string(), "attach": format!("claude attach {}", session.short),
        "parent": parent, "mode": spec.mode, "pinned": pinned,
    });
    extend(&mut out, started_link(fx, &session.short, spec.remote_control.is_some()).await);
    if let Some(suggested) = record.get("suggested") {
        out["suggested"] = suggested.clone();
    }
    if let Some(task_id) = &spec.task_id {
        out["task_id"] = json!(task_id);
        out["role"] = json!(spec.role);
    }
    let warnings: Vec<String> = [tag_warning, task_warning].into_iter().flatten().collect();
    if !warnings.is_empty() {
        out["warning"] = json!(warnings.join("; "));
    }
    Ok(out)
}

fn accept_named(name: &str) -> Accept {
    let name = name.to_string();
    Arc::new(move |s: &Job| s.seed_name.as_deref() == Some(name.as_str()) || s.name == name)
}

/// Put the new session on its task's card (sessions.md R57 step 3). A failure is a warning, never
/// a reason to stop a session that already runs.
async fn attach_task<E: Effects>(fx: &E, spec: &Spec, session_id: Option<&str>) -> Option<String> {
    let task_id = spec.task_id.as_deref().unwrap_or_default();
    let Some(session_id) = session_id else {
        return Some(format!("the session started, but its session id is unknown, so it is not on task {task_id}"));
    };
    let args = json!({"task_id": task_id, "session_id": session_id, "role": spec.role, "name": spec.name});
    match fx.gateway().call(Call::new("operations", "task_attach", args).timeout(TASK_ATTACH_TIMEOUT)).await {
        Err(e) => {
            tracing::warn!("cx: could not attach {session_id} to task {task_id}: {e}");
            Some(format!("the session started, but was not put on task {task_id}: {e}"))
        }
        Ok(out) if out.get("ok") == Some(&Value::Bool(false)) => {
            let why = ["reason", "error"].iter().find_map(|k| out.get(*k).filter(|v| truthy(Some(v)))).map_or("None".into(), py_str);
            Some(format!("the session started, but task {task_id} refused it: {why}"))
        }
        Ok(_) => None,
    }
}

/// Set the scope's tags; a failure is a warning, never a reason not to start.
async fn tag_scope<E: Effects>(fx: &E, spec: &Spec) -> Option<String> {
    let args = json!({"project": spec.project, "scope": spec.scope, "tags": spec.tags});
    match fx.gateway().call(Call::new("scope", "tag", args)).await {
        Err(e) => {
            tracing::warn!(
                "cx: could not tag {}/{}: {e}",
                spec.project.as_deref().unwrap_or_default(),
                spec.scope.as_deref().unwrap_or_default()
            );
            Some(format!("the session started, but its scope was not tagged: {e}"))
        }
        Ok(_) => None,
    }
}

/// The `link` fields of a start result.
///
/// A start with Remote Control on waits a bounded time for the bridge id and says why when it has
/// not appeared, because the connection can finish after the start has returned. One with it off
/// answers `link: null` at once.
pub async fn started_link<E: Effects>(fx: &E, job: &str, remote_control: bool) -> Value {
    if !remote_control {
        return json!({"link": null});
    }
    let wait = fx.link_wait();
    let deadline = Instant::now() + wait;
    loop {
        let j = job.to_string();
        if let Some(link) = blocking(fx, move |fx| roster::bridge_link(fx.job_bridge_id(&j).as_deref())).await {
            return json!({"link": link});
        }
        if Instant::now() >= deadline {
            return json!({"link": null, "link_reason": format!(
                "Remote Control had not connected after {}s; `reflection link` with job {job} returns the link once it has",
                wait.as_secs_f64()
            )});
        }
        sleep(LINK_POLL).await;
    }
}

/// Pin `job`; false, with a warning, when the pin list could not be written.
pub async fn pin<E: Effects>(fx: &E, job: &str) -> bool {
    match pins::pin(&fx.paths().home, job).await {
        Ok(()) => true,
        Err(e) => {
            tracing::warn!("cx: could not pin {job}: {e}");
            false
        }
    }
}

/// The node's lobby folder, made if absent (scopes.md R40). A lobby inside a git repository,
/// where a session could commit, is refused.
pub async fn ensure_lobby(paths: &Paths) -> Result<PathBuf, Refused> {
    let lobby = paths.lobby_dir.clone();
    tokio::fs::create_dir_all(&lobby)
        .await
        .map_err(|e| Refused::new(format!("cannot make the lobby {}: {e}", lobby.display())))?;
    if trust::workspace_key(&lobby).await.is_some() {
        return Err(Refused::new(format!(
            "the lobby {} is inside a git repository; set AWM_CX_LOBBY to a folder outside every repository",
            lobby.display()
        )));
    }
    let agents = paths.workspace_dir().join("AGENTS.md").display().to_string();
    let text = fill(LOBBY_CLAUDE_MD, &[("agents", &agents)]);
    let md = lobby.join("CLAUDE.md");
    if tokio::fs::read_to_string(&md).await.ok().as_deref() != Some(text.as_str()) {
        tokio::fs::write(&md, &text)
            .await
            .map_err(|e| Refused::new(format!("cannot write the lobby's CLAUDE.md: {e}")))?;
    }
    Ok(lobby)
}

/// The awm-only MCP config a strict or lobby session loads, written from the workspace's own.
pub fn write_mcp_config(paths: &Paths) -> Result<PathBuf, Refused> {
    let server = paths
        .awm_mcp_server()
        .map_err(|e| Refused::new(format!("cannot read the workspace MCP config: {e}")))?
        .ok_or_else(|| {
            Refused::new("the workspace .mcp.json names no awm server, so a strict session has nothing to load")
        })?;
    let path = paths.mcp_config_path();
    write_json(&path, &json!({"mcpServers": {"awm": server}}))
        .map_err(|e| Refused::new(format!("cannot write the MCP config ({e})")))?;
    Ok(path)
}

/// The scope's worktree, created through the scopes service when absent.
pub async fn resolve_worktree<E: Effects>(fx: &E, project: &str, scope: &str) -> Result<PathBuf, Failure> {
    let root = fx.paths().projects_dir();
    let cwd = root.join(project).join(scope);
    let (c, r) = (cwd.clone(), root.clone());
    let inside = blocking(fx, move |_| Ok::<_, std::io::Error>(resolve(&c)?.starts_with(resolve(&r)?))).await.map_err(os)?;
    if !inside {
        return Err(Refused::new("the scope resolves outside the projects directory").into());
    }
    if is_dir(&cwd).await {
        return Ok(cwd);
    }
    if let Err(e) = create_scope(fx, project, scope).await {
        return Err(Refused::new(format!("could not create scope {project}/{scope}: {e}")).into());
    }
    if !is_dir(&cwd).await {
        return Err(Refused::new(format!("scope {project}/{scope} was created but {} is missing", cwd.display())).into());
    }
    Ok(cwd)
}

/// `scope create` through the gateway, which can take as long as a clone.
pub async fn create_scope<E: Effects>(fx: &E, project: &str, scope: &str) -> Result<Value, String> {
    let call = Call::new("scope", "create", json!({"project": project, "scope": scope})).timeout(SCOPE_CREATE_TIMEOUT);
    fx.gateway().call(call).await
}

/// Python's lenient `Path.resolve()`: symlinks resolved as far as the path exists, the rest kept.
fn resolve(path: &Path) -> std::io::Result<PathBuf> {
    let path = if path.is_absolute() { path.to_path_buf() } else { std::env::current_dir()?.join(path) };
    match std::fs::canonicalize(&path) {
        Ok(p) => Ok(p),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => match (path.parent(), path.file_name()) {
            (Some(parent), Some(name)) => Ok(resolve(parent)?.join(name)),
            _ => Ok(path),
        },
        Err(e) => Err(e),
    }
}

async fn is_dir(path: &Path) -> bool {
    tokio::fs::metadata(path).await.is_ok_and(|m| m.is_dir())
}

/// Whether a live job, a start in flight or an interactive session already holds `name`.
pub fn name_taken<E: Effects>(fx: &E, name: &str) -> bool {
    let sessions = Sessions::new(fx.paths(), fx.procs());
    sessions.load().iter().any(|s| s.has_record && s.name == name && sessions.is_alive(s))
        || lineage::pending_records(fx.paths()).iter().any(|(_, p)| p.name.as_deref() == Some(name))
        || interactive_records(fx).iter().any(|rec| text(rec.get("name")) == Some(name))
}

// --- lineage -------------------------------------------------------------------

/// Make the lineage directory; refuse to start a session that could not be recorded.
pub fn ensure_starts_dir(paths: &Paths) -> Result<(), Refused> {
    std::fs::create_dir_all(paths.starts_dir()).map_err(|e| {
        Refused::new(format!("cannot keep lineage records ({e}); refusing to start a session that could not be recorded"))
    })
}

fn read_object(path: &Path) -> Option<Object> {
    match serde_json::from_slice(&std::fs::read(path).ok()?).ok()? {
        Value::Object(rec) => Some(rec),
        _ => None,
    }
}

/// The lineage record of `job` as written, or None when `job` is not a job id or its record is
/// absent or unreadable.
pub fn read_lineage(paths: &Paths, job: &str) -> Option<Object> {
    if !lineage::is_job(job) {
        return None;
    }
    read_object(&paths.lineage_path(job))
}

/// `value` as Python's `json.dumps(value, indent=indent)` writes it: ASCII only, `\uXXXX` escapes.
fn py_json(value: &Value, indent: usize) -> String {
    let pad = vec![b' '; indent];
    let mut buf = Vec::new();
    let mut ser = serde_json::Serializer::with_formatter(&mut buf, serde_json::ser::PrettyFormatter::with_indent(&pad));
    value.serialize(&mut ser).expect("a JSON value serializes");
    let text = String::from_utf8(buf).expect("serde_json writes UTF-8");
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        if c.is_ascii() {
            out.push(c);
        } else {
            let mut units = [0u16; 2];
            for unit in c.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

/// Write `value` to `path` as Python's lineage writer does: indent 1, mode 0600, through a `.tmp`
/// sibling and a rename.
fn write_json(path: &Path, value: &Value) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, py_json(value, 1))?;
    std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))?;
    std::fs::rename(&tmp, path)
}

/// Write `job`'s lineage record. A record without a valid `mode` is refused: the gateway would
/// gate its session as `unknown`.
pub fn write_lineage(paths: &Paths, job: &str, record: &Object) -> std::io::Result<()> {
    if !text(record.get("mode")).is_some_and(lineage::is_mode) {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, "a lineage record needs a valid mode"));
    }
    write_json(&paths.lineage_path(job), &Value::Object(record.clone()))
}

/// Record the declared mode before the launch; no record, no launch.
pub fn write_pending(paths: &Paths, record: &Object) -> Result<(), Refused> {
    let mut rec = record.clone();
    rec.insert("pending".into(), Value::Bool(true));
    let name = text(record.get("name")).unwrap_or_default();
    write_json(&paths.pending_path(name), &Value::Object(rec)).map_err(|e| {
        Refused::new(format!("cannot keep lineage records ({e}); refusing to start a session that could not be recorded"))
    })
}

/// Drop the pending record of the start of `name`.
pub fn drop_pending(paths: &Paths, name: &str) {
    match std::fs::remove_file(paths.pending_path(name)) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => {
            tracing::warn!("cx: could not drop the pending record of {}: {e}", py_repr(name));
        }
        _ => {}
    }
}

/// Give `job` a lineage record when it has none, so `resume` takes it; the error, if any. The
/// record says `mode: "worker"`, since a record with no mode gates its session as `unknown`.
pub fn ensure_lineage(paths: &Paths, job: &str, origin: &str, extra: &Object) -> Option<String> {
    if read_lineage(paths, job).is_some() {
        return None;
    }
    let mut record = Object::new();
    record.insert("job".into(), json!(job));
    record.insert("parent".into(), Value::Null);
    record.insert("origin".into(), json!(origin));
    record.insert("mode".into(), json!(DEFAULT_MODE));
    record.extend(extra.clone());
    write_lineage(paths, job, &record).err().map(|e| format!("no lineage: {e}"))
}

fn lineage_objects(paths: &Paths) -> BTreeMap<String, Object> {
    lineage::lineage_records(paths).into_iter().map(|(job, rec)| (job, Object::from(rec))).collect()
}

/// Delete the lineage records of jobs that `claude rm` has removed; the jobs pruned.
///
/// A stopped job keeps its record: `claude attach` brings the conversation back, and it must come
/// back in the same mode. The job is gone only when the roster no longer holds it and its state
/// record is deleted. Nothing is pruned when the roster or the jobs directory cannot be read,
/// because every record would then look orphaned. Reads files: call it off the executor.
pub fn prune_lineage<E: Effects>(fx: &E, now: Option<f64>) -> Vec<String> {
    let paths = fx.paths();
    let Ok(doc) = read_json_strict(&paths.home.roster()) else { return Vec::new() };
    let Some(Value::Object(workers)) = doc.get("workers") else { return Vec::new() };
    let jobs = paths.home.jobs();
    if !jobs.is_dir() {
        return Vec::new();
    }
    let now = now.unwrap_or_else(|| fx.now());
    let mut gone = Vec::new();
    for job in lineage::lineage_records(paths).into_keys() {
        if workers.contains_key(&job) {
            continue;
        }
        let path = paths.lineage_path(&job);
        let prune = || -> std::io::Result<bool> {
            if paths.home.job_state(&job).exists() {
                return Ok(false);
            }
            let mtime = std::fs::metadata(&path)?.modified()?.duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64());
            if now - mtime < PRUNE_GRACE_S {
                return Ok(false);
            }
            std::fs::remove_file(&path)?;
            Ok(true)
        };
        if prune().unwrap_or(false) {
            gone.push(job);
        }
    }
    gone
}

/// Unpin jobs whose daemon record is gone, so the pin list cannot grow stale; the jobs unpinned.
/// Nothing is unpinned when the jobs directory cannot be read.
pub async fn prune_pins<E: Effects>(fx: &E) -> Vec<String> {
    let home = &fx.paths().home;
    let Ok(mut dir) = tokio::fs::read_dir(home.jobs()).await else { return Vec::new() };
    let mut present = BTreeSet::new();
    while let Ok(Some(entry)) = dir.next_entry().await {
        present.insert(entry.file_name().to_string_lossy().into_owned());
    }
    let stale: Vec<String> = pins::pinned(home).await.into_iter().filter(|j| !present.contains(j)).collect();
    if stale.is_empty() {
        return Vec::new();
    }
    pins::unpin(home, &stale).await.unwrap_or_else(|e| {
        tracing::warn!("cx: could not prune pins: {e}");
        Vec::new()
    })
}

fn started_epoch(rec: &Object) -> f64 {
    rec.get("started_epoch").and_then(Value::as_f64).unwrap_or(0.0)
}

/// Whether `s` is the late job of the pending start `rec`: the same name, and not older than the
/// start allowing five seconds of clock skew.
fn is_late_job_of(s: &Session, rec: &Object) -> bool {
    let Some(name) = text(rec.get("name")) else { return false };
    (s.seed_name.as_deref() == Some(name) || s.name == name)
        && s.started_at_ms.unwrap_or(0) as f64 / 1000.0 >= started_epoch(rec) - 5.0
}

/// Give each pending record to the job that outlived its launch timeout; the jobs adopted.
///
/// A pending record whose job never appeared is dropped after [`PENDING_TTL_S`]. A start in flight
/// holds the start lock and is left to finish. Reads files: call it off the executor.
pub fn adopt_pending<E: Effects>(fx: &E, now: Option<f64>) -> Vec<String> {
    let Ok(_held) = fx.start_lock().try_lock() else { return Vec::new() };
    let paths = fx.paths();
    let pending = lineage::pending_records(paths);
    if pending.is_empty() {
        return Vec::new();
    }
    let now = now.unwrap_or_else(|| fx.now());
    let held: BTreeSet<String> = lineage::lineage_records(paths).into_keys().collect();
    let live = Sessions::new(paths, fx.procs()).load();
    let mut adopted = Vec::new();
    for (path, _) in pending {
        // The record as written, so the adopted one keeps its keys in their order.
        let Some(rec) = read_object(&path) else { continue };
        let late = live.iter().find(|s| !held.contains(&s.short) && is_late_job_of(s, &rec));
        let done = match late {
            Some(late) => {
                let mut record = rec.clone();
                record.insert("job".into(), json!(late.short));
                write_lineage(paths, &late.short, &record).and_then(|_| std::fs::remove_file(&path)).map(|_| {
                    adopted.push(late.short.clone());
                    tracing::info!("cx: adopted {} for the pending start of {}", late.short, py_repr(text(rec.get("name")).unwrap_or_default()));
                })
            }
            None if now - started_epoch(&rec) > PENDING_TTL_S => std::fs::remove_file(&path),
            None => Ok(()),
        };
        if let Err(e) = done {
            tracing::warn!("cx: pending record {}: {e}", path.file_name().unwrap_or_default().to_string_lossy());
        }
    }
    adopted
}

// --- replace -------------------------------------------------------------------

/// The newest transcript of `session_id` under Claude Code's projects directory.
fn find_transcript(paths: &Paths, session_id: &str) -> Option<PathBuf> {
    if session_id.is_empty() {
        return None;
    }
    let name = format!("{session_id}.jsonl");
    let mut best: Option<(Option<SystemTime>, PathBuf)> = None;
    for entry in std::fs::read_dir(paths.transcripts_dir()).ok()?.flatten() {
        let candidate = entry.path().join(&name);
        if !candidate.is_file() {
            continue;
        }
        let mtime = candidate.metadata().and_then(|m| m.modified()).ok();
        if best.as_ref().is_none_or(|(m, _)| mtime > *m) {
            best = Some((mtime, candidate));
        }
    }
    best.map(|(_, p)| p)
}

fn transcript_exists(paths: &Paths, session_id: &str) -> bool {
    py_match(&SESSION_ID, session_id) && find_transcript(paths, session_id).is_some()
}

/// `prompt` with the last turns of `old`'s conversation appended. Reads files.
pub fn with_tail(paths: &Paths, prompt: Option<String>, old: &str) -> Option<String> {
    let state = read_json(&paths.home.job_state(old));
    let Some(session_id) = text(state.get("sessionId")).filter(|s| py_match(&SESSION_ID, s)) else { return prompt };
    let Some(found) = find_transcript(paths, session_id) else { return prompt };
    let Ok(bytes) = std::fs::read(found) else { return prompt };
    let tail = kept::transcript_tail(String::from_utf8_lossy(&bytes).lines(), TAIL_TURNS, kept::TAIL_LIMIT);
    if tail.is_empty() {
        return prompt;
    }
    let intro = format!("The session you replace (job {old}) ended with these turns:");
    let parts: Vec<&str> = [prompt.as_deref(), Some(intro.as_str()), Some(tail.as_str())].into_iter().flatten().filter(|p| !p.is_empty()).collect();
    Some(parts.join("\n\n"))
}

/// Stop and remove the job a fresh start replaces, when cx started it. Done after its tail is
/// read, because removing a pinned job deletes the state record that names its transcript.
async fn retire<E: Effects>(fx: &E, old: &str) {
    let o = old.to_string();
    let Some(rec) = blocking(fx, move |fx| read_lineage(fx.paths(), &o)).await else { return };
    let out = if truthy(rec.get("pinned")) || pins::pinned(&fx.paths().home).await.iter().any(|j| j == old) {
        stop_pinned(fx, old).await
    } else {
        let (ok, detail) = run_claude_stop(fx, old).await;
        if ok {
            let o = old.to_string();
            blocking(fx, move |fx| record_stop(fx.paths(), &o)).await;
        }
        json!({"ok": ok, "reason": detail})
    };
    if !truthy(out.get("ok")) {
        tracing::info!("cx: replacing {old}, which would not stop: {}", out.get("reason").map(py_str).unwrap_or_default());
    }
}

// --- adopt ---------------------------------------------------------------------

/// Give a job cx did not start a lineage record, so cx may resume and stop it: a session started
/// by hand that a keeper now keeps.
pub async fn adopt<E: Effects>(fx: &E, args: &Value, as_: Option<&str>) -> Value {
    let (a, stamp) = (args.clone(), as_.map(str::to_string));
    let checked = blocking(fx, move |fx| -> Result<(String, Object), String> {
        if let Some(reason) = who_may_refusal(fx, stamp.as_deref()) {
            return Err(reason);
        }
        let job = match a.get("job") {
            Some(Value::String(j)) if lineage::is_job(j) => j.clone(),
            _ => return Err("job must be a job id".into()),
        };
        if read_lineage(fx.paths(), &job).is_some() {
            return Err(format!("job {job} already has a lineage record"));
        }
        let state = read_json(&fx.paths().home.job_state(&job));
        if state.is_empty() {
            return Err(format!("job {job} has no state record"));
        }
        Ok((job, state))
    })
    .await;
    let (job, state) = match checked {
        Ok(found) => found,
        Err(reason) => return json!({"ok": false, "reason": reason}),
    };
    let pinned = pins::pinned(&fx.paths().home).await.contains(&job);
    let now = fx.now();
    let caller = caller_name(as_);
    let j = job.clone();
    let written = blocking(fx, move |fx| {
        let (project, scope) = project_scope(fx.paths(), text(state.get("cwd")));
        let record = json!({
            "job": j, "name": state.get("name"), "project": project, "scope": scope,
            "cwd": state.get("cwd"), "parent": null, "caller": caller,
            "mode": DEFAULT_MODE, "remote_control": null, "model": null,
            "started_at": iso(now), "started_epoch": now,
            "pinned": pinned, "brief": false, "adopted": true,
        });
        let Value::Object(record) = record else { unreachable!("a json! object") };
        write_lineage(fx.paths(), &j, &record).map(|_| record)
    })
    .await;
    match written {
        Ok(record) => {
            tracing::info!("cx: adopted {job} ({}){}", record.get("name").map(py_str).unwrap_or_default(), if pinned { ", pinned" } else { "" });
            json!({"ok": true, "job": job, "name": record["name"], "pinned": pinned,
                   "project": record["project"], "scope": record["scope"]})
        }
        Err(e) => json!({"ok": false, "reason": format!("could not record {job}: {e}")}),
    }
}

// --- resume --------------------------------------------------------------------

enum Resume {
    /// Respawn the stopped job under its own id.
    Respawn(String, Object),
    /// Start a new job from the transcript of this session id.
    Retired(String, Option<Object>),
}

/// Bring back a stopped job that `start` created, with its own conversation, or start a new job
/// from a retired session's transcript.
pub async fn resume<E: Effects>(fx: &E, args: &Value, as_: Option<&str>) -> Value {
    resume_inner(fx, args, as_).await.unwrap_or_else(|f| f.answer("resume"))
}

/// A job started above the ceiling stays down: resume carries its recorded effort back.
fn check_recorded_effort(rec: &Object) -> Result<(), Refused> {
    match text(rec.get("effort")) {
        Some(e) if OVER_CEILING.contains(&e) => Err(Refused::new(format!(
            "job was started at effort {e}, which is above the limit. High is the upper limit for any model. Start a new session instead."
        ))),
        _ => Ok(()),
    }
}

async fn resume_inner<E: Effects>(fx: &E, args: &Value, as_: Option<&str>) -> Result<Value, Failure> {
    let (a, stamp) = (args.clone(), as_.map(str::to_string));
    let step = blocking(fx, move |fx| -> Result<Resume, Refused> {
        if let Some(reason) = who_may_refusal(fx, stamp.as_deref()) {
            return Err(Refused::new(reason));
        }
        let job = match a.get("job") {
            Some(Value::String(j)) if lineage::is_job(j) || py_match(&UUID, j) => j.clone(),
            _ => return Err(Refused::new("job must be a job id or a session id")),
        };
        let job = if py_match(&UUID, &job) {
            match job_of_session(fx.paths(), &job) {
                None => return Ok(Resume::Retired(job, None)),
                Some(found) => found,
            }
        } else {
            job
        };
        let Some(rec) = read_lineage(fx.paths(), &job) else {
            return Err(Refused::new(format!(
                "job {job} was not started by reflection start_other, so it will not be resumed"
            )));
        };
        if let Some(reason) = caller_refusal_for(fx, a.get(CALLER_PID).unwrap_or(&Value::Null), &rec, "resume") {
            return Err(Refused::new(reason));
        }
        check_recorded_effort(&rec)?;
        let state = read_json(&fx.paths().home.job_state(&job));
        if state.is_empty() {
            return match text(rec.get("session_id")) {
                Some(sid) => Ok(Resume::Retired(sid.to_string(), Some(rec))),
                None => Err(Refused::new(format!("job {job} has no state record; it was removed"))),
            };
        }
        if !text(state.get("sessionId")).is_some_and(|sid| transcript_exists(fx.paths(), sid)) {
            return Err(Refused::new(format!("job {job} has no transcript left to resume")));
        }
        Ok(Resume::Respawn(job, rec))
    })
    .await?;
    let (job, rec) = match step {
        Resume::Retired(session_id, rec) => return resume_retired(fx, &session_id, rec, args).await,
        Resume::Respawn(job, rec) => (job, rec),
    };
    ensure_daemon(fx.daemon(), "resumed", ENSURE_TIMEOUT).await?;
    let session = {
        let _guard = fx.start_lock().lock().await;
        let mut spec = RespawnSpec::new(fx.paths().claude_bin.clone());
        spec.unit_prefix = "awm-cx-resume".into();
        match fx.respawn(&job, spec).await {
            Ok(session) => session,
            Err(LaunchError::Refused(why)) => return Err(Refused::new(why).into()),
            Err(e) => return Err(Refused::new(format!("the session did not resume: {e}")).into()),
        }
    };
    let pinned = truthy(rec.get("pinned")) && pin(fx, &job).await;
    tracing::info!("cx: resumed {} as {job}{}", session.name, if pinned { ", pinned" } else { "" });
    let mut out = json!({
        "ok": true, "job": job, "name": session.name, "node": fx.node_name(),
        "project": rec.get("project"), "scope": rec.get("scope"),
        "attach": format!("claude attach {job}"), "pinned": pinned,
    });
    extend(&mut out, started_link(fx, &job, truthy(rec.get("remote_control"))).await);
    Ok(out)
}

/// The job that still holds `session_id`: in its state record, else in its lineage.
fn job_of_session(paths: &Paths, session_id: &str) -> Option<String> {
    let mut dirs: Vec<PathBuf> =
        std::fs::read_dir(paths.home.jobs()).map(|d| d.flatten().map(|e| e.path()).collect()).unwrap_or_default();
    dirs.sort();
    for dir in dirs {
        if text(read_json(&dir.join("state.json")).get("sessionId")) == Some(session_id) {
            return dir.file_name().map(|n| n.to_string_lossy().into_owned());
        }
    }
    lineage_objects(paths).into_iter().find(|(_, rec)| text(rec.get("session_id")) == Some(session_id)).map(|(job, _)| job)
}

/// Where the session last worked, among the folders its transcript's directory names.
///
/// Claude Code files a transcript under a slug of the folder the session started in, and
/// `--resume` looks there. A later folder whose slug differs, after a move, would not find the
/// transcript, so the folder that matches wins.
fn transcript_cwd(path: &Path) -> Option<String> {
    let bytes = std::fs::read(path).ok()?;
    let slug_dir = path.parent().and_then(Path::file_name).map(|n| n.to_string_lossy().into_owned());
    let (mut last, mut matching) = (None, None);
    for line in String::from_utf8_lossy(&bytes).lines() {
        let Ok(Value::Object(entry)) = serde_json::from_str::<Value>(line) else { continue };
        let Some(cwd) = text(entry.get("cwd")).filter(|c| !c.is_empty()) else { continue };
        last = Some(cwd.to_string());
        let slug: String = cwd.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect();
        if slug_dir.as_deref() == Some(slug.as_str()) {
            matching = Some(cwd.to_string());
        }
    }
    matching.or(last)
}

/// Start a new job from a retired session's transcript, under the same session id
/// (sessions.md R10).
///
/// The job is gone, so `claude --bg --resume` continues the conversation in a new job. The lineage
/// record, while it lasts, gives the name, the mode and the brief back.
async fn resume_retired<E: Effects>(
    fx: &E,
    session_id: &str,
    rec: Option<Object>,
    args: &Value,
) -> Result<Value, Failure> {
    let rec = rec.unwrap_or_default();
    let (a, r, sid) = (args.clone(), rec.clone(), session_id.to_string());
    let (spec, cwd) = blocking(fx, move |fx| -> Result<(Spec, String), Refused> {
        if let Some(reason) = caller_refusal_for(fx, a.get(CALLER_PID).unwrap_or(&Value::Null), &r, "resume") {
            return Err(Refused::new(reason));
        }
        check_recorded_effort(&r)?;
        let Some(transcript) = find_transcript(fx.paths(), &sid) else {
            return Err(Refused::new(format!("session {sid} has no transcript left to resume")));
        };
        let cwd = transcript_cwd(&transcript);
        let Some(cwd) = cwd.filter(|c| Path::new(c).is_dir()) else {
            let shown = transcript_cwd(&transcript).unwrap_or_else(|| "a folder its transcript does not name".into());
            return Err(Refused::new(format!("session {sid} worked in {shown}, which is not on this node any more")));
        };
        let short: String = sid.chars().take(8).collect();
        let name = [a.get("name"), r.get("name")].into_iter().flatten().find(|v| truthy(Some(v))).cloned().unwrap_or_else(|| json!(format!("resumed-{short}")));
        let or_default = |key: &str, default: &str| r.get(key).filter(|v| truthy(Some(v))).cloned().unwrap_or_else(|| json!(default));
        // A resume continues a conversation, so a lost lineage record falls back to a fixed model
        // at the effort ceiling rather than refusing the resume.
        let mut spec = parse(
            fx.paths(),
            &json!({
                "name": name, "project": r.get("project"), "scope": r.get("scope"),
                "model": or_default("model", "opus"), "effort": or_default("effort", "high"),
                "mode": r.get("mode"),
                "remote_control": r.get("remote_control").filter(|v| truthy(Some(v))), "brief": false,
            }),
        )?;
        spec.brief_text = text(r.get("brief_text")).map(str::to_string);
        Ok((spec, cwd))
    })
    .await?;
    ensure_daemon(fx.daemon(), "resumed", ENSURE_TIMEOUT).await?;
    let guard = fx.start_lock().lock().await;
    let name = spec.name.clone();
    if blocking(fx, move |fx| name_taken(fx, &name)).await {
        return Err(Refused::new(format!("a live session is already named {}", py_repr(&spec.name))).into());
    }
    let flagged = spec.clone();
    let mut launch_spec = LaunchSpec::new(fx.paths().claude_bin.clone(), PathBuf::from(&cwd), spec.name.clone());
    launch_spec.flags = blocking(fx, move |fx| build_flags(fx.paths(), &flagged)).await;
    launch_spec.flags.push(format!("--resume={session_id}"));
    launch_spec.prompt = text(args.get("prompt")).filter(|p| !p.is_empty()).map(str::to_string);
    launch_spec.unit_prefix = "awm-cx-resume".into();
    let session = match fx.launch(launch_spec, accept_named(&spec.name)).await {
        Ok(session) => session,
        Err(LaunchError::Refused(why)) => return Err(Refused::new(why).into()),
        Err(e) => return Err(Refused::new(format!("the session did not resume: {e}")).into()),
    };
    let now = fx.now();
    let (short, s, c, sid) = (session.short.clone(), spec.clone(), cwd.clone(), session_id.to_string());
    let (project, scope) = blocking(fx, move |fx| -> std::io::Result<(Value, Value)> {
        let (project, scope) = match project_scope(fx.paths(), Some(&c)) {
            (None, _) => (rec.get("project").cloned().unwrap_or(Value::Null), rec.get("scope").cloned().unwrap_or(Value::Null)),
            (p, sc) => (json!(p), json!(sc)),
        };
        let mut record: Object = rec.iter().filter(|(k, _)| *k != "job").map(|(k, v)| (k.clone(), v.clone())).collect();
        for (key, value) in [
            ("job", json!(short)),
            ("name", json!(s.name)),
            ("cwd", json!(c)),
            ("project", project.clone()),
            ("scope", scope.clone()),
            ("mode", json!(s.mode)),
            ("remote_control", json!(s.remote_control)),
            ("session_id", json!(sid)),
            ("resumed_from", rec.get("job").cloned().unwrap_or(Value::Null)),
            ("started_at", json!(iso(now))),
            ("started_epoch", json!(now)),
            ("pinned", json!(false)),
        ] {
            record.insert(key.into(), value);
        }
        write_lineage(fx.paths(), &short, &record)?;
        Ok((project, scope))
    })
    .await
    .map_err(os)?;
    drop(guard);
    tracing::info!("cx: resumed retired session {session_id} as {} in {cwd}", session.short);
    let mut out = json!({
        "ok": true, "job": session.short, "session_id": ids::to_r9(Some(session_id)),
        "name": spec.name, "node": fx.node_name(), "project": project, "scope": scope,
        "cwd": cwd, "attach": format!("claude attach {}", session.short), "pinned": false,
        "from_transcript": true,
    });
    extend(&mut out, started_link(fx, &session.short, spec.remote_control.is_some()).await);
    Ok(out)
}

// --- stop ----------------------------------------------------------------------

/// Stop a session `start` created.
///
/// An unpinned session keeps its conversation. A pinned one is a kept session that a keeper
/// replaces, so it is unpinned and removed with `claude rm`.
pub async fn stop<E: Effects>(fx: &E, args: &Value, as_: Option<&str>) -> Value {
    let (a, stamp) = (args.clone(), as_.map(str::to_string));
    let checked = blocking(fx, move |fx| -> Result<(String, Object), String> {
        if let Some(reason) = crate::app::caller_refusal(stamp.as_deref(), || fx.edges()) {
            return Err(reason);
        }
        let job = match a.get("job") {
            Some(Value::String(j)) if lineage::is_job(j) => j.clone(),
            _ => return Err("job must be a job id".into()),
        };
        let Some(rec) = read_lineage(fx.paths(), &job) else {
            return Err(format!("job {job} was not started by reflection start_other, so it will not be stopped"));
        };
        if let Some(reason) = caller_refusal_for(fx, a.get(CALLER_PID).unwrap_or(&Value::Null), &rec, "stop") {
            return Err(reason);
        }
        Ok((job, rec))
    })
    .await;
    let (job, rec) = match checked {
        Ok(found) => found,
        Err(reason) => {
            tracing::info!("cx: stop refused — {reason}");
            return json!({"ok": false, "reason": reason});
        }
    };
    // With no daemon a stop cannot reach the session, and `claude rm` on a pinned job would
    // delete the conversation a resume needs.
    if let Err(r) = ensure_daemon(fx.daemon(), "stopped", STOP_ENSURE_TIMEOUT).await {
        tracing::info!("cx: stop refused — {r}");
        return r.answer();
    }
    if truthy(rec.get("pinned")) || pins::pinned(&fx.paths().home).await.contains(&job) {
        return stop_pinned(fx, &job).await;
    }
    let (ok, detail) = run_claude_stop(fx, &job).await;
    if !ok {
        return json!({"ok": false, "job": job, "reason": detail});
    }
    let j = job.clone();
    blocking(fx, move |fx| record_stop(fx.paths(), &j)).await;
    json!({"ok": true, "job": job})
}

/// Unpin `job` and remove it with `claude rm`.
pub async fn stop_pinned<E: Effects>(fx: &E, job: &str) -> Value {
    if let Err(e) = pins::unpin(&fx.paths().home, [job]).await {
        return json!({"ok": false, "job": job, "reason": format!("could not unpin {job}: {e}")});
    }
    let Ok(id) = JobId::parse(job) else {
        return json!({"ok": false, "job": job, "reason": format!("{} is not a job id", py_repr(job))});
    };
    let out = cli::stop_and_remove(&id, &fx.paths().claude_bin, fx.runner(), fx.probe()).await;
    if !out.removed {
        return json!({"ok": false, "job": job, "reason": out.detail});
    }
    json!({"ok": true, "job": job, "removed": true})
}

/// `claude stop <job>`, which keeps the conversation; whether it succeeded, and its output.
pub async fn run_claude_stop<E: Effects>(fx: &E, job: &str) -> (bool, String) {
    let Ok(id) = JobId::parse(job) else { return (false, format!("{} is not a job id", py_repr(job))) };
    let out = cli::stop_keeping(&id, &fx.paths().claude_bin, fx.runner()).await;
    (out.stopped, out.detail)
}

/// Set the job's state record to `stopped`, which `claude stop` leaves as it was.
///
/// The roster drops a stopped job, so the record is all that tells a stop from a death once the
/// job leaves `list`. Reads and writes a file.
pub fn record_stop(paths: &Paths, job: &str) {
    let path = paths.home.job_state(job);
    let mut state = read_json(&path);
    if state.is_empty() {
        return;
    }
    state.insert("state".into(), json!("stopped"));
    let tmp = path.with_file_name(".state.json.cx");
    let written = std::fs::write(&tmp, py_json(&Value::Object(state), 2)).and_then(|_| std::fs::rename(&tmp, &path));
    if let Err(e) = written {
        tracing::warn!("cx: could not record the stop of {job}: {e}");
    }
}

// --- list ----------------------------------------------------------------------

/// Every live session on this node, oldest first: the rows of `list`.
///
/// Background sessions come from the roster, their state records and the lineage records;
/// interactive ones from Claude Code's per-process records, with the tmux session each one sits in.
pub async fn collect<E: Effects>(fx: &E, project: Option<&str>, scope: Option<&str>) -> Vec<Value> {
    let pinned: BTreeSet<String> = pins::pinned(&fx.paths().home).await.into_iter().collect();
    let panes = fx.tmux_panes().await;
    let (project, scope) = (project.map(str::to_string), scope.map(str::to_string));
    blocking(fx, move |fx| collect_rows(fx, &pinned, &panes, project.as_deref(), scope.as_deref())).await
}

/// [`collect`] given the pinned jobs and the tmux panes. Reads files: call it off the executor.
pub fn collect_rows<E: Effects>(
    fx: &E,
    pinned: &BTreeSet<String>,
    panes: &BTreeMap<u32, String>,
    project: Option<&str>,
    scope: Option<&str>,
) -> Vec<Value> {
    let paths = fx.paths();
    let sessions = Sessions::new(paths, fx.procs());
    let lineage = lineage_objects(paths);
    let pending: Vec<Object> = lineage::pending_records(paths).into_iter().map(|(_, r)| Object::from(r)).collect();
    let node = fx.node_name();
    let mut rows: Vec<Object> = Vec::new();
    for s in sessions.load().iter().filter(|s| s.has_record) {
        // The job runs its first prompt before `start` writes its lineage, and that prompt's hook
        // already asks whether the job carries the brief: the pending record answers.
        let rec = lineage.get(&s.short).or_else(|| pending.iter().find(|p| is_late_job_of(s, p)));
        let mut row = job_row(fx, &sessions, s, rec, &node);
        row.insert("pinned".into(), json!(pinned.contains(&s.short)));
        row.insert("recorded".into(), json!(lineage.contains_key(&s.short)));
        rows.push(row);
    }
    for rec in interactive_records(fx) {
        rows.push(interactive_row(fx, &rec, panes, &node));
    }
    let mut out: Vec<Object> = rows
        .into_iter()
        .filter(|r| project.is_none_or(|p| text(r.get("project")) == Some(p)))
        .filter(|r| scope.is_none_or(|s| text(r.get("scope")) == Some(s)))
        .collect();
    out.sort_by(|a, b| text(a.get("started_at")).unwrap_or("").cmp(text(b.get("started_at")).unwrap_or("")));
    out.into_iter().map(Value::Object).collect()
}

fn transcript_of(paths: &Paths, session_id: Option<&str>) -> Value {
    json!(session_id.and_then(|sid| find_transcript(paths, sid)).map(|p| p.display().to_string()))
}

fn job_row<E: Effects>(fx: &E, sessions: &Sessions, s: &Session, rec: Option<&Object>, node: &str) -> Object {
    let paths = fx.paths();
    let empty = Object::new();
    let rec = rec.unwrap_or(&empty);
    let field = |k: &str| rec.get(k).cloned().unwrap_or(Value::Null);
    // The live directory wins over the launch record, so a session that ran `/cd` lists under
    // the scope it is in now.
    let (project, scope) = match s.cwd.as_deref().filter(|c| !c.is_empty()) {
        Some(cwd) => {
            let (p, sc) = project_scope(paths, Some(cwd));
            (json!(p), json!(sc))
        }
        None => (field("project"), field("scope")),
    };
    let record = read_json(&paths.home.job_state(&s.short));
    let state = record.get("state");
    let alive = sessions.is_alive(s);
    let shown = if alive || state == Some(&json!("stopped")) {
        state.filter(|v| truthy(Some(v))).cloned().unwrap_or_else(|| json!("unknown"))
    } else {
        json!("gone")
    };
    let started_at = rec.get("started_at").filter(|v| truthy(Some(v))).cloned().unwrap_or_else(|| json!(iso_ms(s.started_at_ms)));
    let row = json!({
        "job": s.short, "tmux": null, "name": s.name, "node": node,
        "project": project, "scope": scope, "cwd": s.cwd,
        "state": shown,
        "needs": record.get("needs"), "detail": clip(record.get("detail")),
        "tempo": record.get("tempo"),
        "attach": format!("claude attach {}", s.short),
        "parent": field("parent"), "caller": field("caller"),
        "mode": field("mode"), "remote_control": s.remote_control(),
        "remote_control_label": field("remote_control"), "link": s.link(),
        "started_at": started_at,
        "pool": sessions.is_ours(s) || sessions.was_ours(s),
        "brief": truthy(rec.get("brief")),
        "tags": field("tags"),
        "session_id": ids::to_r9(s.session_id.as_deref()),
        "alive": alive,
        "transcript": transcript_of(paths, s.session_id.as_deref()),
    });
    let Value::Object(row) = row else { unreachable!("a json! object") };
    row
}

fn clip(v: Option<&Value>) -> Value {
    json!(text(v).map(|t| t.chars().take(200).collect::<String>()))
}

fn interactive_row<E: Effects>(fx: &E, rec: &Object, panes: &BTreeMap<u32, String>, node: &str) -> Object {
    let paths = fx.paths();
    let (project, scope) = project_scope(paths, text(rec.get("cwd")));
    let pid = rec.get("pid").and_then(Value::as_u64).and_then(|p| u32::try_from(p).ok()).unwrap_or(0);
    let tmux = tmux_session_of(fx.procs(), pid, panes);
    let bridge = text(rec.get("bridgeSessionId")).filter(|b| !b.is_empty());
    let session_id = text(rec.get("sessionId"));
    let row = json!({
        "job": null, "tmux": tmux, "name": rec.get("name"), "node": node,
        "project": project, "scope": scope, "cwd": rec.get("cwd"),
        "state": rec.get("status").filter(|v| truthy(Some(v))).cloned().unwrap_or_else(|| json!("unknown")),
        "needs": null, "detail": null, "tempo": null,
        "attach": tmux.as_ref().map(|t| format!("tmux attach -t {t}")),
        "parent": null, "caller": null, "mode": null,
        "remote_control": truthy(rec.get("bridgeSessionId")), "remote_control_label": null,
        "link": roster::bridge_link(bridge),
        "started_at": rec.get("startedAt").and_then(Value::as_f64).and_then(iso_ms_f),
        "pool": false, "pinned": false, "brief": false,
        "session_id": ids::to_r9(session_id),
        "alive": true,
        "transcript": transcript_of(paths, session_id),
    });
    let Value::Object(row) = row else { unreachable!("a json! object") };
    row
}

/// Project and scope of a directory under the projects tree, else None for each.
pub fn project_scope(paths: &Paths, cwd: Option<&str>) -> (Option<String>, Option<String>) {
    let Some(cwd) = cwd.filter(|c| !c.is_empty()) else { return (None, None) };
    match config::scope_of(Path::new(cwd), &paths.projects_dir()) {
        Some((project, scope)) => (Some(project), scope),
        None => (None, None),
    }
}

/// Live interactive Claude Code sessions, from their per-process records.
fn interactive_records<E: Effects>(fx: &E) -> Vec<Object> {
    let Ok(dir) = std::fs::read_dir(fx.paths().home.sessions()) else { return Vec::new() };
    let mut files: Vec<PathBuf> = dir.flatten().map(|e| e.path()).filter(|p| p.extension().is_some_and(|x| x == "json")).collect();
    files.sort();
    files
        .into_iter()
        .map(|path| read_json(&path))
        .filter(|rec| {
            let pid = rec.get("pid").and_then(Value::as_u64).and_then(|p| u32::try_from(p).ok());
            text(rec.get("kind")) != Some("bg")
                && pid.and_then(|p| fx.procs().start(p)).is_some_and(|live| live == rec.get("procStart").map(py_str).unwrap_or_else(|| "None".into()))
        })
        .collect()
}

/// The tmux session of the pane that holds `pid`, walking up its ancestors.
fn tmux_session_of(procs: &dyn Processes, mut pid: u32, panes: &BTreeMap<u32, String>) -> Option<String> {
    for _ in 0..64 {
        if let Some(name) = panes.get(&pid) {
            return Some(name.clone());
        }
        pid = procs.ppid(pid).unwrap_or(0);
        if pid <= 1 {
            return None;
        }
    }
    None
}

// --- small helpers -------------------------------------------------------------

fn epoch_now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

/// Python's `datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")`.
fn iso(ts: f64) -> String {
    let secs = ts.floor() as i64;
    chrono::DateTime::from_timestamp(secs, 0).map_or_else(String::new, |t| t.format("%Y-%m-%dT%H:%M:%S+00:00").to_string())
}

fn iso_ms(ms: Option<i64>) -> Option<String> {
    ms.filter(|m| *m != 0).map(|m| iso(m as f64 / 1000.0))
}

fn iso_ms_f(ms: f64) -> Option<String> {
    (ms != 0.0).then(|| iso(ms / 1000.0))
}

fn extend(out: &mut Value, more: Value) {
    if let (Value::Object(out), Value::Object(more)) = (out, more) {
        out.extend(more);
    }
}

fn text(v: Option<&Value>) -> Option<&str> {
    v.and_then(Value::as_str)
}

/// Python's truthiness of a JSON value; an absent one is falsy.
fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|f| f != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// Python's `str(v)` for the values a refusal or a record quotes.
fn py_str(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => other.to_string(),
    }
}

/// `re.match` of an anchored pattern, whose `$` also matches before one trailing newline.
fn py_match(re: &Regex, s: &str) -> bool {
    re.is_match(s) || s.strip_suffix('\n').is_some_and(|t| re.is_match(t))
}

/// Python's `str.strip()`: Unicode whitespace and the ASCII separators `\x1c`..`\x1f`.
fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\x1c'..='\x1f').contains(&c))
}

/// Python's `str.isprintable()`, with the common format and private-use characters; unassigned
/// code points pass, which Python would refuse.
fn py_printable(s: &str) -> bool {
    s.chars().all(|c| {
        if c == ' ' {
            return true;
        }
        if c.is_control() || c.is_whitespace() {
            return false;
        }
        !matches!(c as u32,
            0xAD | 0x600..=0x605 | 0x61C | 0x6DD | 0x70F | 0x8E2 | 0x180E | 0x200B..=0x200F
            | 0x202A..=0x202E | 0x2060..=0x2064 | 0x2066..=0x206F | 0xFEFF | 0xFFF9..=0xFFFB
            | 0x110BD | 0x110CD | 0x13430..=0x1343F | 0x1BCA0..=0x1BCA3 | 0x1D173..=0x1D17A
            | 0xE0001 | 0xE0020..=0xE007F | 0xE000..=0xF8FF | 0xF0000..=0xFFFFD | 0x100000..=0x10FFFD
            | 0xFFFE | 0xFFFF)
    })
}

/// `n` random bytes as hex, as Python's `secrets.token_hex(n)`.
fn token_hex(n: usize) -> String {
    uuid::Uuid::new_v4().as_bytes().iter().take(n).map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use awm_harness::claude::cli::{RunError, RunOutput};
    use std::ops::Deref;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Mutex as StdMutex;
    use tempfile::TempDir;

    const PARENT_JOB: &str = "aaaaaaaa";
    const CHILD_JOB: &str = "bbbbbbbb";
    /// The caller: the test process in the Python tests.
    const ME: u32 = 4101;
    /// The started session's REPL: the test process's parent in the Python tests.
    const CHILD_PID: u32 = 4102;
    /// A pid with no process.
    const DEAD: u32 = 4_999_999;
    const BASE_MS: i64 = 1_700_000_000_000;
    const RETIRED: &str = "0e380e38-0e38-0e38-0e38-0e380e380e38";

    fn fit() -> Value {
        json!({"model": "opus", "effort": "high"})
    }

    fn base() -> Value {
        json!({"project": "awm", "scope": "demo", "model": "opus", "effort": "high"})
    }

    /// The objects merged left to right, a later key replacing an earlier one.
    fn merged(parts: &[Value]) -> Value {
        let mut out = Object::new();
        for part in parts {
            out.extend(part.as_object().cloned().unwrap_or_default());
        }
        Value::Object(out)
    }

    fn on(more: Value) -> Value {
        merged(&[base(), more])
    }

    /// A process table with made-up start times; every parent is init.
    struct Table {
        starts: StdMutex<BTreeMap<u32, String>>,
        may: AtomicBool,
    }

    impl Processes for Table {
        fn start(&self, pid: u32) -> Option<String> {
            self.starts.lock().unwrap().get(&pid).cloned()
        }
        fn ppid(&self, _: u32) -> Option<u32> {
            Some(1)
        }
        fn may_be_a_session(&self, _: u32) -> bool {
            self.may.load(Ordering::SeqCst)
        }
    }

    type LaunchFn = Arc<dyn Fn(&Fx, &LaunchSpec) -> Result<Job, LaunchError> + Send + Sync>;
    type GatewayFn = Arc<dyn Fn(&World, &Call) -> Result<Value, String> + Send + Sync>;
    type Hook = Arc<dyn Fn(&World) + Send + Sync>;
    type BridgeHook = Arc<dyn Fn(&World, &str) + Send + Sync>;

    struct Launched {
        spec: LaunchSpec,
        seen_pending: Vec<String>,
    }

    /// A temporary Claude home with a roster the tests add sessions to, and every effect faked.
    struct World {
        _dir: TempDir,
        root: PathBuf,
        paths: Paths,
        procs: Table,
        edges: StdMutex<Option<String>>,
        keep: StdMutex<Result<Value, String>>,
        keep_hook: StdMutex<Option<Hook>>,
        keep_calls: StdMutex<Vec<Duration>>,
        calls: StdMutex<Vec<Call>>,
        gateway: StdMutex<Option<GatewayFn>>,
        runs: StdMutex<Vec<Vec<String>>>,
        stop_answer: StdMutex<(bool, String)>,
        rm_fails: AtomicBool,
        launches: StdMutex<Vec<Launched>>,
        launcher: StdMutex<Option<LaunchFn>>,
        respawns: StdMutex<Vec<(String, RespawnSpec)>>,
        session_id: StdMutex<Option<String>>,
        panes: StdMutex<BTreeMap<u32, String>>,
        bridge_hook: StdMutex<Option<BridgeHook>>,
        link_wait: StdMutex<Duration>,
        lock: Mutex<()>,
    }

    #[derive(Clone)]
    struct Fx(Arc<World>);

    impl Deref for Fx {
        type Target = World;
        fn deref(&self) -> &World {
            &self.0
        }
    }

    impl Fx {
        fn new() -> Fx {
            Self::with(|_, _| {})
        }

        /// A world whose paths `configure` may aim elsewhere first.
        fn with(configure: impl FnOnce(&Path, &mut Paths)) -> Fx {
            let dir = TempDir::new().unwrap();
            let root = dir.path().canonicalize().unwrap();
            let mut paths = Paths::under(&root);
            paths.projects = Some(root.join("projects"));
            paths.mcp_source = Some(root.join("workspace.mcp.json"));
            configure(&root, &mut paths);
            for d in [paths.home.daemon_dir(), paths.home.jobs(), paths.home.sessions(), root.join("projects/awm/demo")] {
                std::fs::create_dir_all(d).unwrap();
            }
            let trust = json!({"projects": {root.display().to_string(): {"hasTrustDialogAccepted": true}}});
            std::fs::write(&paths.home.trust_file, trust.to_string()).unwrap();
            let mcp = json!({"mcpServers": {
                "awm": {"command": "/opt/awm/bin/awm-mcp", "args": [], "env": {"AWM_WORKSPACE": "/ws"}, "extra": "dropped"},
                "other": {"command": "npx"}}});
            std::fs::write(root.join("workspace.mcp.json"), mcp.to_string()).unwrap();
            let starts = [(1, "1"), (ME, "1001"), (CHILD_PID, "1002")].map(|(p, s)| (p, s.to_string()));
            let w = World {
                _dir: dir,
                root,
                paths,
                procs: Table { starts: StdMutex::new(starts.into_iter().collect()), may: AtomicBool::new(false) },
                edges: StdMutex::new(None),
                keep: StdMutex::new(Err("no keeper answers here".into())),
                keep_hook: StdMutex::new(None),
                keep_calls: StdMutex::default(),
                calls: StdMutex::default(),
                gateway: StdMutex::new(None),
                runs: StdMutex::default(),
                stop_answer: StdMutex::new((true, String::new())),
                rm_fails: AtomicBool::new(false),
                launches: StdMutex::default(),
                launcher: StdMutex::new(None),
                respawns: StdMutex::default(),
                session_id: StdMutex::new(None),
                panes: StdMutex::default(),
                bridge_hook: StdMutex::new(None),
                link_wait: StdMutex::new(Duration::ZERO),
                lock: Mutex::const_new(()),
            };
            w.set_workers(json!({}), None);
            Fx(Arc::new(w))
        }
    }

    impl World {
        fn projects(&self) -> PathBuf {
            self.root.join("projects")
        }

        fn proc_start(&self, pid: u32) -> String {
            self.procs.start(pid).unwrap_or_else(|| "0".into())
        }

        fn set_workers(&self, workers: Value, supervisor: Option<u32>) {
            let doc = json!({"supervisorPid": supervisor.unwrap_or(ME), "workers": workers});
            std::fs::write(self.paths.home.roster(), doc.to_string()).unwrap();
        }

        fn roster(&self) -> Object {
            read_json(&self.paths.home.roster())
        }

        fn workers(&self) -> Value {
            self.roster().get("workers").cloned().unwrap_or(json!({}))
        }

        fn add(&self, short: &str, name: &str, pid: u32) {
            self.add_full(short, name, pid, "/home/user", "working", BASE_MS);
        }

        fn add_full(&self, short: &str, name: &str, pid: u32, cwd: &str, state: &str, started_ms: i64) {
            let mut workers = self.workers();
            workers[short] = json!({
                "replPid": pid, "replProcStart": self.proc_start(pid), "startedAt": started_ms,
                "cliVersion": "2.1.268", "sessionId": format!("sid-{pid}"),
                "dispatch": {"source": "shell", "seed": {"name": name}},
            });
            let supervisor = self.roster().get("supervisorPid").and_then(Value::as_u64).map(|p| p as u32);
            self.set_workers(workers, supervisor);
            std::fs::create_dir_all(self.paths.home.jobs().join(short)).unwrap();
            let rec = json!({"name": name, "tokens": 0, "intent": "", "cwd": cwd, "state": state, "needs": null});
            std::fs::write(self.paths.home.job_state(short), rec.to_string()).unwrap();
        }

        /// Claude Code's per-process record for a live pid.
        fn record(&self, pid: u32, kind: &str, job: Option<&str>, name: &str) {
            let rec = json!({
                "pid": pid, "procStart": self.proc_start(pid), "kind": kind, "jobId": job, "name": name,
                "sessionId": format!("sid-{pid}"), "cwd": "/home/user", "status": "idle", "startedAt": 1_650_000_000_000_i64,
            });
            std::fs::write(self.paths.home.session_record(pid), rec.to_string()).unwrap();
        }

        fn edit_json(&self, path: &Path, edit: impl FnOnce(&mut Value)) {
            let mut doc: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
            edit(&mut doc);
            std::fs::write(path, doc.to_string()).unwrap();
        }

        fn lineage(&self, job: &str, over: Value) -> Object {
            ensure_starts_dir(&self.paths).unwrap();
            let rec = merged(&[
                json!({"job": job, "name": job, "project": "awm", "scope": "demo", "cwd": "/x",
                       "parent": null, "caller": "local", "mode": "worker",
                       "remote_control": null, "started_at": "2026-01-01T00:00:00+00:00"}),
                over,
            ]);
            write_json(&self.paths.lineage_path(job), &rec).unwrap();
            rec.as_object().cloned().unwrap()
        }

        /// The test process as a background session, optionally cx-started.
        fn parent_session(&self, mode: Option<&str>) {
            self.add(PARENT_JOB, "caller", ME);
            self.record(ME, "bg", Some(PARENT_JOB), "caller");
            if let Some(mode) = mode {
                self.lineage(PARENT_JOB, json!({"mode": mode, "name": "caller"}));
            }
        }

        fn pendings(&self) -> Vec<String> {
            let mut out: Vec<String> = std::fs::read_dir(self.paths.starts_dir())
                .map(|d| d.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect())
                .unwrap_or_default();
            out.retain(|n| n.starts_with("pending-"));
            out.sort();
            out
        }

        fn set_bridge(&self, short: &str, bridge: &str) {
            self.edit_json(&self.paths.home.job_state(short), |r| r["bridgeSessionId"] = json!(bridge));
        }

        fn state(&self, short: &str) -> Value {
            Value::Object(read_json(&self.paths.home.job_state(short)))
        }

        fn job(&self, short: &str) -> Job {
            roster::load(&self.paths.home).into_iter().find(|s| s.short == short).unwrap()
        }

        fn fleet(&self, nodes: &[(&str, &str)], edges: &[(&str, &[&str])]) {
            let mut yaml = String::from("nodes:\n");
            for (n, role) in nodes {
                yaml.push_str(&format!("  {n}: {{role: {role}}}\n"));
            }
            yaml.push_str("edges:\n");
            for (src, dsts) in edges {
                let targets: Vec<String> = dsts.iter().map(|d| format!("{d}: lan")).collect();
                yaml.push_str(&format!("  {src}: {{{}}}\n", targets.join(", ")));
            }
            *self.edges.lock().unwrap() = Some(yaml);
        }

        /// nodea reaches this node; nodeb is in the fleet but holds no edge here. The edge check
        /// names this node by the SDK's `here`, the host the tests run on.
        fn peers(&self) {
            let here = awm_plugin_sdk::config::here();
            self.fleet(&[(&here, "homebase"), ("nodea", "homebase"), ("nodeb", "homebase")], &[
                ("nodea", &[&here]),
                (&here, &["nodeb"]),
            ]);
        }

        /// Make `job`'s lineage record an hour old.
        fn age(&self, job: &str) -> PathBuf {
            let path = self.paths.lineage_path(job);
            let old = SystemTime::now() - Duration::from_secs(3600);
            std::fs::File::options().write(true).open(&path).unwrap().set_modified(old).unwrap();
            path
        }

        fn launched(&self) -> Vec<LaunchSpec> {
            self.launches.lock().unwrap().iter().map(|l| l.spec.clone()).collect()
        }

        fn flags(&self, i: usize) -> Vec<String> {
            self.launched()[i].flags.clone()
        }

        /// The jobs `claude stop` ran on.
        fn stops(&self) -> Vec<String> {
            self.runs.lock().unwrap().iter().filter(|r| r[0] == "stop").map(|r| r[1].clone()).collect()
        }

        fn on_launch(&self, f: impl Fn(&Fx, &LaunchSpec) -> Result<Job, LaunchError> + Send + Sync + 'static) {
            *self.launcher.lock().unwrap() = Some(Arc::new(f));
        }

        fn fail_launch(&self, timeout: bool) {
            self.on_launch(move |_, _| {
                Err(if timeout { LaunchError::Timeout("late".into()) } else { LaunchError::Spawn(std::io::Error::other("exec failed")) })
            });
        }

        fn mode_of(&self, pid: i64) -> Option<String> {
            lineage::mode_of(&self.paths, &self.procs, pid).as_wire().map(str::to_string)
        }
    }

    fn register(fx: &Fx, short: &str, spec: &LaunchSpec) -> Job {
        fx.add_full(short, &spec.name, CHILD_PID, &spec.cwd.display().to_string(), "working", BASE_MS);
        fx.job(short)
    }

    impl DaemonGate for World {
        async fn pid(&self) -> Option<u32> {
            let sup = self.roster().get("supervisorPid").and_then(Value::as_u64).map(|p| p as u32)?;
            self.procs.start(sup).map(|_| sup)
        }

        async fn ensure(&self, timeout: Duration) -> Result<Value, String> {
            self.keep_calls.lock().unwrap().push(timeout);
            let hook = self.keep_hook.lock().unwrap().clone();
            if let Some(hook) = hook {
                hook(self);
            }
            self.keep.lock().unwrap().clone()
        }
    }

    impl Gateway for World {
        async fn call(&self, call: Call) -> Result<Value, String> {
            self.calls.lock().unwrap().push(call.clone());
            let hook = self.gateway.lock().unwrap().clone();
            match hook {
                Some(f) => f(self, &call),
                None => Ok(json!({})),
            }
        }
    }

    impl Runner for World {
        async fn run(&self, argv: &[String], _: Duration) -> Result<RunOutput, RunError> {
            let args = argv[1..].to_vec();
            self.runs.lock().unwrap().push(args.clone());
            Ok(match args[0].as_str() {
                "stop" => {
                    let (success, output) = self.stop_answer.lock().unwrap().clone();
                    RunOutput { success, output: output.into_bytes() }
                }
                "rm" if self.rm_fails.load(Ordering::SeqCst) => RunOutput { success: false, output: b"busy".to_vec() },
                _ => RunOutput { success: true, output: Vec::new() },
            })
        }
    }

    /// A failed `rm` leaves the job in place.
    impl Probe for World {
        fn worker_alive(&self, _: &JobId) -> bool {
            false
        }
        fn present(&self, _: &JobId) -> bool {
            self.rm_fails.load(Ordering::SeqCst)
        }
    }

    impl Effects for Fx {
        type Daemon = World;
        type Gateway = World;
        type Runner = World;
        type Probe = World;

        fn paths(&self) -> &Paths {
            &self.0.paths
        }
        fn procs(&self) -> &(dyn Processes + Sync) {
            &self.0.procs
        }
        fn daemon(&self) -> &World {
            &self.0
        }
        fn gateway(&self) -> &World {
            &self.0
        }
        fn runner(&self) -> &World {
            &self.0
        }
        fn probe(&self) -> &World {
            &self.0
        }

        /// Records the call and registers the session under the child's pid, as the daemon would.
        async fn launch(&self, spec: LaunchSpec, _accept: Accept) -> Result<Job, LaunchError> {
            let seen_pending = self.pendings();
            self.launches.lock().unwrap().push(Launched { spec: spec.clone(), seen_pending });
            let custom = self.launcher.lock().unwrap().clone();
            let mut job = match custom {
                Some(f) => f(self, &spec)?,
                None => register(self, CHILD_JOB, &spec),
            };
            if let Some(sid) = self.session_id.lock().unwrap().clone() {
                job.session_id = Some(sid);
            }
            Ok(job)
        }

        /// Refuses a live job as `claude respawn` does, else brings it back under the child's pid.
        async fn respawn(&self, job: &str, spec: RespawnSpec) -> Result<Job, LaunchError> {
            let sessions = Sessions::new(&self.paths, &self.procs);
            if sessions.load().iter().any(|s| s.short == job && sessions.is_alive(s)) {
                return Err(LaunchError::Refused(format!("job {job} is running")));
            }
            self.respawns.lock().unwrap().push((job.to_string(), spec));
            self.add(job, "seat - x", CHILD_PID);
            Ok(self.job(job))
        }

        async fn tmux_panes(&self) -> BTreeMap<u32, String> {
            self.panes.lock().unwrap().clone()
        }

        fn node_name(&self) -> String {
            "testnode".into()
        }

        fn edges(&self) -> anyhow::Result<EdgeList> {
            match self.edges.lock().unwrap().as_deref() {
                Some(yaml) => EdgeList::parse(yaml),
                None => Ok(EdgeList::default()),
            }
        }

        fn link_wait(&self) -> Duration {
            *self.link_wait.lock().unwrap()
        }

        fn job_bridge_id(&self, job: &str) -> Option<String> {
            let hook = self.bridge_hook.lock().unwrap().clone();
            if let Some(hook) = hook {
                hook(self, job);
            }
            roster::job_bridge_id(&self.paths.home, job)
        }

        fn start_lock(&self) -> &Mutex<()> {
            &self.lock
        }
    }

    async fn start_fit(w: &Fx, args: Value) -> Value {
        start(w, &merged(&[fit(), args]), None).await
    }

    async fn start_as(w: &Fx, args: Value, as_: &str) -> Value {
        start(w, &merged(&[fit(), args]), Some(as_)).await
    }

    async fn stop_job(w: &Fx, args: Value) -> Value {
        stop(w, &args, None).await
    }

    fn system_prompt(spec: &LaunchSpec) -> Option<String> {
        let found: Vec<&String> = spec.flags.iter().filter(|f| f.starts_with("--append-system-prompt=")).collect();
        assert!(found.len() <= 1);
        found.first().map(|f| f.split_once('=').unwrap().1.to_string())
    }

    fn reason(out: &Value) -> &str {
        out["reason"].as_str().unwrap_or_default()
    }

    fn refused(out: &Value) -> bool {
        out["ok"] == json!(false)
    }

    // --- a successful start ------------------------------------------------------

    #[tokio::test]
    async fn test_start_returns_the_contract_and_writes_lineage() {
        let w = Fx::new();
        w.parent_session(None);
        let out = start_fit(&w, on(json!({"prompt": "do it", "mode": "rep", CALLER_PID: ME}))).await;
        assert!(out["link_reason"].is_string());
        let expected = json!({
            "ok": true, "job": CHILD_JOB, "session_id": null, "name": "demo", "node": "testnode",
            "project": "awm", "scope": "demo", "lobby": false,
            "cwd": w.projects().join("awm/demo").display().to_string(),
            "attach": format!("claude attach {CHILD_JOB}"), "parent": PARENT_JOB, "mode": "rep",
            "pinned": false, "link": null, "link_reason": out["link_reason"],
        });
        assert_eq!(out, expected);
        let rec = read_lineage(&w.paths, CHILD_JOB).unwrap();
        assert_eq!((rec["parent"].as_str(), rec["caller"].as_str()), (Some(PARENT_JOB), Some("local")));
        assert_eq!((rec["mode"].as_str(), rec["project"].as_str(), rec["scope"].as_str()), (Some("rep"), Some("awm"), Some("demo")));
        assert!(rec["remote_control"] == json!("demo") && truthy(rec.get("started_at")));
        assert!(w.launched()[0].prompt.as_deref().unwrap().contains("\n\ndo it\n\n"));
    }

    #[tokio::test]
    async fn test_start_calls_the_shared_launcher_with_cx_settings() {
        let w = Fx::new();
        start_fit(&w, base()).await;
        let call = &w.launched()[0];
        assert!(call.name == "demo" && call.cwd == w.projects().join("awm/demo"));
        assert!(call.env.is_empty(), "the model is a flag, not ANTHROPIC_MODEL");
        assert_eq!(call.unit_prefix, "awm-cx-start");
        assert_eq!(call.claude, w.paths.claude_bin);
    }

    #[tokio::test]
    async fn test_every_valued_flag_uses_the_equals_form() {
        let w = Fx::new();
        start_fit(&w, on(json!({"permission": "auto", "disallowed_tools": "Bash", "remote_control": "rc1", "brief": false}))).await;
        let flags = w.flags(0);
        for flag in &flags {
            assert!(flag.starts_with("--") && (flag.matches('=').count() == 1 || !flag.contains('=')));
        }
        for bare in ["--effort", "--model", "--permission-mode", "--disallowedTools", "--remote-control"] {
            assert!(!flags.contains(&bare.to_string()));
        }
    }

    // --- refusals ----------------------------------------------------------------

    #[tokio::test]
    async fn test_a_station_refuses() {
        let w = Fx::new();
        w.fleet(&[("testnode", "station")], &[]);
        let out = start_fit(&w, base()).await;
        assert!(refused(&out) && reason(&out).contains("station"));
        assert!(w.launched().is_empty());
    }

    #[tokio::test]
    async fn test_a_peer_that_reaches_here_may_start_and_is_recorded_as_the_caller() {
        let w = Fx::new();
        w.peers();
        let out = start_as(&w, base(), "peer:nodea").await;
        assert_eq!(out["ok"], json!(true), "{out}");
        assert_eq!(read_lineage(&w.paths, CHILD_JOB).unwrap()["caller"], json!("nodea"));
    }

    #[tokio::test]
    async fn test_no_daemon_refuses() {
        let w = Fx::new();
        w.set_workers(json!({}), Some(DEAD));
        let out = start_fit(&w, base()).await;
        assert!(refused(&out) && reason(&out).contains("daemon"));
        assert!(w.launched().is_empty());
    }

    #[tokio::test]
    async fn test_an_untrusted_worktree_refuses() {
        let w = Fx::new();
        std::fs::write(&w.paths.home.trust_file, json!({"projects": {}}).to_string()).unwrap();
        let out = start_fit(&w, base()).await;
        assert!(refused(&out) && reason(&out).contains("trust"));
        assert!(w.launched().is_empty());
    }

    #[tokio::test]
    async fn test_remote_control_cannot_inject_flags() {
        for rc in [json!("--evil"), json!("-x"), json!("a b"), json!("x".repeat(65)), json!(""), json!(" "), json!("a;b"), json!(5)] {
            let w = Fx::new();
            let out = start_fit(&w, on(json!({"remote_control": rc}))).await;
            if rc == json!("") || rc == json!(" ") {
                // An empty value is "off" or refused; either way nothing is injected.
                assert!(!w.launched().iter().any(|l| l.flags.iter().any(|f| f == "--remote-control=")));
                continue;
            }
            assert!(refused(&out) && w.launched().is_empty(), "{rc}");
        }
    }

    #[tokio::test]
    async fn test_a_start_with_remote_control_returns_the_link_once_the_bridge_appears() {
        let w = Fx::new();
        *w.link_wait.lock().unwrap() = Duration::from_secs(5);
        let polls = Arc::new(StdMutex::new(Vec::<String>::new()));
        let p = polls.clone();
        *w.bridge_hook.lock().unwrap() = Some(Arc::new(move |w: &World, short: &str| {
            let mut polls = p.lock().unwrap();
            polls.push(short.to_string());
            if polls.len() == 3 {
                w.set_bridge(short, "cse_01Link");
            }
        }));
        let out = start_fit(&w, on(json!({"remote_control": true}))).await;
        assert_eq!(out["link"], json!("https://claude.ai/code/session_01Link"));
        assert!(polls.lock().unwrap().len() == 3 && out.get("link_reason").is_none());
    }

    // --- a launch that outlives its timeout (M3) ---------------------------------

    #[tokio::test]
    async fn test_the_pending_record_exists_during_the_launch_and_is_replaced_after() {
        let w = Fx::new();
        let out = start_fit(&w, on(json!({"mode": "representative"}))).await;
        assert_eq!(w.launches.lock().unwrap()[0].seen_pending.len(), 1, "written before the launch");
        assert!(w.pendings().is_empty(), "dropped once the job record is written");
        assert_eq!(read_lineage(&w.paths, out["job"].as_str().unwrap()).unwrap()["mode"], json!("representative"));
    }

    fn now_ms() -> i64 {
        (epoch_now() * 1000.0) as i64
    }

    #[tokio::test]
    async fn test_adopt_pending_gives_the_late_job_its_record() {
        let w = Fx::new();
        w.fail_launch(true);
        start_fit(&w, on(json!({"mode": "representative"}))).await;
        assert!(adopt_pending(&w, None).is_empty(), "no job has appeared yet");
        w.add_full(CHILD_JOB, "demo", CHILD_PID, "/home/user", "working", now_ms());
        assert_eq!(adopt_pending(&w, None), [CHILD_JOB]);
        assert!(w.pendings().is_empty());
        assert_eq!(read_lineage(&w.paths, CHILD_JOB).unwrap()["mode"], json!("representative"));
        assert_eq!(w.mode_of(CHILD_PID.into()).as_deref(), Some("representative"));
    }

    // --- stop --------------------------------------------------------------------

    #[tokio::test]
    async fn test_a_moded_session_stops_only_its_own_children() {
        let w = Fx::new();
        w.parent_session(Some("representative"));
        w.lineage(CHILD_JOB, json!({"parent": "zzzzzzzz"}));
        let out = stop_job(&w, json!({"job": CHILD_JOB, CALLER_PID: ME})).await;
        assert!(refused(&out) && reason(&out).contains("started"));
        w.lineage(CHILD_JOB, json!({"parent": PARENT_JOB}));
        assert_eq!(stop_job(&w, json!({"job": CHILD_JOB, CALLER_PID: ME})).await["ok"], json!(true));
        assert_eq!(w.stops(), [CHILD_JOB]);
    }

    #[tokio::test]
    async fn test_stop_refuses_a_caller_whose_mode_is_unknown() {
        let w = Fx::new();
        // The Python test's caller had an ancestor that may be a session; here the caller may be one.
        w.procs.may.store(true, Ordering::SeqCst);
        w.lineage(CHILD_JOB, json!({}));
        std::fs::write(w.paths.home.roster(), "{ not json").unwrap();
        let out = stop_job(&w, json!({"job": CHILD_JOB, CALLER_PID: ME})).await;
        assert!(refused(&out) && reason(&out).contains("determined"));
        assert!(w.stops().is_empty());
    }

    /// `claude stop` keeps the conversation; `claude rm` would not.
    #[tokio::test]
    async fn test_stop_never_deletes_the_conversation() {
        let w = Fx::new();
        assert_eq!(run_claude_stop(&w, CHILD_JOB).await, (true, String::new()));
        assert_eq!(*w.runs.lock().unwrap(), [vec!["stop".to_string(), CHILD_JOB.to_string()]]);
    }

    // --- the caller's mode gates what it may start -------------------------------

    #[tokio::test]
    async fn test_a_restricted_start_passes_the_reporting_settings_and_cannot_offer_bypass() {
        let w = Fx::with(|root, p| p.restricted_settings = root.join("reporting-settings.json"));
        std::fs::write(&w.paths.restricted_settings, "{}").unwrap();
        start_fit(&w, on(json!({"restricted": true, "permission": "dontAsk"}))).await;
        let flags = w.flags(0);
        assert!(flags.contains(&format!("--settings={}", w.paths.restricted_settings.display())));
        assert!(!flags.contains(&"--allow-dangerously-skip-permissions".into()), "the CLI refuses bypass under --restricted");
    }

    #[tokio::test]
    async fn test_a_strict_start_loads_only_the_awm_server_from_the_workspace_config() {
        let w = Fx::new();
        start_fit(&w, on(json!({"strict_mcp": true}))).await;
        let flags = w.flags(0);
        let path = flags.iter().find_map(|f| f.strip_prefix("--mcp-config=")).unwrap();
        assert_eq!(Value::Object(read_json(Path::new(path))), json!({"mcpServers": {"awm": {
            "command": "/opt/awm/bin/awm-mcp", "args": [], "env": {"AWM_WORKSPACE": "/ws"}}}}));
    }

    #[tokio::test]
    async fn test_a_caller_whose_mode_is_unknown_may_not_start() {
        let w = Fx::new();
        w.parent_session(None);
        std::fs::write(w.paths.home.roster(), "{ not json").unwrap();
        let out = start_fit(&w, on(json!({CALLER_PID: ME}))).await;
        assert!(refused(&out) && reason(&out).contains("determined"));
        assert!(w.launched().is_empty());
    }

    // --- the reconcile prune -----------------------------------------------------

    #[tokio::test]
    async fn test_prune_drops_lineage_only_after_the_job_is_removed() {
        let w = Fx::new();
        start_fit(&w, base()).await;
        let path = w.age(CHILD_JOB);
        w.set_workers(json!({}), None);
        assert!(prune_lineage(&w, None).is_empty(), "stopped, but its conversation remains");
        assert!(path.exists());
        std::fs::remove_file(w.paths.home.job_state(CHILD_JOB)).unwrap();
        assert_eq!(prune_lineage(&w, None), [CHILD_JOB]);
        assert!(!path.exists());
    }

    // --- mode_of: three answers --------------------------------------------------

    #[tokio::test]
    async fn test_mode_of_is_none_only_for_a_pid_that_is_positively_not_cx_started() {
        let w = Fx::new();
        start_fit(&w, base()).await;
        w.add("dddddddd", "<warm otter>", ME);
        assert_eq!(w.mode_of(ME.into()), None, "a pool session has no lineage");
        w.record(1, "interactive", None, "x");
        assert_eq!(w.mode_of(1), None, "an interactive terminal");
    }

    #[tokio::test]
    async fn test_mode_of_is_unknown_when_a_cx_launched_job_lost_its_lineage() {
        let w = Fx::new();
        start_fit(&w, base()).await;
        let mut workers = w.workers();
        workers[CHILD_JOB]["dispatch"]["launch"] = json!({"args": ["--permission-mode=dontAsk"]});
        w.set_workers(workers, None);
        std::fs::remove_dir_all(w.paths.starts_dir()).unwrap();
        assert_eq!(w.mode_of(CHILD_PID.into()).as_deref(), Some("unknown"));
    }

    // --- the lifecycle brief ----------------------------------------------------

    // --- the parent comes from a verified session record -------------------------

    // --- list --------------------------------------------------------------------

    fn by_key(rows: &[Value]) -> Vec<(String, Value)> {
        rows.iter().map(|r| (r["job"].as_str().or(r["tmux"].as_str()).unwrap_or_default().to_string(), r.clone())).collect()
    }

    #[tokio::test]
    async fn test_list_merges_roster_lineage_and_interactive_sessions() {
        let w = Fx::new();
        w.add_full(PARENT_JOB, "<warm saiga>", ME, "/home/user", "working", 1_600_000_000_000);
        start_fit(&w, on(json!({"mode": "rep"}))).await;
        w.record(ME, "interactive", None, "dev");
        let cwd = w.projects().join("awm/demo").display().to_string();
        w.edit_json(&w.paths.home.session_record(ME), |r| r["cwd"] = json!(cwd));
        *w.panes.lock().unwrap() = BTreeMap::from([(ME, "main".to_string())]);

        let rows = by_key(&collect(&w, None, None).await);
        let get = |k: &str| rows.iter().find(|(key, _)| key == k).map(|(_, r)| r.clone()).unwrap();
        let pool_row = get(PARENT_JOB);
        assert!(pool_row["pool"] == json!(true) && pool_row["mode"].is_null());
        let child = get(CHILD_JOB);
        assert!(child["pool"] == json!(false) && child["mode"] == json!("rep"));
        assert!(child["parent"].is_null() && child["caller"] == json!("local"));
        assert!(child["project"] == json!("awm") && child["scope"] == json!("demo"));
        assert!(child["attach"] == json!(format!("claude attach {CHILD_JOB}")) && child["node"] == json!("testnode"));
        let tmux = get("main");
        assert!(tmux["attach"] == json!("tmux attach -t main") && tmux["job"].is_null());
        assert!(tmux["name"] == json!("dev") && tmux["scope"] == json!("demo"));
        let order: Vec<&str> = rows.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(order, [PARENT_JOB, "main", CHILD_JOB], "oldest first");
    }

    // --- pins --------------------------------------------------------------------

    fn pins_file(w: &World) -> Value {
        let path = w.paths.home.pins();
        if path.exists() { serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap() } else { json!([]) }
    }

    // --- resume ------------------------------------------------------------------

    async fn resume_job(w: &Fx, args: Value) -> Value {
        resume(w, &args, None).await
    }

    /// A job cx started that has since stopped: lineage and state, no roster entry.
    fn stopped_job(w: &World, job: &str, transcript: bool, over: Value) {
        w.lineage(job, merged(&[json!({"name": "seat - x"}), over]));
        std::fs::create_dir_all(w.paths.home.jobs().join(job)).unwrap();
        let state = json!({"name": "seat - x", "sessionId": format!("{job}-sid"), "state": "done"});
        std::fs::write(w.paths.home.job_state(job), state.to_string()).unwrap();
        if transcript {
            let folder = w.paths.transcripts_dir().join("-some-cwd");
            std::fs::create_dir_all(&folder).unwrap();
            std::fs::write(folder.join(format!("{job}-sid.jsonl")), "{}\n").unwrap();
        }
    }

    fn respawned(w: &World) -> Vec<(String, RespawnSpec)> {
        w.respawns.lock().unwrap().clone()
    }

    #[tokio::test]
    async fn test_resume_respawns_the_same_job_and_re_pins_it() {
        let w = Fx::new();
        stopped_job(&w, CHILD_JOB, true, json!({"pinned": true}));
        let out = resume_job(&w, json!({"job": CHILD_JOB})).await;
        assert!(out["ok"] == json!(true) && out["job"] == json!(CHILD_JOB) && out["pinned"] == json!(true));
        assert!(out["name"] == json!("seat - x") && out["link"].is_null());
        assert_eq!(respawned(&w)[0].0, CHILD_JOB);
        assert_eq!(respawned(&w)[0].1.unit_prefix, "awm-cx-resume");
        assert_eq!(pins_file(&w), json!([CHILD_JOB]));
    }

    /// A launcher that registers a new job for a resume from the transcript.
    fn relaunched(w: &World) {
        w.on_launch(|fx, spec| Ok(register(fx, "feedface", spec)));
    }

    fn retired_transcript(w: &World, cwd: &Path) {
        let slug: String = cwd.display().to_string().chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect();
        let folder = w.paths.transcripts_dir().join(slug);
        std::fs::create_dir_all(&folder).unwrap();
        let lines = format!(
            "{}\n{}\n",
            json!({"type": "user", "cwd": cwd.display().to_string()}),
            json!({"type": "user", "cwd": "/somewhere/it/moved"})
        );
        std::fs::write(folder.join(format!("{RETIRED}.jsonl")), lines).unwrap();
    }

    #[tokio::test]
    async fn test_a_retired_session_resumes_from_its_transcript_in_a_new_job() {
        let w = Fx::new();
        relaunched(&w);
        let cwd = w.projects().join("awm/demo");
        retired_transcript(&w, &cwd);
        let out = resume_job(&w, json!({"job": RETIRED, "prompt": "carry on"})).await;
        assert!(out["ok"] == json!(true) && out["from_transcript"] == json!(true) && out["job"] == json!("feedface"), "{out}");
        assert_eq!(out["session_id"], json!(format!("cc{}", RETIRED.replace('-', ""))));
        let call = &w.launched()[0];
        assert!(call.flags.contains(&format!("--resume={RETIRED}")) && call.cwd == cwd);
        assert!(call.prompt.as_deref() == Some("carry on") && call.name == "resumed-0e380e38");
        let rec = read_lineage(&w.paths, "feedface").unwrap();
        assert_eq!(rec["session_id"], json!(RETIRED));
        // The gateway gates a session with no mode in its record as `unknown` and refuses its calls.
        assert_eq!(rec["mode"], json!("worker"));
    }

    fn keep_answers(w: &World) {
        *w.keep.lock().unwrap() = Ok(json!({"ok": false, "reason": "daemon_down", "action": "held"}));
    }

    #[tokio::test]
    async fn test_resume_without_a_daemon_asks_keep_and_says_daemon_down() {
        let w = Fx::new();
        keep_answers(&w);
        stopped_job(&w, CHILD_JOB, true, json!({}));
        w.set_workers(json!({}), Some(DEAD));
        let out = resume_job(&w, json!({"job": CHILD_JOB})).await;
        assert!(refused(&out) && out["code"] == json!("daemon_down") && respawned(&w).is_empty());
        assert_eq!(*w.keep_calls.lock().unwrap(), [ENSURE_TIMEOUT]);
        assert!(reason(&out).ends_with("(held: daemon_down)"));
    }

    fn transcript(w: &World, session_id: &str, turns: &[(&str, &str)]) {
        let folder = w.paths.transcripts_dir().join("-proj");
        std::fs::create_dir_all(&folder).unwrap();
        let lines: String = turns.iter().map(|(role, text)| format!("{}\n", json!({"type": role, "message": {"content": text}}))).collect();
        std::fs::write(folder.join(format!("{session_id}.jsonl")), lines).unwrap();
    }

    #[tokio::test]
    async fn test_a_replacing_start_carries_the_old_tail_and_retires_the_old_job() {
        let w = Fx::new();
        stopped_job(&w, "dddddddd", false, json!({}));
        transcript(&w, "dddddddd-sid", &[("user", "where were we"), ("assistant", "on step 3")]);
        let out = start_fit(&w, on(json!({"prompt": "carry on", "replaces": "dddddddd"}))).await;
        assert_eq!(out["ok"], json!(true), "{out}");
        let prompt = w.launched()[0].prompt.clone().unwrap();
        assert!(prompt.contains("carry on") && prompt.contains("job dddddddd") && prompt.contains("on step 3"));
        assert_eq!(w.stops(), ["dddddddd"]);
        assert_eq!(w.state("dddddddd")["state"], json!("stopped"));
        assert_eq!(read_lineage(&w.paths, CHILD_JOB).unwrap()["replaces"], json!("dddddddd"));
    }

    // --- the lobby (test_lobby.py) -------------------------------------------------

    fn lobby_world() -> Fx {
        Fx::with(|root, p| p.lobby_dir = root.join("lobby"))
    }

    #[tokio::test]
    async fn test_no_scope_starts_in_the_lobby() {
        let w = lobby_world();
        let out = start_fit(&w, json!({"prompt": "route yourself"})).await;
        assert!(out["ok"] == json!(true) && out["lobby"] == json!(true), "{out}");
        assert_eq!(out["cwd"], json!(w.paths.lobby_dir.display().to_string()));
        assert!(out["project"].is_null() && out["scope"].is_null());
        assert!(out["name"].as_str().unwrap().starts_with("lobby-"));
        let text = system_prompt(&w.launched()[0]).unwrap();
        assert!(text.contains("You started in the lobby") && text.contains("Find your home first"));
        assert!(text.contains("new_project:true"));
    }

    // --- the plan's fixes and invariants -------------------------------------------

    /// A lineage record is what Python's `json.dumps(record, indent=1)` wrote, mode 0600, and a
    /// pending one is filed under `sha256(name)[:16]`.
    #[test]
    fn lineage_records_keep_the_python_format() {
        let dir = TempDir::new().unwrap();
        let paths = Paths::under(dir.path());
        let rec = json!({"name": "café/w", "mode": "worker", "tags": [], "suggested": {}, "parent": null, "pinned": false})
            .as_object()
            .cloned()
            .unwrap();
        write_lineage(&paths, CHILD_JOB, &rec).unwrap();
        let path = paths.lineage_path(CHILD_JOB);
        assert_eq!(
            std::fs::read_to_string(&path).unwrap(),
            "{\n \"name\": \"caf\\u00e9/w\",\n \"mode\": \"worker\",\n \"tags\": [],\n \"suggested\": {},\n \"parent\": null,\n \"pinned\": false\n}"
        );
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        assert!(!path.with_extension("tmp").exists());
        write_pending(&paths, &rec).unwrap();
        let expected = paths.starts_dir().join("pending-cbdf06e4a340e910.json");
        assert!(expected.exists(), "{}", expected.display());
        assert_eq!(read_json(&expected)["pending"], json!(true));
        assert_eq!(py_json(&json!({"x": "\u{1F600}"}), 1), "{\n \"x\": \"\\ud83d\\ude00\"\n}");
    }

}
