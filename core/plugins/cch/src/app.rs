//! The live objects behind the verb table: the [`Backend`] every verb acts through, [`Live`] over
//! this host's sessions, daemon, warm pool and containers, and the boot that migrates the old
//! state, re-arms the follow-ups an earlier process still owed and starts the background loops.

use std::collections::BTreeSet;
use std::future::Future;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use awm_harness::claude::cli::{self, JobProbe, Probe, ProcessRunner, Runner, StopOutcome};
use awm_harness::claude::launch::LaunchError;
use awm_harness::claude::roster::{self, JobId};
use awm_harness::claude::ClaudeHome;
use awm_harness::contract::Reporter;
use awm_plugin_sdk::config::{self, EdgeList};
use awm_plugin_sdk::{spawn_supervised, CallOpts, Hub};
use serde_json::{json, Value};

use crate::claim::DaemonKeys;
use crate::control::{self, ControlError, RemoveJob, Remover, Target};
use crate::isolated::{self, Isolated};
use crate::keeper::{self, Keeper};
use crate::lane::{Host, LaneError};
use crate::lifecycle::{self, DaemonGate, Effects as _};
use crate::lineage::{self, Mode, Procfs};
use crate::memwatch::{self, MemWatch, Notify, SystemNode};
use crate::paths::Paths;
use crate::permission_mode::{self, ModeResult};
use crate::pool::{self, LiveTerminals, Pool};
use crate::promises::{Pending, Promises};
use crate::reconcile::{self, Bookkeeping, LivePool};
use crate::resume::{self, LiveLiveness, LiveWatch, LiveWorld, Resumer};
use crate::seed::{self, LiveLauncher};
use crate::sender::{SendError, SendRequest, Sender};
use crate::sessions::Sessions;
use crate::transcript::Tail;
use crate::verbs::{py_repr, App};
use crate::watcher::{self, LiveProbe};
use crate::{claim, hooks_reconcile, migrate, remove, session_events};

/// How long a supervised loop waits before it is started again after it died.
pub const RESPAWN: Duration = Duration::from_secs(5);
/// The user unit the Python cch synced isolated logins with; the watch in-process replaces it.
pub const LOGIN_SYNC_UNIT: &str = "awm-isolated-login-sync.service";
const SYSTEMCTL_TIMEOUT: Duration = Duration::from_secs(30);
/// How long a relaunched job gets to bring its Remote Control bridge up before its recorded
/// switch is compared with it.
pub const RC_SETTLE: Duration = Duration::from_secs(20);

/// One control verb, as the verb table hands it to [`Backend::control`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Op {
    Message { text: String, confirm: bool },
    Interrupt,
    Model(String),
    Effort(String),
    Rename(String),
    Cd { path: String, brief: String },
    RemoteControl { on: bool, name: Option<String> },
    Mcp { action: String, server: String },
    Link,
    /// Stop the session. Without `keep` its job is removed; with it the job stays to resume.
    Stop { confirm: bool, keep: bool },
}

/// A lifecycle verb, as the verb table hands it to [`Backend::lifecycle`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Life {
    Start,
    Adopt,
    Resume,
    Stop,
}

/// A warm-pool verb, as the verb table hands it to [`Backend::pool`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PoolOp {
    /// The pool summary `list` carries.
    Status,
    Claim(String),
    Seed,
    Remove { apply: bool },
}

/// A verb called on another node's cch.
#[derive(Debug, Clone, PartialEq)]
pub struct PeerCall {
    pub node: String,
    pub tool: &'static str,
    pub verb: &'static str,
    pub args: Value,
    pub timeout: Duration,
}

