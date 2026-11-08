//! Starting one background Claude Code session.
//!
//! This is the step that can damage the user's work, so read the precondition before changing
//! anything here.
//!
//! Every background Claude Code session on a node is a child of the *first* process that
//! launched one after the reboot — the daemon it started. That daemon donates its control group
//! and its environment to every session that follows. If a long-running service were ever the
//! process that started it, then restarting that service would kill every session the user is
//! working in, and every session on the box would come up with that service's environment.
//!
//! So a launch is refused unless a daemon is already running. No daemon means nobody is using
//! Claude Code on that node, and a cold first launch is the right answer there. The transient
//! unit below is defence in depth for the residual race, where the daemon dies between the check
//! and the launch.
//!
//! This module decides nothing about which session to start or whether the caller may start it.
//! It takes the directory, name, flags, environment and prompt in a [`LaunchSpec`], and returns
//! the job the daemon registered.

use std::collections::HashSet;
use std::future::Future;
use std::path::PathBuf;
use std::time::Duration;

use awm_plugin_sdk::{proc, procfs};
use tokio::time::{sleep, timeout, Instant};

use super::roster::{self, Job};
use super::ClaudeHome;

/// A launch answers in about four seconds. The ceiling is generous because the cost of giving up
/// early is a second session nobody asked for.
pub const LAUNCH_TIMEOUT: Duration = Duration::from_secs(45);
/// How often the roster is read while waiting for the new job.
pub const POLL: Duration = Duration::from_millis(250);

const DEFAULT_UNIT_PREFIX: &str = "awm-claude";

/// Why a launch or respawn did not produce a job.
#[derive(Debug, thiserror::Error)]
pub enum LaunchError {
    /// The precondition on launching was not met; nothing was spawned.
    #[error("{0}")]
    Refused(String),
    /// The launcher ran, but no matching job appeared in the roster in time.
    #[error("{0}")]
    Timeout(String),
    /// The launcher could not be started at all.
    #[error("{0}")]
    Spawn(#[from] std::io::Error),
}

/// What the host can tell about reaching the daemon: its user systemd manager and mount
/// namespaces. Injected so tests can drive both.
pub trait HostProbe {
    /// The environment `systemd-run --user` needs, or None when there is no user manager.
    fn user_manager_env(&self) -> Option<Vec<(String, String)>>;
    /// Whether `pid` shares this process's mount namespace; None when unreadable.
    fn same_mount_ns(&self, pid: u32) -> Option<bool>;
}

/// The real host, read through the SDK.
#[derive(Debug, Clone, Copy, Default)]
pub struct SystemHost;

impl HostProbe for SystemHost {
    fn user_manager_env(&self) -> Option<Vec<(String, String)>> {
        proc::user_manager_env()
    }

    fn same_mount_ns(&self, pid: u32) -> Option<bool> {
        procfs::same_mount_ns(pid)
    }
}

/// One command to run: its argv, its working directory, and the variables set on top of this
/// process's own environment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaunchCommand {
    pub argv: Vec<String>,
    pub cwd: PathBuf,
    pub env: Vec<(String, String)>,
}

/// Runs the launcher. Injected so tests never start a real `claude`.
pub trait Spawner {
    /// Start `cmd` and wait up to `limit` for it to exit, killing it after that; an error only
    /// when it could not be started.
    fn run(&self, cmd: &LaunchCommand, limit: Duration) -> impl Future<Output = std::io::Result<()>> + Send;
}

/// The real spawner, on `tokio::process`, with output discarded.
#[derive(Debug, Clone, Copy, Default)]
pub struct ProcessSpawner;

impl Spawner for ProcessSpawner {
    async fn run(&self, cmd: &LaunchCommand, limit: Duration) -> std::io::Result<()> {
        let Some((program, args)) = cmd.argv.split_first() else {
            return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, "empty argv"));
        };
        let mut child = tokio::process::Command::new(program)
            .args(args)
            .current_dir(&cmd.cwd)
            .envs(cmd.env.iter().map(|(k, v)| (k, v)))
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .kill_on_drop(true)
            .spawn()?;
        if timeout(limit, child.wait()).await.is_err() {
            let _ = child.kill().await;
        }
        Ok(())
    }
}

