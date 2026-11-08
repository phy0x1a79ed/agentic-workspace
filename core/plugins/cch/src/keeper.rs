//! Keeps the node's Claude Code daemon running in its own transient user unit, outside awm and
//! every terminal, and holds it off on request.
//!
//! Every background session is a child of the daemon and lives in its control group. A daemon
//! started by `claude agents` lives in that terminal's scope, where an OOM kill anywhere takes it
//! and every session with it; one started from inside the gateway dies on the next deploy. So the
//! daemon runs in `awm-claude-daemon.service`, launched from here with `KillMode=process` (the
//! sessions outlive the daemon's own stops and self-upgrades), a restart backoff that systemd
//! never gives up on, `OOMPolicy=continue` and `OOMScoreAdjust=-200`.
//!
//! `KillMode=process` alone keeps a session for about a minute: each pty host kills its REPL once
//! its daemon has been gone for 30 checks of `CLAUDE_PTY_ORPHAN_CHECK_MS`. The daemon hands its
//! environment to every pty host, so the check is stretched until the grace outlasts the longest
//! restart backoff. A transient daemon that will not yield to the kept one over its control
//! socket gets a SIGTERM to itself alone, which leaves its sessions running.

use std::collections::BTreeMap;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Mutex as SyncMutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use awm_harness::claude::{daemonlog, roster, ClaudeHome};
use awm_plugin_sdk::procfs;
use serde::Serialize;
use serde_json::{json, Map, Value};
use tokio::sync::Mutex;
use tokio::time::Instant;

use crate::paths::{KeepKnobs, Paths};

/// Seconds between two reconciles of the keeper's loop.
pub const TICK_S: f64 = 30.0;
/// The loop's period, as a sleep takes it.
pub const TICK: Duration = Duration::from_secs(30);
/// A kept daemon up this long has its restart count reset. systemd keeps counting restarts across
/// healthy runs, so without the reset a few weeks of upgrades leave every restart waiting 10 min.
pub const SETTLE_S: f64 = 600.0;
/// How long a transient daemon gets to yield to the kept one before it is terminated.
pub const DISPLACE_S: f64 = 90.0;
/// How long `ensure` waits for a daemon by default.
pub const ENSURE_TIMEOUT_S: f64 = 30.0;
/// The most `daemon_ensure` waits, whatever the caller asks, so the verb answers inside its 60 s.
pub const ENSURE_CAP_S: f64 = 55.0;
/// `ensure` cuts a unit's restart backoff short at most this often.
pub const KICK_EVERY_S: f64 = 60.0;
/// How long a session outlives a daemon that is gone: longer than the 10 min restart backoff, so
/// a daemon that comes back at all finds its sessions.
pub const ORPHAN_GRACE_S: u64 = 1200;
/// The pty host gives up after this many orphan checks.
pub const ORPHAN_CHECKS: u64 = 30;
/// A hold's length when the caller names none.
pub const HOLD_DEFAULT_S: f64 = 600.0;

/// The verbs the keeper serves, each answered by the function of the same name.
pub const VERBS: [&str; 4] = ["daemon_status", "daemon_ensure", "daemon_hold", "daemon_release"];

/// The properties every launch of the unit carries.
pub const UNIT_PROPERTIES: [&str; 6] = [
    "KillMode=process",
    "Restart=always",
    "StartLimitIntervalSec=0",
    "OOMPolicy=continue",
    "OOMScoreAdjust=-200",
    "TimeoutStopSec=30",
];
/// systemd before 254 refuses `RestartSteps` and `RestartMaxDelaySec`, and with them the whole
/// unit, so an older host restarts at one fixed, longer delay.
pub const BACKOFF_SINCE: u32 = 254;
/// The restart backoff, 10 s to 10 min.
pub const BACKOFF: [&str; 3] = ["RestartSec=10s", "RestartSteps=6", "RestartMaxDelaySec=10min"];
/// The fixed restart delay of a systemd older than [`BACKOFF_SINCE`].
pub const NO_BACKOFF: [&str; 1] = ["RestartSec=1min"];

const SHOW: [&str; 10] = [
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "NRestarts",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "MemoryHigh",
    "MemoryMax",
];

const RUNNING_STATES: [&str; 3] = ["active", "activating", "reloading"];

/// A user manager's bus: the environment `systemctl --user` needs to reach it.
pub type Bus = Vec<(String, String)>;

/// A hold record, `{since, until, reason}`.
pub type Hold = Map<String, Value>;

/// The `CLAUDE_PTY_ORPHAN_CHECK_MS` that stretches a session's orphan grace to [`ORPHAN_GRACE_S`].
pub fn orphan_env() -> Vec<(String, String)> {
    let check_ms = ORPHAN_GRACE_S * 1000 / ORPHAN_CHECKS;
    vec![("CLAUDE_PTY_ORPHAN_CHECK_MS".into(), check_ms.to_string())]
}

/// The unit's memory limits, from `AWM_KEEP_MEMORY_HIGH` and `AWM_KEEP_MEMORY_MAX`.
pub fn memory_properties(knobs: &KeepKnobs) -> Vec<String> {
    [("MemoryHigh", &knobs.memory_high), ("MemoryMax", &knobs.memory_max)]
        .into_iter()
        .filter_map(|(prop, value)| value.as_deref().map(str::trim).filter(|v| !v.is_empty()).map(|v| format!("{prop}={v}")))
        .collect()
}

/// What the daemon needs beyond the user manager's own environment.
///
/// The daemon hands its environment to every session it starts, so this is the sessions' `PATH`.
/// The gateway's own environment is not copied: on a node where it was restarted from inside a
/// session it carries that session's ids.
pub fn daemon_env(knobs: &KeepKnobs, user_home: &Path, conda_root: Option<&Path>) -> Vec<(String, String)> {
    let mut path = vec![user_home.join(".local").join("bin").to_string_lossy().into_owned()];
    let mut env = Vec::new();
    if let Some(root) = conda_root {
        path.push(root.join("condabin").to_string_lossy().into_owned());
        path.push(root.join("bin").to_string_lossy().into_owned());
        env.push(("MAMBA_ROOT_PREFIX".to_string(), root.to_string_lossy().into_owned()));
    }
    path.extend(["/usr/local/bin", "/usr/bin", "/bin"].map(String::from));
    env.push(("PATH".into(), path.join(":")));
    env.extend(knobs.passed_env.iter().filter(|(_, v)| !v.is_empty()).cloned());
    env.extend(orphan_env());
    env
}

/// The major version from `systemctl --version`, e.g. 249 from `systemd 249 (249.11)`.
pub fn systemd_version(text: &str) -> Option<u32> {
    let mut parts = text.split_whitespace();
    match (parts.next(), parts.next()) {
        (Some("systemd"), Some(n)) if !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()) => n.parse().ok(),
        _ => None,
    }
}

/// The restart properties a systemd of `version` accepts; an unknown version gets the backoff.
pub fn restart_properties(version: Option<u32>) -> &'static [&'static str] {
    match version {
        Some(v) if v < BACKOFF_SINCE => &NO_BACKOFF,
        _ => &BACKOFF,
    }
}

/// The `systemd-run` command line that launches the kept daemon as `unit`.
pub fn launch_argv(
    unit: &str,
    claude: &Path,
    env: &[(String, String)],
    memory: &[String],
    restart: &[&str],
    user_home: &Path,
) -> Vec<String> {
    let mut argv: Vec<String> = ["systemd-run", "--user", "--quiet", "--collect"].map(String::from).into();
    argv.push(format!("--unit={unit}"));
    argv.push("--description=Claude Code daemon, kept by awm".into());
    let props = UNIT_PROPERTIES.iter().chain(restart).map(|p| p.to_string()).chain(memory.iter().cloned());
    argv.extend(props.map(|p| format!("--property={p}")));
    argv.push(format!("--working-directory={}", user_home.display()));
    argv.extend(env.iter().map(|(k, v)| format!("--setenv={k}={v}")));
    argv.push("--".into());
    argv.push(claude.to_string_lossy().into_owned());
    argv.extend(["daemon", "run", "--origin", "service"].map(String::from));
    argv
}