/// Everything the verbs do beyond reading cch's and Claude Code's files under [`Backend::paths`].
/// [`Live`] is the real one; the tests fake it.
pub trait Backend: Send + Sync + 'static {
    type Docker: isolated::Docker;

    fn paths(&self) -> &Paths;
    fn home(&self) -> &ClaudeHome {
        &self.paths().home
    }
    /// This node's isolated sessions.
    fn isolated(&self) -> &Isolated<Self::Docker>;
    /// What cch takes the session at `pid` to be: `session`, `session_id`, `pid`, `hosting`, `pane`.
    fn describe_caller(&self, pid: Option<u32>) -> impl Future<Output = Result<Value, LaneError>> + Send;
    /// The mode the session at `pid` was started with, for the gateway's gate.
    fn mode_of(&self, pid: Option<i64>) -> impl Future<Output = Mode> + Send;
    /// The session `job` names, or the caller's own ([`control::resolve`]).
    fn resolve(&self, job: Option<String>, pid: Option<u32>) -> impl Future<Output = Result<Target, ControlError>> + Send;
    fn control(&self, op: Op, job: Option<String>, pid: Option<u32>) -> impl Future<Output = Result<Value, ControlError>> + Send;
    /// Type into the caller's own session, arming the resume a slash command is owed.
    fn send(&self, req: SendRequest) -> impl Future<Output = Result<Value, SendError>> + Send;
    fn ensure_bypass(&self, pid: Option<u32>, expect: Option<String>) -> impl Future<Output = Result<ModeResult, LaneError>> + Send;
    /// Every promise still worth keeping, clearing the ones that are not.
    fn pending(&self) -> impl Future<Output = Vec<Pending>> + Send;
    /// Why the caller stamped `as_` may not act on this node's sessions, or None if it may.
    fn caller_refusal(&self, as_: Option<String>) -> impl Future<Output = Option<String>> + Send;
    /// Why this node runs no sessions for others, or None when it does.
    fn station_refusal(&self) -> impl Future<Output = Option<String>> + Send;
    fn node_name(&self) -> String;
    fn call_peer(&self, call: PeerCall) -> impl Future<Output = Result<Value, String>> + Send;
    /// `lifecycle`'s `start`, `adopt`, `resume` or `stop` over `args`, its own refusals included.
    fn lifecycle(&self, verb: Life, args: Value, as_: Option<String>) -> impl Future<Output = Value> + Send;
    /// The scope's worktree, created through the scopes service when absent; else why not.
    fn resolve_worktree(&self, project: String, scope: String) -> impl Future<Output = Result<PathBuf, String>> + Send;
    /// The session rows of `list` ([`lifecycle::collect`]).
    fn collect(&self, project: Option<String>, scope: Option<String>) -> impl Future<Output = Vec<Value>> + Send;
    /// The short ids of the background jobs whose process still runs.
    fn live_jobs(&self) -> impl Future<Output = BTreeSet<String>> + Send;
    /// Return once a daemon runs, asking the keeper first; else the refusal.
    fn ensure_daemon(&self, action: &'static str) -> impl Future<Output = Result<(), String>> + Send;
    /// One warm-pool verb's answer; an error is a failure the gateway answers as one.
    fn pool(&self, op: PoolOp) -> impl Future<Output = Result<Value, String>> + Send;
    /// One of the keeper's `daemon_*` verbs.
    fn daemon(&self, verb: &str, args: &Value) -> impl Future<Output = Value> + Send;
}

/// Why a call stamped `as_` may not start or stop sessions here, or None if it may. A peer must
/// be a node that holds an edge to this one; the bare legacy `peer` names no node and is refused.
pub fn caller_refusal(as_: Option<&str>, edges: impl FnOnce() -> anyhow::Result<EdgeList>) -> Option<String> {
    let stamp = as_?;
    if !(stamp == "peer" || stamp.starts_with("peer:")) {
        return None;
    }
    let Some(node) = config::caller_peer(as_) else {
        return Some("an unverified peer call cannot start or stop sessions".into());
    };
    let admitted = match edges() {
        Ok(edges) => config::admits(&edges, as_),
        Err(e) => return Some(format!("the fleet edge list is invalid: {e}")),
    };
    (!admitted).then(|| {
        format!(
            "peer {} holds no edge to this node; only a peer that reaches this node may start or stop sessions",
            py_repr(node)
        )
    })
}

/// A kept stop: `claude stop` alone, so the job resumes by its session id (`sessions.md` R10). A
/// job whose worker is already gone counts as stopped, and a stop is recorded in its state file.
#[derive(Debug, Clone)]
pub struct StopKeeping<R, P> {
    pub paths: Paths,
    pub runner: R,
    pub probe: P,
}