/// Everything about one launch that is the caller's to decide.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaunchSpec {
    /// The `claude` binary to run.
    pub claude: PathBuf,
    /// The session's working directory.
    pub cwd: PathBuf,
    /// The job name, passed as `--name=<name>`.
    pub name: String,
    /// Further `claude` flags, placed after the name.
    pub flags: Vec<String>,
    /// Variables set for the session on top of the launcher's environment.
    pub env: Vec<(String, String)>,
    /// The first prompt, or None to start the session empty.
    pub prompt: Option<String>,
    /// The prefix of the transient unit's name.
    pub unit_prefix: String,
    /// The user's home directory, set as the launcher's `HOME`.
    pub home_dir: PathBuf,
    /// How the session is named in a timeout message, such as `background` or `warm`.
    pub label: String,
    /// How long to wait for the launcher, and then again for the job to appear.
    pub timeout: Duration,
}

impl LaunchSpec {
    /// A spec with no flags, env or prompt, the default unit prefix, `$HOME`, the label
    /// `background` and [`LAUNCH_TIMEOUT`].
    pub fn new(claude: impl Into<PathBuf>, cwd: impl Into<PathBuf>, name: impl Into<String>) -> Self {
        Self {
            claude: claude.into(),
            cwd: cwd.into(),
            name: name.into(),
            flags: Vec::new(),
            env: Vec::new(),
            prompt: None,
            unit_prefix: DEFAULT_UNIT_PREFIX.into(),
            home_dir: user_home(),
            label: "background".into(),
            timeout: LAUNCH_TIMEOUT,
        }
    }
}

/// Everything about one respawn that is the caller's to decide.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RespawnSpec {
    /// The `claude` binary to run.
    pub claude: PathBuf,
    /// The prefix of the transient unit's name.
    pub unit_prefix: String,
    /// The user's home directory: the launcher's working directory and `HOME`.
    pub home_dir: PathBuf,
    /// How long to wait for the launcher, and then again for the job to come back.
    pub timeout: Duration,
}

impl RespawnSpec {
    /// A spec with the default unit prefix, `$HOME` and [`LAUNCH_TIMEOUT`].
    pub fn new(claude: impl Into<PathBuf>) -> Self {
        Self { claude: claude.into(), unit_prefix: DEFAULT_UNIT_PREFIX.into(), home_dir: user_home(), timeout: LAUNCH_TIMEOUT }
    }
}