/// The unit's state as `systemctl show` reports it. Serialized under systemd's own names.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "PascalCase")]
pub struct UnitState {
    pub load_state: Option<String>,
    pub active_state: Option<String>,
    pub sub_state: Option<String>,
    #[serde(rename = "MainPID")]
    pub main_pid: Option<i64>,
    pub n_restarts: Option<i64>,
    pub result: Option<String>,
    pub exec_main_code: Option<String>,
    pub exec_main_status: Option<i64>,
    pub memory_high: Option<String>,
    pub memory_max: Option<String>,
}

impl UnitState {
    /// The unit's main pid, when it names one.
    pub fn main(&self) -> Option<u32> {
        self.main_pid.and_then(|p| u32::try_from(p).ok()).filter(|p| *p > 0)
    }

    fn running(&self) -> bool {
        self.active_state.as_deref().is_some_and(|s| RUNNING_STATES.contains(&s))
    }

    fn restarted(&self) -> bool {
        self.n_restarts.is_some_and(|n| n != 0)
    }
}

/// The fields of `systemctl show` output that the keeper reads.
pub fn parse_show(text: &str) -> UnitState {
    let fields: BTreeMap<&str, &str> = text.lines().filter_map(|l| l.split_once('=')).collect();
    let text = |k: &str| fields.get(k).map(|v| v.to_string());
    let int = |k: &str| fields.get(k).filter(|v| !v.is_empty()).and_then(|v| v.trim().parse().ok());
    UnitState {
        load_state: text("LoadState"),
        active_state: text("ActiveState"),
        sub_state: text("SubState"),
        main_pid: int("MainPID"),
        n_restarts: int("NRestarts"),
        result: text("Result"),
        exec_main_code: text("ExecMainCode"),
        exec_main_status: int("ExecMainStatus"),
        memory_high: text("MemoryHigh"),
        memory_max: text("MemoryMax"),
    }
}

/// The value of `key` in `systemctl show -p Environment` output, or None when the unit sets none.
pub fn unit_env_value(text: &str, key: &str) -> Option<String> {
    let line = text.lines().find_map(|l| l.strip_prefix("Environment="))?;
    line.split_whitespace()
        .map(|kv| kv.trim_matches('"'))
        .find_map(|kv| kv.strip_prefix(key)?.strip_prefix('='))
        .filter(|v| !v.is_empty())
        .map(str::to_string)
}

/// A running Claude Code daemon.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Daemon {
    pub pid: u32,
    /// `--origin`: `service` for a kept one, `transient` for one a terminal started.
    pub origin: String,
    /// The systemd unit or scope its cgroup names.
    pub unit: Option<String>,
    /// Seconds since the epoch.
    pub started_at: Option<f64>,
    /// The version directory its binary runs from.
    pub version: Option<String>,
    /// Whether its control socket accepts a connection.
    pub control: bool,
    pub spawned_by: Option<String>,
}

impl Daemon {
    /// Seconds it has been up at `now`, never negative.
    pub fn uptime(&self, now: f64) -> Option<f64> {
        self.started_at.map(|t| (now - t).max(0.0))
    }
}

fn flag_value(argv: &[String], flag: &str) -> Option<String> {
    let i = argv.iter().position(|a| a == flag)?;
    Some(argv.get(i + 1).cloned().unwrap_or_default())
}

/// What `pid` is, if it is a Claude Code daemon, read from `/proc` with `control` left false.
fn describe_proc(pid: u32) -> Option<Daemon> {
    let argv = procfs::cmdline(pid).unwrap_or_default();
    if !argv.iter().any(|a| a == "daemon") || !argv.iter().any(|a| a == "run") {
        return None;
    }
    let version = procfs::exe(pid)
        .and_then(|exe| Path::new(&exe).file_name().map(|n| n.to_string_lossy().into_owned()))
        .filter(|n| !n.is_empty());
    Some(Daemon {
        pid,
        origin: flag_value(&argv, "--origin").unwrap_or_else(|| "unknown".into()),
        unit: procfs::cgroup_unit(pid),
        started_at: procfs::started_at_epoch(pid),
        version,
        control: false,
        spawned_by: flag_value(&argv, "--spawned-by"),
    })
}

async fn reachable(path: &Path) -> bool {
    matches!(tokio::time::timeout(Duration::from_secs(2), tokio::net::UnixStream::connect(path)).await, Ok(Ok(_)))
}

/// What the process `pid` is, if it is a running Claude Code daemon.
pub async fn describe(home: &ClaudeHome, pid: u32) -> Option<Daemon> {
    let mut d = tokio::task::spawn_blocking(move || describe_proc(pid)).await.ok().flatten()?;
    d.control = reachable(&home.control_socket()).await;
    Some(d)
}

/// The running daemon: the roster's supervisor, else the unit's main process.
pub async fn find_daemon(home: &ClaudeHome, main_pid: Option<u32>) -> Option<Daemon> {
    let roster_home = home.clone();
    let supervisor = tokio::task::spawn_blocking(move || roster::daemon_pid(&roster_home)).await.ok().flatten();
    for pid in [supervisor, main_pid].into_iter().flatten().filter(|p| *p > 0) {
        if let Some(d) = describe(home, pid).await {
            return Some(d);
        }
    }
    None
}

/// What one command printed and how it exited; a command that could not run exits -1.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Ran {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

impl Ran {
    /// A command that exited zero with `stdout`.
    pub fn ok(stdout: &str) -> Self {
        Self { code: 0, stdout: stdout.into(), stderr: String::new() }
    }

    fn error(&self, limit: usize) -> String {
        let text = if self.stderr.is_empty() { &self.stdout } else { &self.stderr };
        text.trim().chars().take(limit).collect()
    }
}

/// Every side effect the keeper has, so a test can swap the whole host.
pub trait Host: Send + Sync + 'static {
    /// Seconds since the epoch.
    fn now(&self) -> f64;
    /// The user manager's bus, or None when this user has none.
    fn bus(&self) -> Option<Bus>;
    /// Whether the Claude Code binary is there to launch.
    fn claude_present(&self, bin: &Path) -> impl Future<Output = bool> + Send;
    /// Take the per-Claude-home keeper lock, or report that it is held already by this host.
    fn take_lock(&self) -> impl Future<Output = bool> + Send;
    /// The running daemon, given the unit's main pid.
    fn daemon(&self, main_pid: Option<u32>) -> impl Future<Output = Option<Daemon>> + Send;
    /// `systemctl --user <args>` on `bus`.
    fn systemctl(&self, bus: &[(String, String)], args: Vec<String>) -> impl Future<Output = Ran> + Send;
    /// The kept unit's state.
    fn unit(&self, bus: &[(String, String)]) -> impl Future<Output = UnitState> + Send;
    /// The kept unit's `systemctl show -p Environment` output.
    fn unit_environment(&self, bus: &[(String, String)]) -> impl Future<Output = String> + Send;
    /// The systemd major version, or None when it cannot be read.
    fn systemd_version(&self) -> impl Future<Output = Option<u32>> + Send;
    /// Run the `systemd-run` command line `argv` on `bus`.
    fn run_unit(&self, bus: &[(String, String)], argv: Vec<String>) -> impl Future<Output = Ran> + Send;
    /// SIGTERM to `pid` alone; a process already gone is not an error.
    fn terminate(&self, pid: u32);
    /// The tail of `daemon.log`.
    fn log_tail(&self) -> impl Future<Output = String> + Send;
    /// Whether this user lingers, or None when loginctl cannot say.
    fn linger(&self) -> impl Future<Output = Option<bool>> + Send;
    /// Turn linger on; the error text, or None when it worked.
    fn enable_linger(&self) -> impl Future<Output = Option<String>> + Send;
    /// The hold record, when one exists and is an object.
    fn held(&self) -> impl Future<Output = Option<Hold>> + Send;
    /// Write the hold record, or delete it for None.
    fn set_held(&self, hold: Option<Hold>) -> impl Future<Output = std::io::Result<()>> + Send;
}

/// The real host: systemd, loginctl, signals, the daemon's socket and `/proc`.
pub struct SystemHost {
    home: ClaudeHome,
    unit: String,
    hold_path: PathBuf,
    lock: SyncMutex<Option<std::fs::File>>,
}