impl<R, P> Remover for StopKeeping<R, P>
where
    R: Runner + Send + Sync + 'static,
    P: Probe + Clone + Send + Sync + 'static,
{
    async fn remove(&self, job: &JobId) -> StopOutcome {
        let mut out = cli::stop_keeping(job, &self.paths.claude_bin, &self.runner).await;
        if !out.stopped {
            let (probe, id) = (self.probe.clone(), job.clone());
            if !tokio::task::spawn_blocking(move || probe.worker_alive(&id)).await.unwrap_or(true) {
                out.stopped = true;
                out.detail = "it had already stopped".into();
            }
        }
        if out.stopped {
            let (paths, short) = (self.paths.clone(), job.to_string());
            let _ = tokio::task::spawn_blocking(move || lifecycle::record_stop(&paths, &short)).await;
        }
        out
    }
}

/// The keeper as lifecycle's daemon gate: in-process, never a call back through the gateway.
pub struct KeeperGate {
    pub keeper: Arc<Keeper>,
    pub home: ClaudeHome,
}

impl DaemonGate for KeeperGate {
    async fn pid(&self) -> Option<u32> {
        let home = self.home.clone();
        tokio::task::spawn_blocking(move || roster::daemon_pid(&home)).await.ok().flatten()
    }

    async fn ensure(&self, timeout: Duration) -> Result<Value, String> {
        Ok(self.keeper.ensure(timeout.as_secs_f64().min(keeper::ENSURE_CAP_S)).await)
    }
}

/// Lifecycle's bookkeeping for the pool loop. The sync two run on the blocking pool.
#[derive(Clone)]
pub struct Books<E> {
    pub fx: E,
}

impl<E: lifecycle::Effects> Bookkeeping for Books<E> {
    async fn adopt_pending(&self) -> anyhow::Result<Vec<String>> {
        let fx = self.fx.clone();
        Ok(tokio::task::spawn_blocking(move || lifecycle::adopt_pending(&fx, None)).await?)
    }

    async fn prune_pins(&self) -> anyhow::Result<Vec<String>> {
        Ok(lifecycle::prune_pins(&self.fx).await)
    }

    async fn prune_lineage(&self) -> anyhow::Result<Vec<String>> {
        let fx = self.fx.clone();
        Ok(tokio::task::spawn_blocking(move || lifecycle::prune_lineage(&fx, None)).await?)
    }
}

fn station() -> Option<String> {
    lifecycle::station_refusal(config::edges(), &config::node_name())
}

/// The live resume watcher: one detached task per promise.
pub type Watch = LiveWatch<Host, Host, LiveProbe, LiveWorld>;
/// Lifecycle over this host, the in-process keeper and the hub.
pub type LifeFx = lifecycle::Live<KeeperGate, Hub>;
type PoolLauncher = LiveLauncher<fn() -> Option<String>>;
type PoolLoop = reconcile::Loop<LivePool<Procfs, LiveTerminals, PoolLauncher, ProcessRunner>, Books<LifeFx>>;

/// The real [`Backend`]: this host's records, tmux server, daemon, warm pool and containers, the
/// promise store, and the hub.
pub struct Live {
    pub paths: Arc<Paths>,
    pub host: Host,
    pub promises: Promises,
    pub reporter: Reporter,
    pub watch: Watch,
    pub hub: Hub,
    pub keeper: Arc<Keeper>,
    pub life: LifeFx,
    pub iso: Arc<Isolated>,
    pub pool: Pool<Procfs, LiveTerminals>,
    pub pool_loop: Arc<PoolLoop>,
    launcher: PoolLauncher,
    fx: control::Live<Watch>,
}