fn user_home() -> PathBuf {
    std::env::var_os("HOME").filter(|h| !h.is_empty()).map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

/// The `claude` on `PATH`, else `~/.local/bin/claude`.
pub fn default_claude_bin() -> PathBuf {
    let on_path = std::env::var_os("PATH")
        .and_then(|p| std::env::split_paths(&p).map(|d| d.join("claude")).find(|c| is_executable(c)));
    on_path.unwrap_or_else(|| user_home().join(".local").join("bin").join("claude"))
}

fn is_executable(path: &std::path::Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

/// Why a launch must not happen right now, or None if it may.
///
/// With no user manager the launch runs straight from this process, and it joins the running
/// daemon only if it can see that daemon's sockets. A service under a hardened unit has a mount
/// namespace of its own, so its launch starts a second daemon inside its own control group,
/// which takes the user's sessions down with the next restart.
pub fn daemon_refusal(home: &ClaudeHome, host: &impl HostProbe) -> Option<String> {
    let Some(pid) = roster::daemon_pid(home) else {
        return Some(
            "no claude code daemon is running — launching now would put every background session \
             on this node inside awm's control group"
                .into(),
        );
    };
    if host.user_manager_env().is_none() && !host.same_mount_ns(pid).unwrap_or(false) {
        return Some(format!(
            "the claude code daemon (pid {pid}) is not reachable from here: there is no user \
             systemd manager, and the daemon runs in another mount namespace — launching now \
             would start a second daemon inside awm's control group"
        ));
    }
    None
}

/// A command ready to run, how to describe where it runs, and the environment the command
/// itself needs to reach the user manager.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Invocation {
    pub argv: Vec<String>,
    pub how: String,
    pub env: Vec<(String, String)>,
}

/// The command that starts the session `spec` describes, wrapped for the user manager `bus`.
///
/// `KillMode=process` is the property that matters. A transient unit torn down the default way
/// kills its whole control group, and in the race this unit exists to cover — the daemon having
/// just died — that group holds the fresh daemon and the session it was launching. That is
/// exactly the defect that had a timer seed a session and kill it 0.3 seconds later, silently,
/// every fifteen minutes.
///
/// The prompt goes after a `--`. Without it the CLI drops the prompt silently and the session
/// starts empty.
pub fn build_argv(spec: &LaunchSpec, bus: Option<Vec<(String, String)>>) -> Invocation {
    let mut cmd = vec![spec.claude.to_string_lossy().into_owned(), "--bg".into(), format!("--name={}", spec.name)];
    cmd.extend(spec.flags.iter().cloned());
    if let Some(prompt) = spec.prompt.as_deref().filter(|p| !p.is_empty()) {
        cmd.extend(["--".into(), prompt.into()]);
    }
    unit_argv(cmd, &spec.cwd, &spec.env, &spec.unit_prefix, bus)
}

/// `cmd` wrapped in a transient user unit when there is a user manager `bus`, else as it is.
pub fn unit_argv(
    cmd: Vec<String>,
    cwd: &std::path::Path,
    env: &[(String, String)],
    unit_prefix: &str,
    bus: Option<Vec<(String, String)>>,
) -> Invocation {
    let Some(bus) = bus.filter(|b| !b.is_empty()) else {
        return Invocation { argv: cmd, how: "directly (no user systemd manager)".into(), env: Vec::new() };
    };
    let unit = format!("{unit_prefix}-{}", unit_suffix());
    let mut argv: Vec<String> = [
        "systemd-run",
        "--user",
        "--quiet",
        "--collect",
        &format!("--unit={unit}"),
        "--property=Restart=no",
        "--property=KillMode=process",
        "--nice=19",
        &format!("--working-directory={}", cwd.display()),
    ]
    .map(String::from)
    .into();
    argv.extend(env.iter().map(|(k, v)| format!("--setenv={k}={v}")));
    argv.push("--".into());
    argv.extend(cmd);
    Invocation { argv, how: format!("into user unit {unit}.service"), env: bus }
}

fn unit_suffix() -> String {
    let mut bytes = [0u8; 4];
    // SAFETY: getrandom writes at most `bytes.len()` bytes into `bytes`.
    let n = unsafe { libc::getrandom(bytes.as_mut_ptr().cast(), bytes.len(), 0) };
    if n != bytes.len() as isize {
        let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
        bytes = (nanos ^ std::process::id().rotate_left(16)).to_be_bytes();
    }
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn command(inv: Invocation, cwd: PathBuf, env: &[(String, String)], home_dir: &std::path::Path) -> LaunchCommand {
    let mut all = env.to_vec();
    all.extend(inv.env);
    all.push(("HOME".into(), home_dir.to_string_lossy().into_owned()));
    LaunchCommand { argv: inv.argv, cwd, env: all }
}

async fn wait_for(home: &ClaudeHome, limit: Duration, found: impl Fn(&Job) -> bool) -> Option<Job> {
    let deadline = Instant::now() + limit;
    while Instant::now() < deadline {
        if let Some(job) = roster::load(home).into_iter().find(&found) {
            return Some(job);
        }
        sleep(POLL).await;
    }
    None
}

/// Start one background session and return the roster's record of it.
///
/// The exit status of the launch is deliberately not trusted: the session is the daemon's child,
/// not the launcher's, so the only proof that one exists is a new record in the roster. `accept`
/// narrows which new record counts.
pub async fn launch(
    home: &ClaudeHome,
    spec: &LaunchSpec,
    accept: impl Fn(&Job) -> bool,
    spawner: &impl Spawner,
    host: &impl HostProbe,
) -> Result<Job, LaunchError> {
    if let Some(refusal) = daemon_refusal(home, host) {
        return Err(LaunchError::Refused(refusal));
    }
    let limit = spec.timeout;
    let before: HashSet<String> = roster::load(home).into_iter().map(|j| j.short).collect();
    let inv = build_argv(spec, host.user_manager_env());
    tracing::info!("claude launch: launching {} {}", spec.name, inv.how);
    let cmd = command(inv, spec.cwd.clone(), &spec.env, &spec.home_dir);
    spawner.run(&cmd, limit).await?;

    wait_for(home, limit, |j| !before.contains(&j.short) && accept(j)).await.ok_or_else(|| {
        LaunchError::Timeout(format!("no new {} session appeared within {:.0}s", spec.label, limit.as_secs_f64()))
    })
}

fn alive(job: &Job) -> bool {
    let Some(start) = job.repl_proc_start.as_deref().filter(|s| !s.is_empty()) else { return false };
    job.repl_pid.and_then(procfs::start_ticks).as_deref() == Some(start)
}

/// Bring a stopped background job back under its own id, conversation intact.
///
/// `claude respawn` keeps the job id, the transcript and the launch flags. `claude --bg --resume
/// <session>` does not: it starts a new, empty job. A respawn of a live job restarts it, so a
/// live job is refused.
pub async fn respawn(
    home: &ClaudeHome,
    job: &str,
    spec: &RespawnSpec,
    spawner: &impl Spawner,
    host: &impl HostProbe,
) -> Result<Job, LaunchError> {
    if let Some(refusal) = daemon_refusal(home, host) {
        return Err(LaunchError::Refused(refusal));
    }
    let before = roster::load(home).into_iter().find(|j| j.short == job);
    if before.as_ref().is_some_and(alive) {
        return Err(LaunchError::Refused(format!("job {job} is running")));
    }
    let stale = before.and_then(|j| j.repl_proc_start);

    let limit = spec.timeout;
    let cmd = vec![spec.claude.to_string_lossy().into_owned(), "respawn".into(), job.into()];
    let inv = unit_argv(cmd, &spec.home_dir, &[], &spec.unit_prefix, host.user_manager_env());
    tracing::info!("claude launch: respawning {job} {}", inv.how);
    let cmd = command(inv, spec.home_dir.clone(), &[], &spec.home_dir);
    spawner.run(&cmd, limit).await?;

    wait_for(home, limit, |j| j.short == job && j.repl_proc_start != stale && alive(j))
        .await
        .ok_or_else(|| LaunchError::Timeout(format!("job {job} did not come back within {:.0}s", limit.as_secs_f64())))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::claude::records::tests::{home, me, own_start, DEAD_PID};
    use serde_json::{json, Value};
    use std::sync::Mutex;

    // A host with a fixed user bus, and a fixed mount-namespace answer or (None) the real one.
    struct Host {
        bus: Option<Vec<(String, String)>>,
        ns: Option<Option<bool>>,
    }

    impl HostProbe for Host {
        fn user_manager_env(&self) -> Option<Vec<(String, String)>> {
            self.bus.clone()
        }
        fn same_mount_ns(&self, pid: u32) -> Option<bool> {
            self.ns.unwrap_or_else(|| procfs::same_mount_ns(pid))
        }
    }

    fn no_bus() -> Host {
        Host { bus: None, ns: None }
    }

    fn bus() -> Host {
        Host { bus: Some(vec![("XDG_RUNTIME_DIR".into(), "/x".into())]), ns: None }
    }

    type Hook = Box<dyn Fn() + Send + Sync>;

    // Records each spawn and runs `on_spawn`, which writes what a real daemon would.
    #[derive(Default)]
    struct Spawns {
        calls: Mutex<Vec<LaunchCommand>>,
        on_spawn: Option<Hook>,
    }

    impl Spawns {
        fn on(hook: impl Fn() + Send + Sync + 'static) -> Self {
            Self { calls: Mutex::default(), on_spawn: Some(Box::new(hook)) }
        }
        fn calls(&self) -> Vec<LaunchCommand> {
            self.calls.lock().unwrap().clone()
        }
    }

    impl Spawner for Spawns {
        async fn run(&self, cmd: &LaunchCommand, _limit: Duration) -> std::io::Result<()> {
            self.calls.lock().unwrap().push(cmd.clone());
            if let Some(hook) = &self.on_spawn {
                hook();
            }
            Ok(())
        }
    }

    fn roster_file(h: &ClaudeHome, workers: Value, supervisor: Option<u32>) {
        let doc = json!({"supervisorPid": supervisor.unwrap_or_else(me), "workers": workers});
        std::fs::write(h.roster(), doc.to_string()).unwrap();
    }

    fn worker(started: i64) -> Value {
        json!({"replPid": me(), "startedAt": started})
    }

    fn live(proc_start: Option<&str>) -> Value {
        json!({"replPid": me(), "replProcStart": proc_start.map(String::from).unwrap_or_else(own_start), "startedAt": 1})
    }

    fn spec(h: &ClaudeHome, name: &str) -> LaunchSpec {
        LaunchSpec { home_dir: h.root.clone(), ..LaunchSpec::new("/bin/claude", h.root.clone(), name) }
    }

    fn respawn_spec(h: &ClaudeHome) -> RespawnSpec {
        RespawnSpec { home_dir: h.root.clone(), ..RespawnSpec::new("/bin/claude") }
    }

    fn any(_: &Job) -> bool {
        true
    }

    #[test]
    fn a_daemon_that_cannot_be_reached_is_a_refusal() {
        let (_d, h) = home();
        assert!(daemon_refusal(&h, &bus()).unwrap().contains("no claude code daemon"));
        roster_file(&h, json!({}), Some(DEAD_PID));
        assert!(daemon_refusal(&h, &bus()).unwrap().contains("no claude code daemon"));

        roster_file(&h, json!({}), None);
        assert_eq!(daemon_refusal(&h, &no_bus()), None, "a daemon in this namespace launches directly");
        let refusal = daemon_refusal(&h, &Host { bus: None, ns: Some(Some(false)) }).unwrap();
        assert!(refusal.contains("not reachable") && refusal.contains("mount namespace"), "{refusal}");
        assert!(daemon_refusal(&h, &Host { bus: None, ns: Some(None) }).unwrap().contains("not reachable"));
        assert_eq!(daemon_refusal(&h, &Host { ns: Some(Some(false)), ..bus() }), None, "a user bus reaches it");
    }

    #[tokio::test(start_paused = true)]
    async fn nothing_is_spawned_without_a_reachable_daemon_or_for_a_live_job() {
        let (_d, h) = home();
        let spawns = Spawns::default();
        let err = launch(&h, &spec(&h, "n"), any, &spawns, &bus()).await.unwrap_err();
        assert!(matches!(&err, LaunchError::Refused(m) if m.contains("no claude code daemon")), "{err}");
        let err = respawn(&h, "seat1", &respawn_spec(&h), &spawns, &bus()).await.unwrap_err();
        assert!(matches!(&err, LaunchError::Refused(m) if m.contains("no claude code daemon")), "{err}");

        roster_file(&h, json!({"seat1": live(None)}), None);
        let err = respawn(&h, "seat1", &respawn_spec(&h), &spawns, &bus()).await.unwrap_err();
        assert!(matches!(&err, LaunchError::Refused(m) if m.contains("running")), "{err}");

        let host = Host { bus: None, ns: Some(Some(false)) };
        let err = launch(&h, &spec(&h, "n"), any, &spawns, &host).await.unwrap_err();
        assert!(matches!(&err, LaunchError::Refused(m) if m.contains("not reachable")), "{err}");
        assert!(spawns.calls().is_empty());
    }

    #[test]
    fn the_command_line_keeps_systemd_run_claude_and_the_prompt_apart() {
        let s = LaunchSpec {
            flags: vec!["--effort".into(), "low".into()],
            env: vec![("ANTHROPIC_MODEL".into(), "m".into())],
            prompt: Some("do it".into()),
            unit_prefix: "awm-test".into(),
            ..LaunchSpec::new("/bin/claude", "/w", "n")
        };
        let inv = build_argv(&s, bus().bus);
        assert_eq!(inv.argv[..3], ["systemd-run", "--user", "--quiet"]);
        assert!(inv.argv.iter().any(|a| a == "--property=KillMode=process"));
        assert!(inv.argv.iter().any(|a| a == "--working-directory=/w"));
        assert!(inv.argv.iter().any(|a| a == "--setenv=ANTHROPIC_MODEL=m"));
        let unit = inv.argv.iter().find_map(|a| a.strip_prefix("--unit=awm-test-")).unwrap();
        assert!(unit.len() == 8 && unit.bytes().all(|b| b.is_ascii_hexdigit()), "{unit}");
        assert_eq!(inv.env, [("XDG_RUNTIME_DIR".to_string(), "/x".to_string())]);
        // Everything before the first `--` belongs to systemd-run, everything after to claude, and
        // the prompt follows a second `--` or the CLI drops it.
        let sep = inv.argv.iter().position(|a| a == "--").unwrap();
        assert_eq!(inv.argv[sep + 1..], ["/bin/claude", "--bg", "--name=n", "--effort", "low", "--", "do it"]);

        let inv = build_argv(&LaunchSpec::new("/bin/claude", "/w", "--dangerously-skip-permissions"), None);
        assert_eq!(inv.argv, ["/bin/claude", "--bg", "--name=--dangerously-skip-permissions"]);
        assert!(inv.env.is_empty() && inv.how.contains("directly"));

        let cmd = vec!["/bin/claude".to_string(), "respawn".into(), "j".into()];
        let inv = unit_argv(cmd, std::path::Path::new("/h"), &[], "awm-test-resume", bus().bus);
        assert!(inv.argv.iter().any(|a| a == "--property=KillMode=process"));
    }

    #[tokio::test(start_paused = true)]
    async fn launch_returns_the_job_that_appears_after_the_spawn() {
        let (_d, h) = home();
        roster_file(&h, json!({"old": worker(1)}), None);
        let hh = h.clone();
        let spawns = Spawns::on(move || roster_file(&hh, json!({"old": worker(1), "fresh": worker(2)}), None));
        let s = LaunchSpec {
            flags: vec!["--x".into()],
            env: vec![("K".into(), "v".into())],
            prompt: Some("hi".into()),
            timeout: Duration::from_secs(2),
            ..spec(&h, "n")
        };
        let job = launch(&h, &s, any, &spawns, &no_bus()).await.unwrap();
        assert_eq!(job.short, "fresh");
        let call = &spawns.calls()[0];
        assert_eq!(call.cwd, h.root);
        assert!(call.env.contains(&("K".into(), "v".into())));
        assert!(call.env.contains(&("HOME".into(), h.root.to_string_lossy().into_owned())));
        assert!(call.argv.iter().any(|a| a == "/bin/claude") && call.argv.iter().any(|a| a == "hi"));

        // A job the caller rejects does not count; no new job is a timeout.
        roster_file(&h, json!({}), None);
        let hh = h.clone();
        let spawns = Spawns::on(move || roster_file(&hh, json!({"x": worker(1)}), None));
        let s = LaunchSpec { label: "warm".into(), timeout: Duration::from_millis(200), ..spec(&h, "n") };
        let err = launch(&h, &s, |_| false, &spawns, &no_bus()).await.unwrap_err();
        assert!(matches!(&err, LaunchError::Timeout(m) if m.contains("no new warm session")), "{err}");
        roster_file(&h, json!({}), None);
        let s = LaunchSpec { timeout: Duration::from_millis(100), ..spec(&h, "n") };
        let err = launch(&h, &s, any, &Spawns::default(), &no_bus()).await.unwrap_err();
        assert!(matches!(&err, LaunchError::Timeout(m) if m.contains("no new background session")), "{err}");

        struct Broken;
        impl Spawner for Broken {
            async fn run(&self, _: &LaunchCommand, _: Duration) -> std::io::Result<()> {
                Err(std::io::Error::from(std::io::ErrorKind::NotFound))
            }
        }
        assert!(matches!(launch(&h, &spec(&h, "n"), any, &Broken, &no_bus()).await, Err(LaunchError::Spawn(_))));
    }

    #[tokio::test(start_paused = true)]
    async fn respawn_waits_for_the_same_job_to_come_back_past_a_stale_entry() {
        let (_d, h) = home();
        roster_file(&h, json!({"other": live(None)}), None);
        let hh = h.clone();
        let spawns = Spawns::on(move || roster_file(&hh, json!({"other": live(None), "seat1": live(None)}), None));
        let s = RespawnSpec { timeout: Duration::from_secs(2), ..respawn_spec(&h) };
        let job = respawn(&h, "seat1", &s, &spawns, &no_bus()).await.unwrap();
        assert_eq!(job.short, "seat1");
        let call = &spawns.calls()[0];
        assert_eq!(call.argv, ["/bin/claude", "respawn", "seat1"]);
        assert_eq!(call.cwd, h.root);

        roster_file(&h, json!({"seat1": live(Some("dead"))}), None);
        let spawns = Spawns::default();
        let s = RespawnSpec { timeout: Duration::from_millis(100), ..respawn_spec(&h) };
        let err = respawn(&h, "seat1", &s, &spawns, &no_bus()).await.unwrap_err();
        assert!(matches!(&err, LaunchError::Timeout(m) if m.contains("seat1 did not come back")), "{err}");
        assert_eq!(spawns.calls().len(), 1);
    }
}