impl SystemHost {
    /// The host for cch's `paths`.
    pub fn new(paths: &Paths) -> Self {
        Self {
            home: paths.home.clone(),
            unit: format!("{}.service", paths.keep.unit),
            hold_path: paths.hold_path(),
            lock: SyncMutex::new(None),
        }
    }
}

async fn run(argv: &[String], env: &[(String, String)], limit: Duration) -> Result<Ran, String> {
    let (program, args) = argv.split_first().ok_or("empty argv")?;
    let mut cmd = tokio::process::Command::new(program);
    cmd.args(args)
        .envs(env.iter().map(|(k, v)| (k, v)))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let out = match tokio::time::timeout(limit, cmd.output()).await {
        Err(_) => return Err(format!("TimeoutExpired: {program} timed out after {} seconds", limit.as_secs())),
        Ok(Err(e)) => return Err(format!("OSError: {e}")),
        Ok(Ok(out)) => out,
    };
    Ok(Ran {
        code: out.status.code().unwrap_or(-1),
        stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
    })
}

async fn run_or_failed(argv: &[String], env: &[(String, String)], limit: Duration) -> Ran {
    run(argv, env, limit).await.unwrap_or_else(|stderr| Ran { code: -1, stdout: String::new(), stderr })
}

fn lock_file(path: &Path) -> Option<std::fs::File> {
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::OpenOptionsExt;
    let file = std::fs::OpenOptions::new().create(true).truncate(false).read(true).write(true).mode(0o600).open(path).ok()?;
    // flock, not fcntl, so a Python keeper and this one exclude each other.
    // SAFETY: flock takes a valid open fd and plain flags.
    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    (rc == 0).then_some(file)
}

fn user() -> String {
    std::env::var("USER").ok().filter(|u| !u.is_empty()).unwrap_or_else(|| {
        // SAFETY: getuid cannot fail.
        unsafe { libc::getuid() }.to_string()
    })
}

impl Host for SystemHost {
    fn now(&self) -> f64 {
        SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
    }

    fn bus(&self) -> Option<Bus> {
        awm_plugin_sdk::proc::user_manager_env()
    }

    async fn claude_present(&self, bin: &Path) -> bool {
        tokio::fs::metadata(bin).await.is_ok_and(|m| m.is_file())
    }

    async fn take_lock(&self) -> bool {
        if self.lock.lock().unwrap_or_else(|p| p.into_inner()).is_some() {
            return true;
        }
        let path = self.home.root.join("awm-keep.lock");
        let Some(file) = tokio::task::spawn_blocking(move || lock_file(&path)).await.ok().flatten() else {
            return false;
        };
        *self.lock.lock().unwrap_or_else(|p| p.into_inner()) = Some(file);
        true
    }

    async fn daemon(&self, main_pid: Option<u32>) -> Option<Daemon> {
        find_daemon(&self.home, main_pid).await
    }

    async fn systemctl(&self, bus: &[(String, String)], args: Vec<String>) -> Ran {
        let argv: Vec<String> = ["systemctl".to_string(), "--user".to_string()].into_iter().chain(args).collect();
        run_or_failed(&argv, bus, Duration::from_secs(30)).await
    }

    async fn unit(&self, bus: &[(String, String)]) -> UnitState {
        let args = ["show".to_string(), self.unit.clone()].into_iter().chain(SHOW.iter().map(|k| format!("-p{k}")));
        parse_show(&self.systemctl(bus, args.collect()).await.stdout)
    }

    async fn unit_environment(&self, bus: &[(String, String)]) -> String {
        self.systemctl(bus, vec!["show".into(), "-p".into(), "Environment".into(), self.unit.clone()]).await.stdout
    }

    async fn systemd_version(&self) -> Option<u32> {
        let out = run(&["systemctl".into(), "--version".into()], &[], Duration::from_secs(10)).await.ok()?;
        systemd_version(&out.stdout)
    }

    async fn run_unit(&self, bus: &[(String, String)], argv: Vec<String>) -> Ran {
        run_or_failed(&argv, bus, Duration::from_secs(60)).await
    }

    fn terminate(&self, pid: u32) {
        let Ok(pid) = libc::pid_t::try_from(pid) else { return };
        // SAFETY: kill takes a plain pid and signal number.
        unsafe { libc::kill(pid, libc::SIGTERM) };
    }

    async fn log_tail(&self) -> String {
        let home = self.home.clone();
        tokio::task::spawn_blocking(move || daemonlog::read_tail(&home, daemonlog::TAIL_BYTES)).await.unwrap_or_default()
    }

    async fn linger(&self) -> Option<bool> {
        let argv = ["loginctl", "show-user", &user(), "-pLinger", "--value"].map(String::from);
        let out = run(&argv, &[], Duration::from_secs(10)).await.ok()?;
        match out.stdout.trim() {
            "yes" => Some(true),
            "no" => Some(false),
            _ => None,
        }
    }

    async fn enable_linger(&self) -> Option<String> {
        match run(&["loginctl".into(), "enable-linger".into()], &[], Duration::from_secs(20)).await {
            Err(e) => Some(e),
            Ok(out) if out.code == 0 => None,
            Ok(out) => Some(out.error(300)),
        }
    }

    async fn held(&self) -> Option<Hold> {
        let bytes = tokio::fs::read(&self.hold_path).await.ok()?;
        match serde_json::from_slice(&bytes).ok()? {
            Value::Object(hold) => Some(hold),
            _ => None,
        }
    }

    async fn set_held(&self, hold: Option<Hold>) -> std::io::Result<()> {
        let Some(hold) = hold else {
            return match tokio::fs::remove_file(&self.hold_path).await {
                Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
                _ => Ok(()),
            };
        };
        if let Some(dir) = self.hold_path.parent() {
            tokio::fs::create_dir_all(dir).await?;
        }
        tokio::fs::write(&self.hold_path, Value::Object(hold).to_string()).await
    }
}

/// Python's truthiness of a JSON value.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// Python's `float(v)` of a JSON value.
fn as_float(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        Value::Bool(b) => Some(f64::from(u8::from(*b))),
        _ => None,
    }
}

/// When a hold ends. One written before holds expired ends after the default.
fn hold_until(hold: &Hold) -> f64 {
    match hold.get("until").filter(|u| truthy(u)) {
        Some(until) => as_float(until).unwrap_or(0.0),
        None => hold.get("since").and_then(as_float).map_or(0.0, |s| s + HOLD_DEFAULT_S),
    }
}

fn round(v: Option<f64>) -> Option<i64> {
    v.map(|v| v.round_ties_even() as i64)
}

fn merge(mut base: Value, extra: Value) -> Value {
    if let (Some(b), Value::Object(e)) = (base.as_object_mut(), extra) {
        b.extend(e);
    }
    base
}

fn action(out: &Value) -> &str {
    out.get("action").and_then(Value::as_str).unwrap_or("")
}

/// The keeper's in-memory decision state. A restart drops it, which costs only a timer.
#[derive(Default)]
struct State {
    transient_since: Option<f64>,
    last_kick: f64,
    linger_tried: bool,
    env_pushed: bool,
    /// The conda root read from the kept unit's environment, once.
    unit_conda_root: Option<PathBuf>,
    unit_env_read: bool,
    conda_warned: bool,
}

/// The last reconcile's outcome and when it ran, for `status`.
struct Report {
    last_tick: Option<f64>,
    last: Value,
}

/// Adopts the daemon, launches it, or clears what blocks it. One per process: its reconciles run
/// one at a time.
pub struct Keeper<H: Host = SystemHost> {
    pub host: H,
    knobs: KeepKnobs,
    user_home: PathBuf,
    state: Mutex<State>,
    report: SyncMutex<Report>,
}

impl Keeper<SystemHost> {
    /// The keeper of this node's daemon, for cch's `paths`.
    pub fn system(paths: &Paths) -> Self {
        let home = std::env::var_os("HOME").filter(|h| !h.is_empty()).map_or_else(|| PathBuf::from("/"), PathBuf::from);
        Keeper::new(SystemHost::new(paths), paths.keep.clone(), home)
    }
}