impl Live {
    /// The live objects over `paths`, reporting to reflection through `hub`.
    pub fn new(paths: Paths, hub: Hub) -> Self {
        let home = paths.home.clone();
        let host = Host::live(home.clone(), None);
        let promises = Promises::new(paths.promises_dir());
        let reporter = resume::reporter(hub.clone(), &promises);
        let sender = Sender { detect: host.clone(), opener: host.clone(), probe: LiveProbe { home: home.clone() } };
        let resumer = Resumer::new(sender, LiveWorld { home: home.clone() }, promises.clone(), reporter.clone());
        let watch = LiveWatch { resumer };
        let fx = control::Live::new(paths.clone(), host.clone(), promises.clone(), watch.clone());
        let keeper = Arc::new(Keeper::system(&paths));
        let life = lifecycle::Live::new(paths.clone(), KeeperGate { keeper: keeper.clone(), home }, hub.clone());
        let iso = Arc::new(Isolated::live(&paths));
        let paths = Arc::new(paths);
        let pool = Pool::new(paths.clone(), Procfs, LiveTerminals);
        let launcher: PoolLauncher = LiveLauncher::new(station as fn() -> Option<String>);
        let tend = LivePool { pool: pool.clone(), launcher: launcher.clone(), runner: ProcessRunner };
        let pool_loop = Arc::new(reconcile::Loop::new(paths.clone(), tend, Books { fx: life.clone() }));
        Self { paths, host, promises, reporter, watch, hub, keeper, life, iso, pool, pool_loop, launcher, fx }
    }

    fn sender(&self) -> Sender<Host, Host, LiveProbe> {
        Sender { detect: self.host.clone(), opener: self.host.clone(), probe: LiveProbe { home: self.paths.home.clone() } }
    }

    fn liveness(&self) -> LiveLiveness {
        LiveLiveness { home: self.paths.home.clone(), reporter: self.reporter.clone() }
    }

    /// Move the old state over, re-arm the follow-ups the process this one replaced still owed,
    /// resend the events reflection did not take, put the hooks in place, then start the loops.
    /// The service answers no verb until this returns.
    pub async fn boot(&self) {
        // The promise store moved to `<service dir>/pending`: the replay reads the migrated one.
        migrate::migrate(&self.paths).await;
        self.replay().await;
        let hooks = hooks_reconcile::reconcile(&self.paths).await;
        for change in &hooks.changes {
            tracing::info!("cch: hooks: {change}");
        }
        if let Some(e) = &hooks.error {
            tracing::warn!("cch: the hook reconcile failed: {e}");
        }
        tokio::spawn(disable_login_sync_unit());
        let paths = self.paths.clone();
        if let Ok(Err(r)) = tokio::task::spawn_blocking(move || lifecycle::ensure_starts_dir(&paths)).await {
            tracing::warn!("cx: {r}");
        }
        self.spawn_loops();
    }

    async fn replay(&self) {
        let home = self.paths.home.clone();
        let mut watch = self.watch.clone();
        let out = resume::replay_pending(
            &self.promises,
            &self.liveness(),
            watcher::now_ms(),
            |pid| Tail::new(home.clone(), pid),
            &mut watch,
        )
        .await;
        if out["pending"].as_u64().unwrap_or(0) > 0 {
            tracing::info!("cch: {} pending resume(s) found on boot, {} re-armed", out["pending"], out["resumed"]);
        }
        let reporter = self.reporter.clone();
        tokio::spawn(async move {
            let sent = reporter.resend().await;
            if sent > 0 {
                tracing::info!("cch: resent {sent} spooled harness events");
            }
        });
    }

    /// The keeper, the memory watch, the pool loop, the isolated login sync and the session
    /// events, each respawned if it dies.
    fn spawn_loops(&self) {
        let k = self.keeper.clone();
        spawn_supervised("keep.reconcile", RESPAWN, move || keep_daemon_running(k.clone()));
        let (home, hub) = (self.paths.home.clone(), self.hub.clone());
        let notify =
            Notify { node: config::node_name(), operator: self.paths.operator_dm.clone(), social_peer: self.paths.social_peer.clone() };
        let low = self.paths.keep.memory_low;
        spawn_supervised("keep.memory", RESPAWN, move || {
            watch_memory(MemWatch::new(low), SystemNode { home: home.clone() }, hub.clone(), notify.clone())
        });
        // Spawned even when the pool is off: the bookkeeping in its tick runs regardless.
        if let Some(why) = self.pool_loop.enabled() {
            tracing::info!("cx: not tending the warm pool — {why}");
        }
        let l = self.pool_loop.clone();
        spawn_supervised("cx.reconcile", RESPAWN, move || {
            let l = l.clone();
            async move {
                l.run().await;
                Ok(())
            }
        });
        let iso = self.iso.clone();
        spawn_supervised("cch.isolated-login", RESPAWN, move || {
            let iso = iso.clone();
            async move {
                iso.watch(isolated::WATCH_EVERY).await;
                Ok(())
            }
        });
        let (relaunched, relaunches) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(restore_after_relaunch(self.fx.clone(), self.paths.clone(), relaunches));
        let (paths, gw) = (self.paths.clone(), Arc::new(self.hub.clone()));
        spawn_supervised("cch.session-events", RESPAWN, move || {
            let (paths, gw, relaunched) = (paths.clone(), gw.clone(), relaunched.clone());
            async move {
                session_events::run(paths, gw, relaunched).await;
                Ok(())
            }
        });
    }
}