impl<H: Host> Keeper<H> {
    /// A keeper over `host`, with the daemon's working directory and `~/.local/bin` under `user_home`.
    pub fn new(host: H, knobs: KeepKnobs, user_home: PathBuf) -> Self {
        Self {
            host,
            knobs,
            user_home,
            state: Mutex::new(State::default()),
            report: SyncMutex::new(Report { last_tick: None, last: json!({}) }),
        }
    }

    fn unit_name(&self) -> String {
        format!("{}.service", self.knobs.unit)
    }

    /// Why this process must leave the daemon alone, or None if it may keep it.
    pub async fn enabled(&self) -> Option<String> {
        if !self.knobs.enabled {
            return Some("switched off (AWM_KEEP=0)".into());
        }
        if !self.host.claude_present(&self.knobs.claude_bin).await {
            return Some(format!("no Claude Code binary at {}", self.knobs.claude_bin.display()));
        }
        None
    }

    /// One tick of the keeper's loop. The loop runs it every [`TICK`].
    pub async fn tick(&self) -> Value {
        self.reconcile(false).await
    }

    /// Adopt the daemon, launch it, or clear what blocks it. `kick` cuts a restart backoff short.
    pub async fn reconcile(&self, kick: bool) -> Value {
        let out = {
            let mut state = self.state.lock().await;
            self.reconcile_locked(&mut state, kick).await
        };
        let mut report = self.report.lock().unwrap_or_else(|p| p.into_inner());
        report.last_tick = Some(self.host.now());
        if out != report.last && action(&out) != "none" {
            tracing::info!("keep: {out}");
        }
        report.last = out.clone();
        out
    }

    async fn reconcile_locked(&self, state: &mut State, kick: bool) -> Value {
        if let Some(why) = self.enabled().await {
            return json!({"action": "off", "reason": why});
        }
        if !self.host.take_lock().await {
            return json!({"action": "off", "reason": "another keeper holds the lock for this Claude home"});
        }
        let now = self.host.now();
        let mut expired = None;
        if let Some(hold) = self.host.held().await.filter(|h| !h.is_empty()) {
            if now < hold_until(&hold) {
                return json!({"action": "held", "hold": hold});
            }
            // The holder may have died with its own sessions, and nobody would ever release it.
            if let Err(e) = self.host.set_held(None).await {
                tracing::warn!("keep: could not clear the expired hold: {e}");
            }
            let reason = hold.get("reason").cloned().unwrap_or_default();
            tracing::warn!("keep: the hold {reason} expired; keeping the daemon again");
            expired = Some(hold);
        }
        let Some(bus) = self.host.bus() else {
            return self.no_manager(state).await;
        };
        if !state.env_pushed {
            // A unit launched before the grace existed lacks it in its own environment, and takes
            // the manager's on its next restart.
            let args = std::iter::once("set-environment".to_string()).chain(orphan_env().into_iter().map(|(k, v)| format!("{k}={v}")));
            state.env_pushed = self.host.systemctl(&bus, args.collect()).await.code == 0;
        }
        let out = self.settle(state, &bus, now, kick).await;
        match expired {
            Some(hold) => merge(out, json!({"hold_expired": hold})),
            None => out,
        }
    }

    async fn settle(&self, state: &mut State, bus: &[(String, String)], now: f64, kick: bool) -> Value {
        let unit_name = self.unit_name();
        let unit = self.host.unit(bus).await;
        let d = self.host.daemon(unit.main()).await;

        if let Some(d) = d.as_ref().filter(|d| d.unit.as_deref() == Some(unit_name.as_str())) {
            state.transient_since = None;
            if self.knobs.conda_root.is_none() && !state.unit_env_read {
                state.unit_env_read = true;
                state.unit_conda_root = unit_env_value(&self.host.unit_environment(bus).await, "MAMBA_ROOT_PREFIX").map(PathBuf::from);
            }
            if unit.restarted() && d.uptime(now).unwrap_or(0.0) >= SETTLE_S {
                self.host.systemctl(bus, vec!["reset-failed".into(), unit_name]).await;
                return json!({"action": "settled", "pid": d.pid, "restarts": unit.n_restarts});
            }
            return json!({"action": "none", "pid": d.pid});
        }
        if let Some(d) = d.as_ref().filter(|d| d.origin != "transient") {
            // A service daemon in some other unit: a hand-written one on an old node. It would
            // refuse ours anyway, so adopt it as it is.
            state.transient_since = None;
            return json!({"action": "none", "pid": d.pid, "foreign_unit": d.unit});
        }
        if let Some(d) = d {
            if unit.running() {
                let since = *state.transient_since.get_or_insert(now);
                if now - since < DISPLACE_S {
                    return json!({"action": "displacing", "pid": d.pid});
                }
                self.host.terminate(d.pid);
                state.transient_since = None;
                return json!({"action": "displaced", "pid": d.pid,
                              "reason": "the transient daemon did not yield to the kept one"});
            }
            return self.launch(state, bus, &unit, &format!("a transient daemon (pid {}) runs", d.pid)).await;
        }

        state.transient_since = None;
        if unit.running() {
            if kick && now - state.last_kick >= KICK_EVERY_S {
                state.last_kick = now;
                self.host.systemctl(bus, vec!["start".into(), unit_name]).await;
                return json!({"action": "kicked", "restarts": unit.n_restarts});
            }
            return json!({"action": "restarting", "restarts": unit.n_restarts, "result": unit.result});
        }
        self.launch(state, bus, &unit, "no daemon runs").await
    }

    /// The conda root for a fresh unit: the knob, else what the kept unit carried. Never a default.
    async fn conda_root(&self, state: &mut State, bus: &[(String, String)]) -> Option<PathBuf> {
        if let Some(root) = self.knobs.conda_root.clone().or_else(|| state.unit_conda_root.clone()) {
            return Some(root);
        }
        let root = unit_env_value(&self.host.unit_environment(bus).await, "MAMBA_ROOT_PREFIX").map(PathBuf::from);
        if root.is_none() && !state.conda_warned {
            state.conda_warned = true;
            tracing::warn!(
                "keep: no conda root (AWM_CONDA_ROOT, MAMBA_ROOT_PREFIX or the daemon unit's environment); \
                 the daemon's sessions get no mamba on their PATH"
            );
        }
        root
    }

    async fn launch(&self, state: &mut State, bus: &[(String, String)], unit: &UnitState, reason: &str) -> Value {
        let unit_name = self.unit_name();
        let memory = memory_properties(&self.knobs);
        let proc = if unit.load_state.as_deref() == Some("loaded") {
            // A dead unit whose sessions still run stays loaded, and its name cannot be reused, so
            // it is started again in place.
            self.host.systemctl(bus, vec!["reset-failed".into(), unit_name.clone()]).await;
            if !memory.is_empty() {
                let args = ["set-property".to_string(), "--runtime".to_string(), unit_name.clone()].into_iter().chain(memory);
                self.host.systemctl(bus, args.collect()).await;
            }
            self.host.systemctl(bus, vec!["start".into(), "--no-block".into(), unit_name]).await
        } else {
            let restart = restart_properties(self.host.systemd_version().await);
            let root = self.conda_root(state, bus).await;
            let env = daemon_env(&self.knobs, &self.user_home, root.as_deref());
            let argv = launch_argv(&self.knobs.unit, &self.knobs.claude_bin, &env, &memory, restart, &self.user_home);
            self.host.run_unit(bus, argv).await
        };
        if proc.code != 0 {
            return json!({"action": "failed", "reason": reason, "error": proc.error(400)});
        }
        json!({"action": "launched", "reason": reason})
    }

    async fn no_manager(&self, state: &mut State) -> Value {
        let linger = self.host.linger().await;
        let mut error = None;
        if linger == Some(false) && !state.linger_tried {
            state.linger_tried = true;
            error = self.host.enable_linger().await;
        }
        json!({"action": "no-manager", "linger": linger, "error": error,
               "reason": "no user systemd manager, so the daemon cannot run outside awm's control group; \
                          run `loginctl enable-linger` for this user"})
    }

    /// Reconcile now and wait up to `timeout` seconds for a daemon that answers on its control socket.
    pub async fn ensure(&self, timeout: f64) -> Value {
        let deadline = Instant::now() + Duration::try_from_secs_f64(timeout).unwrap_or(Duration::ZERO);
        let mut out = self.reconcile(true).await;
        loop {
            let main = match self.host.bus() {
                Some(bus) => self.host.unit(&bus).await.main(),
                None => None,
            };
            if let Some(d) = self.host.daemon(main).await.filter(|d| d.control) {
                return json!({"ok": true, "pid": d.pid, "origin": d.origin, "unit": d.unit, "action": out.get("action")});
            }
            if ["off", "held", "no-manager", "failed"].contains(&action(&out)) || Instant::now() >= deadline {
                if action(&out) == "held" {
                    out = merge(out, json!({"hint": "a daemon_hold is in force; daemon_release ends it"}));
                }
                // The outcome's own `reason` overrides `daemon_down`, as Python's `{**out}` did.
                return merge(json!({"ok": false, "reason": "daemon_down"}), out);
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
            if action(&out) == "displacing" {
                out = self.reconcile(false).await;
            }
        }
    }

    /// Stop the daemon and keep it stopped for `seconds` at most.
    ///
    /// A session outlives its daemon for [`ORPHAN_GRACE_S`], so a longer hold is refused: it
    /// would kill every background session on the node.
    pub async fn hold(&self, reason: &str, seconds: f64) -> Value {
        if !(seconds > 0.0 && seconds <= ORPHAN_GRACE_S as f64) {
            return json!({"held": false, "error": format!(
                "a hold lasts 1 to {ORPHAN_GRACE_S} s; every background session dies \
                 once the daemon has been gone for {ORPHAN_GRACE_S} s")});
        }
        let now = self.host.now();
        let mut record = Hold::new();
        record.insert("since".into(), json!(now));
        record.insert("until".into(), json!(now + seconds));
        record.insert("reason".into(), json!(reason));
        if let Err(e) = self.host.set_held(Some(record)).await {
            return json!({"held": false, "error": format!("could not write the hold record: {e}")});
        }
        let mut main = None;
        if let Some(bus) = self.host.bus() {
            main = self.host.unit(&bus).await.main();
            self.host.systemctl(&bus, vec!["stop".into(), self.unit_name()]).await;
        }
        let d = self.host.daemon(main).await;
        if let Some(d) = d.as_ref().filter(|d| d.unit.as_deref() != Some(self.unit_name().as_str())) {
            self.host.terminate(d.pid);
        }
        json!({"held": true, "reason": reason, "until": now + seconds,
               "stopped": d.map(|d| d.pid),
               "warning": format!("sessions survive for {ORPHAN_GRACE_S} s without the daemon, \
                                   but one whose pty host started before keep set that grace dies after 60 s")})
    }

    /// End a hold and start the daemon again.
    pub async fn release(&self) -> Value {
        if let Err(e) = self.host.set_held(None).await {
            tracing::warn!("keep: could not delete the hold record: {e}");
        }
        merge(json!({"held": false}), self.reconcile(true).await)
    }

    /// The daemon, its unit, the hold, linger, the daemon's start history and the loop's last tick.
    pub async fn status(&self) -> Value {
        let now = self.host.now();
        let bus = self.host.bus();
        let unit = match &bus {
            Some(bus) => Some(self.host.unit(bus).await),
            None => None,
        };
        let d = self.host.daemon(unit.as_ref().and_then(UnitState::main)).await;
        let events = daemonlog::history(&self.host.log_tail().await, daemonlog::HISTORY);
        let unit_name = self.unit_name();
        let daemon = d.as_ref().map(|d| merge(json!(d), json!({"uptime_s": round(d.uptime(now))})));
        let unit_row = unit.as_ref().map(|u| merge(json!({"name": unit_name}), json!(u)));
        let why_off = self.enabled().await;
        let (last_tick, last) = {
            let report = self.report.lock().unwrap_or_else(|p| p.into_inner());
            (report.last_tick, report.last.clone())
        };
        json!({
            "running": d.is_some(),
            "daemon": daemon,
            "kept": d.as_ref().is_some_and(|d| d.unit.as_deref() == Some(unit_name.as_str())),
            "unit": unit_row,
            "manager": bus.is_some(),
            "linger": self.host.linger().await,
            "held": self.host.held().await,
            "last_stop": daemonlog::last_stop(&events),
            "history": events,
            "loop": {"enabled": why_off.unwrap_or_else(|| "yes".into()),
                     "last_tick_s": round(last_tick.map(|t| now - t)), "last": last},
        })
    }
}

/// Python's `float(args.get(key) or default)`, with `default` for a value that is not a number.
fn number_arg(args: &Value, key: &str, default: f64) -> f64 {
    args.get(key).filter(|v| truthy(v)).and_then(as_float).unwrap_or(default)
}

/// `daemon_status`: the keeper's status.
pub async fn daemon_status<H: Host>(keeper: &Keeper<H>, _args: &Value) -> Value {
    keeper.status().await
}

/// `daemon_ensure {timeout}`: start the daemon if none runs and wait for it, at most [`ENSURE_CAP_S`].
pub async fn daemon_ensure<H: Host>(keeper: &Keeper<H>, args: &Value) -> Value {
    keeper.ensure(number_arg(args, "timeout", ENSURE_TIMEOUT_S).min(ENSURE_CAP_S)).await
}

/// `daemon_hold {reason, seconds}`: stop the daemon and keep it stopped; a hold needs a reason.
pub async fn daemon_hold<H: Host>(keeper: &Keeper<H>, args: &Value) -> Value {
    let reason = match args.get("reason").filter(|v| truthy(v)) {
        Some(Value::String(s)) => s.trim().to_string(),
        Some(other) => other.to_string().trim().to_string(),
        None => String::new(),
    };
    if reason.is_empty() {
        return json!({"ok": false, "error": "a hold needs a reason"});
    }
    keeper.hold(&reason, number_arg(args, "seconds", HOLD_DEFAULT_S)).await
}

/// `daemon_release`: end a hold and start the daemon again.
pub async fn daemon_release<H: Host>(keeper: &Keeper<H>, _args: &Value) -> Value {
    keeper.release().await
}

/// The answer to one of [`VERBS`], or None for any other verb.
pub async fn handle<H: Host>(keeper: &Keeper<H>, verb: &str, args: &Value) -> Option<Value> {
    Some(match verb {
        "daemon_status" => daemon_status(keeper, args).await,
        "daemon_ensure" => daemon_ensure(keeper, args).await,
        "daemon_hold" => daemon_hold(keeper, args).await,
        "daemon_release" => daemon_release(keeper, args).await,
        _ => return None,
    })
}

/// A host with no systemd, no daemon and no signals, for tests.
pub mod testing {
    use std::sync::Mutex;

    use super::*;

    /// What the fake host reports, and what it was asked to do.
    pub struct FakeState {
        pub clock: f64,
        pub has_bus: bool,
        pub unit_state: UnitState,
        pub running: Option<Daemon>,
        /// Each effect: `["systemctl", args..]`, `["systemd-run", ..]`, `["kill", pid]`, `["enable-linger"]`.
        pub calls: Vec<Vec<String>>,
        pub hold_state: Option<Hold>,
        pub linger_state: Option<bool>,
        pub tail: String,
        /// What `set-environment` put in the user manager's environment.
        pub manager_env: BTreeMap<String, String>,
        pub systemd: Option<u32>,
        /// The kept unit's `systemctl show -p Environment` output.
        pub unit_env: String,
        /// The daemon a `systemd-run` brings up, if any.
        pub start_on_launch: Option<Daemon>,
        pub claude_present: bool,
        pub lock_free: bool,
    }

    impl Default for FakeState {
        fn default() -> Self {
            Self {
                clock: 10_000.0,
                has_bus: true,
                unit_state: UnitState {
                    load_state: Some("not-found".into()),
                    active_state: Some("inactive".into()),
                    ..UnitState::default()
                },
                running: None,
                calls: Vec::new(),
                hold_state: None,
                linger_state: Some(true),
                tail: String::new(),
                manager_env: BTreeMap::new(),
                systemd: Some(255),
                unit_env: "Environment=\n".into(),
                start_on_launch: None,
                claude_present: true,
                lock_free: true,
            }
        }
    }