/// Put each relaunched job's Remote Control back to its last recorded switch once its bridge has
/// had [`RC_SETTLE`] to come up. Claude Code relaunches a job with the flags it was created with,
/// so a switch made since would otherwise be lost. A job whose record holds no switch is skipped.
async fn restore_after_relaunch(
    fx: control::Live<Watch>,
    paths: Arc<Paths>,
    mut relaunches: tokio::sync::mpsc::UnboundedReceiver<String>,
) {
    while let Some(job) = relaunches.recv().await {
        let (fx, paths) = (fx.clone(), paths.clone());
        tokio::spawn(async move {
            tokio::time::sleep(RC_SETTLE).await;
            let j = job.clone();
            let recorded = tokio::task::spawn_blocking(move || lifecycle::read_lineage(&paths, &j)).await.ok().flatten();
            let Some(want) = recorded.and_then(|rec| rec.get("remote_control").cloned()) else { return };
            let on = !matches!(want, Value::Null | Value::Bool(false));
            match control::restore_remote_control(&fx, &job, on, want.as_str()).await {
                None => {}
                Some(Ok(out)) => tracing::info!("cch: relaunched {job}: Remote Control put back {}: {out}", if on { "on" } else { "off" }),
                Some(Err(e)) => tracing::warn!("cch: relaunched {job}: Remote Control could not be put back: {e}"),
            }
        });
    }
}

async fn keep_daemon_running(keeper: Arc<Keeper>) -> anyhow::Result<()> {
    loop {
        keeper.tick().await;
        tokio::time::sleep(keeper::TICK).await;
    }
}

async fn watch_memory(mut watch: MemWatch, node: SystemNode, hub: Hub, notify: Notify) -> anyhow::Result<()> {
    loop {
        memwatch::memory_tick(&mut watch, &node, &hub, &notify, epoch_now()).await;
        tokio::time::sleep(memwatch::TICK).await;
    }
}

/// Boot `app` ([`Live::boot`]), then bring back the pinned jobs a host reboot ended, in the
/// background once the daemon is up.
pub async fn boot(app: Arc<App<Live>>) {
    app.backend.boot().await;
    let mark = app.backend.paths.cx_state.join(crate::verbs::BOOT_MARK);
    spawn_supervised("cch.revive", RESPAWN, move || {
        let (app, mark) = (app.clone(), mark.clone());
        async move {
            app.revive_after_boot(std::path::Path::new(crate::verbs::BOOT_ID), &mark).await;
            // One pass a boot: parked, so the supervisor does not run it again.
            std::future::pending::<()>().await;
            Ok(())
        }
    });
}

/// Disable the Python cch's login-sync user unit when it is installed; best effort, logged.
async fn disable_login_sync_unit() {
    let Some(home) = std::env::var_os("HOME").filter(|h| !h.is_empty()) else { return };
    let config = std::env::var_os("XDG_CONFIG_HOME").filter(|c| !c.is_empty()).map_or_else(|| PathBuf::from(&home).join(".config"), PathBuf::from);
    if !tokio::fs::try_exists(config.join("systemd").join("user").join(LOGIN_SYNC_UNIT)).await.unwrap_or(false) {
        return;
    }
    let run = tokio::process::Command::new("systemctl")
        .args(["--user", "disable", "--now", LOGIN_SYNC_UNIT])
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true)
        .output();
    match tokio::time::timeout(SYSTEMCTL_TIMEOUT, run).await {
        Ok(Ok(out)) if out.status.success() => tracing::info!("cch: disabled {LOGIN_SYNC_UNIT}; cch syncs isolated logins itself"),
        Ok(Ok(out)) => tracing::warn!("cch: could not disable {LOGIN_SYNC_UNIT}: {}", String::from_utf8_lossy(&out.stderr).trim()),
        Ok(Err(e)) => tracing::warn!("cch: could not disable {LOGIN_SYNC_UNIT}: {e}"),
        Err(_) => tracing::warn!("cch: disabling {LOGIN_SYNC_UNIT} timed out"),
    }
}