    /// The fake host. Lock `state` to set it up or read what happened.
    #[derive(Default)]
    pub struct FakeHost {
        pub state: Mutex<FakeState>,
    }

    impl FakeHost {
        /// A host in `state`.
        pub fn with(state: FakeState) -> Self {
            Self { state: Mutex::new(state) }
        }

        fn s(&self) -> std::sync::MutexGuard<'_, FakeState> {
            self.state.lock().unwrap()
        }
    }

    impl Host for FakeHost {
        fn now(&self) -> f64 {
            self.s().clock
        }

        fn bus(&self) -> Option<Bus> {
            self.s().has_bus.then(|| vec![("XDG_RUNTIME_DIR".into(), "/run/user/1".into())])
        }

        async fn claude_present(&self, _bin: &Path) -> bool {
            self.s().claude_present
        }

        async fn take_lock(&self) -> bool {
            self.s().lock_free
        }

        async fn daemon(&self, _main_pid: Option<u32>) -> Option<Daemon> {
            self.s().running.clone()
        }

        async fn systemctl(&self, _bus: &[(String, String)], args: Vec<String>) -> Ran {
            let mut s = self.s();
            if args.first().map(String::as_str) == Some("set-environment") {
                s.manager_env.extend(args[1..].iter().filter_map(|a| a.split_once('=')).map(|(k, v)| (k.into(), v.into())));
            } else {
                s.calls.push(std::iter::once("systemctl".to_string()).chain(args).collect());
            }
            Ran::ok("")
        }

        async fn unit(&self, _bus: &[(String, String)]) -> UnitState {
            self.s().unit_state.clone()
        }

        async fn unit_environment(&self, _bus: &[(String, String)]) -> String {
            self.s().unit_env.clone()
        }

        async fn systemd_version(&self) -> Option<u32> {
            self.s().systemd
        }

        async fn run_unit(&self, _bus: &[(String, String)], argv: Vec<String>) -> Ran {
            let mut s = self.s();
            s.calls.push(argv);
            if let Some(d) = s.start_on_launch.clone() {
                s.running = Some(d);
            }
            Ran::ok("")
        }

        fn terminate(&self, pid: u32) {
            self.s().calls.push(vec!["kill".into(), pid.to_string()]);
        }

        async fn log_tail(&self) -> String {
            self.s().tail.clone()
        }

        async fn linger(&self) -> Option<bool> {
            self.s().linger_state
        }

        async fn enable_linger(&self) -> Option<String> {
            self.s().calls.push(vec!["enable-linger".into()]);
            None
        }

        async fn held(&self) -> Option<Hold> {
            self.s().hold_state.clone()
        }

        async fn set_held(&self, hold: Option<Hold>) -> std::io::Result<()> {
            self.s().hold_state = hold;
            Ok(())
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::{FakeHost, FakeState};
    use super::*;

    const UNIT: &str = "awm-claude-daemon.service";

    fn knobs() -> KeepKnobs {
        Paths::under(Path::new("/r")).keep
    }

    fn keeper(state: FakeState) -> Keeper<FakeHost> {
        keeper_with(state, knobs())
    }

    fn keeper_with(state: FakeState, knobs: KeepKnobs) -> Keeper<FakeHost> {
        Keeper::new(FakeHost::with(state), knobs, PathBuf::from("/u"))
    }

    fn daemon(origin: &str, unit: &str, started_at: f64) -> Daemon {
        Daemon {
            pid: 42,
            origin: origin.into(),
            unit: Some(unit.into()),
            started_at: Some(started_at),
            version: Some("9.0.1".into()),
            control: true,
            spawned_by: None,
        }
    }

    fn kept() -> Daemon {
        daemon("service", UNIT, 9_000.0)
    }

    fn unit(load: Option<&str>, active: &str) -> UnitState {
        UnitState { load_state: load.map(String::from), active_state: Some(active.into()), ..UnitState::default() }
    }

    fn hold(since: f64, until: Option<f64>, reason: &str) -> Hold {
        let mut h = json!({"since": since, "reason": reason});
        if let Some(until) = until {
            h["until"] = json!(until);
        }
        h.as_object().unwrap().clone()
    }

    fn calls(k: &Keeper<FakeHost>) -> Vec<Vec<String>> {
        k.host.state.lock().unwrap().calls.clone()
    }

    fn run_argv(k: &Keeper<FakeHost>) -> Vec<String> {
        calls(k).into_iter().find(|c| c[0] == "systemd-run").expect("a systemd-run call")
    }

    fn call(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|p| p.to_string()).collect()
    }

    fn has_kill(k: &Keeper<FakeHost>) -> bool {
        calls(k).iter().any(|c| c[0] == "kill")
    }

    #[tokio::test]
    async fn test_no_daemon_launches_the_unit_with_backoff_and_oom_properties() {
        let k = keeper(FakeState::default());
        assert_eq!(k.reconcile(false).await["action"], "launched");
        let argv = run_argv(&k);
        assert!(argv.contains(&"--unit=awm-claude-daemon".to_string()));
        for prop in [
            "KillMode=process",
            "Restart=always",
            "RestartSteps=6",
            "RestartMaxDelaySec=10min",
            "StartLimitIntervalSec=0",
            "OOMPolicy=continue",
            "OOMScoreAdjust=-200",
        ] {
            assert!(argv.contains(&format!("--property={prop}")), "{prop}");
        }
        assert_eq!(argv[argv.len() - 4..], call(&["daemon", "run", "--origin", "service"]));
    }

    #[tokio::test]
    async fn test_a_systemd_older_than_254_gets_a_fixed_restart_delay_it_accepts() {
        let k = keeper(FakeState { systemd: Some(249), ..FakeState::default() });
        assert_eq!(k.reconcile(false).await["action"], "launched");
        let argv = run_argv(&k);
        assert!(argv.contains(&"--property=RestartSec=1min".to_string()));
        assert!(!argv.iter().any(|a| a.contains("RestartSteps") || a.contains("RestartMaxDelaySec")));
    }

    #[test]
    fn test_systemd_version_reads_the_major_number() {
        assert_eq!(systemd_version("systemd 249 (249.11-0ubuntu3.23)\n+PAM +AUDIT"), Some(249));
        assert_eq!(systemd_version(""), None);
    }

    #[tokio::test]
    async fn test_memory_limits_come_from_the_environment() {
        let mut kn = knobs();
        kn.memory_high = Some("2G".into());
        kn.memory_max = Some("3G".into());
        let k = keeper_with(FakeState::default(), kn);
        k.reconcile(false).await;
        let argv = run_argv(&k);
        assert!(argv.contains(&"--property=MemoryHigh=2G".to_string()));
        assert!(argv.contains(&"--property=MemoryMax=3G".to_string()));
    }

    #[tokio::test]
    async fn test_a_dead_unit_that_still_holds_sessions_is_started_in_place() {
        let k = keeper(FakeState { unit_state: unit(Some("loaded"), "inactive"), ..FakeState::default() });
        assert_eq!(k.reconcile(false).await["action"], "launched");
        assert!(calls(&k).contains(&call(&["systemctl", "start", "--no-block", UNIT])));
        assert!(!calls(&k).iter().any(|c| c[0] == "systemd-run"));
    }

    #[tokio::test]
    async fn test_a_kept_daemon_is_left_alone() {
        let state = UnitState { n_restarts: Some(0), ..unit(None, "active") };
        let k = keeper(FakeState { running: Some(kept()), unit_state: state, ..FakeState::default() });
        assert_eq!(k.reconcile(false).await["action"], "none");
        assert!(calls(&k).is_empty());
    }

    #[tokio::test]
    async fn test_restart_count_is_reset_once_the_daemon_has_settled() {
        let state = UnitState { n_restarts: Some(3), ..unit(None, "active") };
        let young = keeper(FakeState {
            running: Some(daemon("service", UNIT, 10_000.0 - 60.0)),
            unit_state: state.clone(),
            ..FakeState::default()
        });
        assert_eq!(young.reconcile(false).await["action"], "none");
        let old = keeper(FakeState {
            running: Some(daemon("service", UNIT, 10_000.0 - SETTLE_S - 1.0)),
            unit_state: state,
            ..FakeState::default()
        });
        assert_eq!(old.reconcile(false).await["action"], "settled");
        assert!(calls(&old).contains(&call(&["systemctl", "reset-failed", UNIT])));
    }