fn epoch_now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

impl Backend for Live {
    type Docker = isolated::DockerCli;

    fn paths(&self) -> &Paths {
        &self.paths
    }

    fn isolated(&self) -> &Isolated {
        &self.iso
    }

    async fn describe_caller(&self, pid: Option<u32>) -> Result<Value, LaneError> {
        self.sender().describe_caller(pid).await
    }

    async fn mode_of(&self, pid: Option<i64>) -> Mode {
        let Some(pid) = pid else { return Mode::Unknown };
        let paths = self.paths.clone();
        match tokio::task::spawn_blocking(move || lineage::mode_of(&paths, &Procfs, pid)).await {
            Ok(mode) => mode,
            Err(e) => {
                // A mode that cannot be read restricts hardest.
                tracing::warn!("cch: could not read the mode of pid {pid}: {e}");
                Mode::Unknown
            }
        }
    }

    async fn resolve(&self, job: Option<String>, pid: Option<u32>) -> Result<Target, ControlError> {
        control::resolve(&self.fx, job.as_deref(), pid).await
    }

    async fn control(&self, op: Op, job: Option<String>, pid: Option<u32>) -> Result<Value, ControlError> {
        let (fx, job) = (&self.fx, job.as_deref());
        match op {
            Op::Message { text, confirm } => control::message(fx, job, &text, pid, confirm).await,
            Op::Interrupt => control::interrupt(fx, job, pid).await,
            Op::Model(name) => control::model(fx, &name, job, pid).await,
            Op::Effort(level) => control::effort(fx, &level, job, pid).await,
            Op::Rename(name) => control::rename(fx, &name, job, pid).await,
            Op::Cd { path, brief } => control::cd(fx, &path, job, pid, &brief).await,
            Op::RemoteControl { on, name } => control::remote_control(fx, on, name.as_deref(), job, pid).await,
            Op::Mcp { action, server } => control::mcp(fx, &action, &server, job, pid).await,
            Op::Link => control::link(fx, job, pid).await,
            Op::Stop { confirm, keep: false } => {
                let remove = RemoveJob { home: self.paths.home.clone(), claude: self.paths.claude_bin.clone() };
                control::stop(fx, job, pid, confirm, remove).await
            }
            Op::Stop { confirm, keep: true } => {
                let probe = JobProbe { home: self.paths.home.clone() };
                let remove = StopKeeping { paths: (*self.paths).clone(), runner: ProcessRunner, probe };
                control::stop(fx, job, pid, confirm, remove).await
            }
        }
    }

    async fn send(&self, req: SendRequest) -> Result<Value, SendError> {
        let tail = Tail::new(self.paths.home.clone(), req.caller_pid.unwrap_or_default());
        let mut watch = self.watch.clone();
        self.sender().send(&req, &self.promises, tail, &mut watch).await
    }

    async fn ensure_bypass(&self, pid: Option<u32>, expect: Option<String>) -> Result<ModeResult, LaneError> {
        permission_mode::ensure_bypass(&self.host, pid, expect.as_deref()).await
    }

    async fn pending(&self) -> Vec<Pending> {
        self.promises.load_all(watcher::now_ms(), &self.liveness()).await
    }

    async fn caller_refusal(&self, as_: Option<String>) -> Option<String> {
        tokio::task::spawn_blocking(move || caller_refusal(as_.as_deref(), config::edges))
            .await
            .unwrap_or_else(|e| Some(format!("could not check the caller: {e}")))
    }

    async fn station_refusal(&self) -> Option<String> {
        tokio::task::spawn_blocking(station).await.unwrap_or_else(|e| Some(format!("could not read the fleet: {e}")))
    }

    fn node_name(&self) -> String {
        config::node_name()
    }