    fn backing_off() -> UnitState {
        UnitState { sub_state: Some("auto-restart".into()), n_restarts: Some(5), ..unit(Some("loaded"), "activating") }
    }

    #[tokio::test]
    async fn test_a_unit_waiting_out_its_backoff_is_not_relaunched() {
        let k = keeper(FakeState { unit_state: backing_off(), ..FakeState::default() });
        assert_eq!(k.reconcile(false).await["action"], "restarting");
        assert!(calls(&k).is_empty());
    }

    #[tokio::test]
    async fn test_ensure_cuts_the_backoff_short_at_most_once_a_minute() {
        let k = keeper(FakeState { unit_state: backing_off(), ..FakeState::default() });
        assert_eq!(k.reconcile(true).await["action"], "kicked");
        assert_eq!(k.reconcile(true).await["action"], "restarting");
        k.host.state.lock().unwrap().clock += KICK_EVERY_S;
        assert_eq!(k.reconcile(true).await["action"], "kicked");
    }

    #[tokio::test]
    async fn test_a_transient_daemon_with_no_unit_gets_the_kept_one_launched_beside_it() {
        let k = keeper(FakeState { running: Some(daemon("transient", "session-1.scope", 9_000.0)), ..FakeState::default() });
        assert_eq!(k.reconcile(false).await["action"], "launched");
        assert!(!has_kill(&k));
    }

    #[tokio::test]
    async fn test_a_transient_daemon_that_will_not_yield_is_terminated_after_the_grace() {
        let state = UnitState { sub_state: Some("auto-restart".into()), ..unit(Some("loaded"), "activating") };
        let k = keeper(FakeState {
            running: Some(daemon("transient", "session-1.scope", 9_000.0)),
            unit_state: state,
            ..FakeState::default()
        });
        assert_eq!(k.reconcile(false).await["action"], "displacing");
        k.host.state.lock().unwrap().clock += DISPLACE_S - 1.0;
        assert_eq!(k.reconcile(false).await["action"], "displacing");
        k.host.state.lock().unwrap().clock += 2.0;
        assert_eq!(k.reconcile(false).await["action"], "displaced");
        assert!(calls(&k).contains(&call(&["kill", "42"])));
    }

    #[tokio::test]
    async fn test_a_service_daemon_in_a_hand_written_unit_is_adopted() {
        let k = keeper(FakeState { running: Some(daemon("service", "claude-daemon.service", 9_000.0)), ..FakeState::default() });
        assert_eq!(k.reconcile(false).await, json!({"action": "none", "pid": 42, "foreign_unit": "claude-daemon.service"}));
        assert!(calls(&k).is_empty());
    }

    #[tokio::test]
    async fn test_held_means_hands_off() {
        let k = keeper(FakeState { hold_state: Some(hold(9_900.0, Some(10_500.0), "rehearsal")), ..FakeState::default() });
        assert_eq!(k.reconcile(false).await["action"], "held");
        assert!(calls(&k).is_empty());
    }

    #[tokio::test]
    async fn test_hold_stops_the_unit_and_release_starts_it() {
        let state = UnitState { main_pid: Some(42), ..unit(None, "active") };
        let k = keeper(FakeState { running: Some(kept()), unit_state: state, ..FakeState::default() });
        let out = k.hold("rehearsal", HOLD_DEFAULT_S).await;
        assert_eq!(out["held"], true);
        assert!(calls(&k).contains(&call(&["systemctl", "stop", UNIT])));
        assert!(!has_kill(&k));
        {
            let mut s = k.host.state.lock().unwrap();
            s.running = None;
            s.unit_state = unit(Some("not-found"), "inactive");
        }
        let out = k.release().await;
        assert_eq!((&out["held"], &out["action"]), (&json!(false), &json!("launched")));
    }

    #[tokio::test]
    async fn test_hold_terminates_a_daemon_outside_the_unit() {
        let k = keeper(FakeState { running: Some(daemon("transient", "session-1.scope", 9_000.0)), ..FakeState::default() });
        k.hold("rehearsal", HOLD_DEFAULT_S).await;
        assert!(calls(&k).contains(&call(&["kill", "42"])));
    }

    #[tokio::test]
    async fn test_no_user_manager_launches_nothing_and_tries_linger_once() {
        let k = keeper(FakeState { has_bus: false, linger_state: Some(false), ..FakeState::default() });
        assert_eq!(k.reconcile(false).await["action"], "no-manager");
        k.reconcile(false).await;
        assert_eq!(calls(&k), vec![call(&["enable-linger"])]);
    }

    #[tokio::test(start_paused = true)]
    async fn test_ensure_reports_daemon_down_when_it_cannot_run() {
        let k = keeper(FakeState { hold_state: Some(hold(9_900.0, Some(10_500.0), "rehearsal")), ..FakeState::default() });
        let out = k.ensure(0.1).await;
        assert_eq!((&out["ok"], &out["reason"], &out["action"]), (&json!(false), &json!("daemon_down"), &json!("held")));
        assert!(out["hint"].as_str().unwrap().contains("daemon_release"));
    }

    #[tokio::test]
    async fn test_an_expired_hold_is_cleared_and_the_daemon_launched() {
        let k = keeper(FakeState { hold_state: Some(hold(9_000.0, Some(9_600.0), "rehearsal")), ..FakeState::default() });
        let out = k.reconcile(false).await;
        assert_eq!(out["action"], "launched");
        assert_eq!(out["hold_expired"]["reason"], "rehearsal");
        assert!(k.host.state.lock().unwrap().hold_state.is_none());
    }

    #[tokio::test]
    async fn test_a_hold_written_before_expiry_existed_ends_after_the_default() {
        let k = keeper(FakeState { hold_state: Some(hold(10_000.0 - HOLD_DEFAULT_S - 1.0, None, "old")), ..FakeState::default() });
        assert_eq!(k.reconcile(false).await["action"], "launched");
        let k = keeper(FakeState { hold_state: Some(hold(9_990.0, None, "old")), ..FakeState::default() });
        assert_eq!(k.reconcile(false).await["action"], "held");
    }

    #[tokio::test]
    async fn test_a_hold_past_the_orphan_grace_is_refused() {
        let state = UnitState { main_pid: Some(42), ..unit(None, "active") };
        let k = keeper(FakeState { running: Some(kept()), unit_state: state, ..FakeState::default() });
        let out = k.hold("maintenance", ORPHAN_GRACE_S as f64 + 1.0).await;
        assert_eq!(out["held"], false);
        assert!(out["error"].as_str().unwrap().contains("session dies"));
        assert!(k.host.state.lock().unwrap().hold_state.is_none());
        assert!(calls(&k).is_empty());
    }

    #[tokio::test]
    async fn test_a_hold_expires_and_warns() {
        let state = UnitState { main_pid: Some(42), ..unit(None, "active") };
        let k = keeper(FakeState { running: Some(kept()), unit_state: state, ..FakeState::default() });
        let out = k.hold("rehearsal", 120.0).await;
        assert_eq!(out["until"].as_f64(), Some(10_120.0));
        assert!(out["warning"].as_str().unwrap().contains("60 s"));
        assert_eq!(k.host.state.lock().unwrap().hold_state.as_ref().unwrap()["until"].as_f64(), Some(10_120.0));
    }

    /// The path a peer takes: one daemon_ensure, no ssh.
    #[tokio::test(start_paused = true)]
    async fn test_ensure_brings_back_a_daemon_after_its_holder_died() {
        let k = keeper(FakeState {
            hold_state: Some(hold(1.0, Some(601.0), "a holder that died")),
            start_on_launch: Some(kept()),
            ..FakeState::default()
        });
        let out = k.ensure(2.0).await;
        assert_eq!((&out["ok"], &out["pid"]), (&json!(true), &json!(42)));
        assert!(k.host.state.lock().unwrap().hold_state.is_none());
    }

    #[tokio::test]
    async fn test_the_daemon_and_the_user_manager_carry_the_orphan_grace() {
        let k = keeper(FakeState::default());
        k.reconcile(false).await;
        let check_ms = ORPHAN_GRACE_S * 1000 / ORPHAN_CHECKS;
        assert!(run_argv(&k).contains(&format!("--setenv=CLAUDE_PTY_ORPHAN_CHECK_MS={check_ms}")));
        let manager_env = k.host.state.lock().unwrap().manager_env.clone();
        assert_eq!(manager_env, BTreeMap::from([("CLAUDE_PTY_ORPHAN_CHECK_MS".to_string(), check_ms.to_string())]));
        const { assert!(ORPHAN_GRACE_S > 600, "the grace must outlast the 10 min restart backoff") };
    }

    #[tokio::test(start_paused = true)]
    async fn test_ensure_returns_once_the_daemon_answers() {
        let k = keeper(FakeState { running: Some(kept()), unit_state: unit(None, "active"), ..FakeState::default() });
        let out = k.ensure(0.1).await;
        assert_eq!((&out["ok"], &out["pid"]), (&json!(true), &json!(42)));
    }

    const LOG: &str = "\
[2026-10-19T21:34:28.606Z] [supervisor] shutting down (cause=upgrade, uptime=7322s, leases=0, live_workers=2)
[2026-10-19T21:34:39.307Z] [supervisor] ─── daemon start ─── version=9.0.1 pid=1001 origin=service
[2026-10-19T22:00:00.000Z] [bg] something else
[2026-10-20T02:40:43.397Z] [supervisor] ─── daemon start ─── version=9.0.1 pid=1002 origin=transient
[2026-10-20T03:00:00.000Z] [supervisor] shutting down (cause=signal, uptime=1157s, leases=1, live_workers=15)
";

    #[tokio::test]
    async fn test_status_reports_the_last_stop() {
        let k = keeper(FakeState { tail: LOG.into(), ..FakeState::default() });
        let out = k.status().await;
        assert_eq!(out["running"], false);
        assert_eq!(out["last_stop"]["cause"], "signal");
    }

    #[test]
    fn test_parse_show() {
        let out = parse_show("MainPID=0\nNRestarts=4\nResult=oom-kill\nActiveState=active\n");
        assert_eq!((out.main_pid, out.n_restarts, out.result.as_deref()), (Some(0), Some(4), Some("oom-kill")));
    }

    #[test]
    fn test_daemon_env_puts_local_bin_and_mamba_first() {
        let mut kn = knobs();
        kn.passed_env = vec![("AWM_WORKSPACE".into(), "/opt/awm".into())];
        let env: BTreeMap<String, String> = daemon_env(&kn, Path::new("/u"), Some(Path::new("/m"))).into_iter().collect();
        let parts: Vec<&str> = env["PATH"].split(':').collect();
        assert!(parts[0].ends_with("/.local/bin"));
        assert_eq!(parts[1], "/m/condabin");
        assert_eq!(env["AWM_WORKSPACE"], "/opt/awm");
        assert!(!env.contains_key("CLAUDE_CODE_SESSION_ID"));
    }

    #[tokio::test]
    async fn test_manifest_projects_into_the_reflection_tool() {
        let k = keeper(FakeState { running: Some(kept()), ..FakeState::default() });
        for verb in VERBS {
            assert!(crate::verbs::OPTIONAL_VERBS.contains(&verb), "{verb}");
            assert!(handle(&k, verb, &json!({"reason": "r"})).await.is_some(), "{verb}");
        }
        assert!(handle(&k, "daemon_other", &json!({})).await.is_none());
    }

    fn setenv(argv: &[String], key: &str) -> Option<String> {
        argv.iter().find_map(|a| a.strip_prefix(&format!("--setenv={key}=")).map(String::from))
    }

    #[tokio::test]
    async fn the_conda_root_knob_goes_into_the_unit() {
        let mut kn = knobs();
        kn.conda_root = Some("/conda".into());
        let k = keeper_with(FakeState { unit_env: "Environment=MAMBA_ROOT_PREFIX=/other\n".into(), ..FakeState::default() }, kn);
        k.reconcile(false).await;
        let argv = run_argv(&k);
        assert_eq!(setenv(&argv, "MAMBA_ROOT_PREFIX").as_deref(), Some("/conda"));
        assert!(setenv(&argv, "PATH").unwrap().starts_with("/u/.local/bin:/conda/condabin:/conda/bin:"));
    }

    #[tokio::test]
    async fn without_the_knob_the_conda_root_comes_from_the_daemon_unit() {
        let unit_env = "Environment=PATH=/usr/bin MAMBA_ROOT_PREFIX=/opt/conda CLAUDE_PTY_ORPHAN_CHECK_MS=40000\n";
        let k = keeper(FakeState { unit_env: unit_env.into(), ..FakeState::default() });
        k.reconcile(false).await;
        assert_eq!(setenv(&run_argv(&k), "MAMBA_ROOT_PREFIX").as_deref(), Some("/opt/conda"));
        assert!(!k.state.lock().await.conda_warned);

        let state = UnitState { n_restarts: Some(0), ..unit(Some("loaded"), "active") };
        let k = keeper(FakeState { running: Some(kept()), unit_state: state, unit_env: unit_env.into(), ..FakeState::default() });
        assert_eq!(k.reconcile(false).await["action"], "none");
        {
            let mut s = k.host.state.lock().unwrap();
            s.running = None;
            s.unit_state = unit(Some("not-found"), "inactive");
            s.unit_env = "Environment=\n".into();
        }
        assert_eq!(k.reconcile(false).await["action"], "launched");
        assert_eq!(setenv(&run_argv(&k), "MAMBA_ROOT_PREFIX").as_deref(), Some("/opt/conda"), "learned from the kept unit");
    }

    #[tokio::test]
    async fn with_no_conda_root_anywhere_the_unit_launches_without_it_and_warns_once() {
        let k = keeper(FakeState::default());
        assert_eq!(k.reconcile(false).await["action"], "launched");
        let argv = run_argv(&k);
        assert_eq!(setenv(&argv, "MAMBA_ROOT_PREFIX"), None);
        assert_eq!(setenv(&argv, "PATH").as_deref(), Some("/u/.local/bin:/usr/local/bin:/usr/bin:/bin"));
        assert!(k.state.lock().await.conda_warned);
        k.reconcile(false).await;
        assert!(k.state.lock().await.conda_warned);
    }

    #[tokio::test(start_paused = true)]
    async fn ensure_waits_at_most_55_seconds_whatever_the_caller_asks() {
        let k = keeper(FakeState { unit_state: backing_off(), ..FakeState::default() });
        let start = Instant::now();
        let out = daemon_ensure(&k, &json!({"timeout": 300})).await;
        let waited = start.elapsed().as_secs_f64();
        assert_eq!((&out["ok"], &out["reason"]), (&json!(false), &json!("daemon_down")));
        assert!((ENSURE_CAP_S..ENSURE_CAP_S + 2.0).contains(&waited), "{waited}");
    }

    #[tokio::test]
    async fn a_hold_needs_a_reason() {
        let k = keeper(FakeState::default());
        assert_eq!(daemon_hold(&k, &json!({"reason": "  "})).await, json!({"ok": false, "error": "a hold needs a reason"}));
        let out = daemon_hold(&k, &json!({"reason": "drill", "seconds": "120"})).await;
        assert_eq!(out["until"].as_f64(), Some(10_120.0));
    }

    #[tokio::test]
    async fn another_keepers_lock_turns_this_one_off() {
        let k = keeper(FakeState { lock_free: false, ..FakeState::default() });
        assert_eq!(k.reconcile(false).await["reason"], "another keeper holds the lock for this Claude home");
        assert!(calls(&k).is_empty());
    }

    #[test]
    fn the_lock_is_an_flock_another_open_cannot_take() {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("awm-keep.lock");
        let first = lock_file(&path).expect("the first lock");
        assert!(lock_file(&path).is_none());
        drop(first);
        assert!(lock_file(&path).is_some());
    }
}