    async fn call_peer(&self, call: PeerCall) -> Result<Value, String> {
        let opts = CallOpts { peer: Some(call.node), timeout: call.timeout, ..CallOpts::default() };
        self.hub.call_with(call.tool, call.verb, call.args, &opts).await.map_err(|e| e.to_string())
    }

    async fn lifecycle(&self, verb: Life, args: Value, as_: Option<String>) -> Value {
        let (fx, as_) = (&self.life, as_.as_deref());
        match verb {
            Life::Start => lifecycle::start(fx, &args, as_).await,
            Life::Adopt => lifecycle::adopt(fx, &args, as_).await,
            Life::Resume => lifecycle::resume(fx, &args, as_).await,
            Life::Stop => lifecycle::stop(fx, &args, as_).await,
        }
    }

    async fn resolve_worktree(&self, project: String, scope: String) -> Result<PathBuf, String> {
        lifecycle::resolve_worktree(&self.life, &project, &scope).await.map_err(|f| match f {
            lifecycle::Failure::Refused(r) => r.reason,
            lifecycle::Failure::Os(e) => format!("isolated start failed: {e}"),
        })
    }

    async fn collect(&self, project: Option<String>, scope: Option<String>) -> Vec<Value> {
        lifecycle::collect(&self.life, project.as_deref(), scope.as_deref()).await
    }

    async fn live_jobs(&self) -> BTreeSet<String> {
        let paths = self.paths.clone();
        tokio::task::spawn_blocking(move || {
            let sessions = Sessions::new(&paths, &Procfs);
            sessions.load().into_iter().filter(|s| sessions.is_alive(s)).map(|s| s.short).collect()
        })
        .await
        .unwrap_or_default()
    }

    async fn ensure_daemon(&self, action: &'static str) -> Result<(), String> {
        lifecycle::ensure_daemon(self.life.daemon(), action, lifecycle::ENSURE_TIMEOUT).await.map_err(|r| r.reason)
    }

    async fn pool(&self, op: PoolOp) -> Result<Value, String> {
        match op {
            PoolOp::Status => {
                let held = self.pool_loop.status();
                let now = epoch_now();
                Ok(self.pool.read(move |p| pool::status(p, now, Some(held))).await)
            }
            PoolOp::Claim(cwd) => {
                let out = claim::claim(&self.pool, &DaemonKeys, &cwd).await;
                if out.get("session").and_then(Value::as_str).is_some_and(|s| !s.is_empty()) {
                    // Seed the replacement while the caller is still starting up, rather than up
                    // to a tick later.
                    self.pool_loop.kick();
                }
                Ok(out)
            }
            PoolOp::Seed => match seed::seed_one(&self.pool, &self.launcher).await {
                Ok(short) => Ok(json!({"session": short})),
                Err(LaunchError::Refused(why)) => Ok(json!({"session": null, "refused": why})),
                Err(e) => Err(e.to_string()),
            },
            PoolOp::Remove { apply: true } => {
                let done = remove::apply(&self.pool, &ProcessRunner, None).await;
                Ok(json!({"applied": done}))
            }
            PoolOp::Remove { apply: false } => Ok(json!({"plan": self.pool.read(|p| remove::plan(p, None)).await})),
        }
    }

    async fn daemon(&self, verb: &str, args: &Value) -> Value {
        keeper::handle(&self.keeper, verb, args).await.unwrap_or_else(|| json!({"ok": false, "error": format!("no daemon verb {verb}")}))
    }
}

#[cfg(test)]
mod tests {

    

    use super::*;

    #[test]
    fn caller_refusal_fails_closed_on_a_peer_it_cannot_place() {
        let empty = || Ok(EdgeList::default());
        assert_eq!(caller_refusal(None, empty), None);
        assert_eq!(caller_refusal(Some("user:someone"), empty), None);
        assert_eq!(caller_refusal(Some("peer"), empty).as_deref(), Some("an unverified peer call cannot start or stop sessions"));
        let far = caller_refusal(Some("peer:far"), empty).unwrap();
        assert!(far.starts_with("peer 'far' holds no edge to this node"), "{far}");
        let broken = caller_refusal(Some("peer:far"), || Err(anyhow::anyhow!("bad yaml"))).unwrap();
        assert!(broken.contains("bad yaml"), "{broken}");
    }

    // --- tests/test_cch_hub.py ---

}
