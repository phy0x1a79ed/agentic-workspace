//! Keep this host's tether binary running: `tether-operator` on a member node, `tether-relay`
//! on the public host.
//!
//! Both binaries are adopted, not owned. The operator daemon holds machine sessions as sockets,
//! and the relay holds every room's backlog in memory, so a binary parented here would lose them
//! on every deploy. Each runs instead in a transient unit of the user's systemd manager, outside
//! the gateway's cgroup. A start attaches to a binary that answers and launches one only when
//! none does. It replaces a running one only when it is stale and no session or room is live.
//! Stopping the plugin leaves the binary running. Without a user manager the binary starts in
//! its own session, and then a cgroup kill of the plugin's unit still ends it.
//!
//! The daemon reads its environment once, at launch. `daemon.env` is written from an allowlist
//! with no secret in it: a secret the binary needs reaches the transient unit by name through
//! `systemd-run --setenv`, which keeps it in the manager's runtime directory, not on disk.

use std::future::Future;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;
use std::pin::Pin;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use super::paths::{Paths, Role};

pub type BoxFut<T> = Pin<Box<dyn Future<Output = T> + Send>>;

/// What a probe saw. Busy counts as alive: replacing a daemon that is slow to answer would end
/// the sessions adoption keeps.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Probe {
    Absent,
    Busy,
    Answering,
}

pub type ProbeFn = Arc<dyn Fn(Duration) -> BoxFut<(Probe, Value)> + Send + Sync>;

pub const TICK: Duration = Duration::from_secs(5);
const START_TIMEOUT: Duration = Duration::from_secs(15);
pub const PROBE_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_BACKOFF: Duration = Duration::from_secs(300);
/// A binary that dies younger than this counts as a failed launch, so one that binds and then
/// exits backs off instead of cycling every tick.
const SETTLE: Duration = Duration::from_secs(60);

/// The secrets a tether binary may need. They never reach `daemon.env`.
const SECRETS: [&str; 1] = ["AWM_TETHER_ISSUE_TOKEN"];
const KEEP: [&str; 19] = [
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TZ", "TMPDIR", "TERM", "RUST_LOG", "RUST_BACKTRACE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
];
const KEEP_PREFIX: [&str; 4] = ["AWM_", "TETHER_", "LC_", "XDG_"];
/// The variables a running binary must share with this launch to count as current: the slot
/// issuer, and the gateway's egress proxy, whose port changes each time the gateway restarts.
const PINNED: [&str; 9] = [
    "TETHER_ISSUER", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
];

/// How the binary is launched: its environment and the secrets it gets by name.
#[derive(Debug, Clone, Default)]
pub struct Launch {
    pub env: Vec<(String, String)>,
    pub secrets: Vec<(String, String)>,
    /// Hand the binary to the user's systemd manager when there is one. Tests run without.
    pub use_manager: bool,
}

fn valid_name(k: &str) -> bool {
    let mut chars = k.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphabetic() || c == '_') && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

impl Launch {
    /// The launch for `paths`' role from this process's environment. `issuer` is the argv the
    /// operator daemon runs for each slot.
    pub fn from_env(paths: &Paths, issuer: Option<Vec<String>>) -> Self {
        let (mut env, mut secrets) = (vec![], vec![]);
        for (k, v) in std::env::vars() {
            if SECRETS.contains(&k.as_str()) {
                secrets.push((k, v));
            } else if valid_name(&k) && (KEEP.contains(&k.as_str()) || KEEP_PREFIX.iter().any(|p| k.starts_with(p))) {
                env.push((k, v));
            }
        }
        let mut set = |k: &str, v: String, overwrite: bool| {
            if let Some(slot) = env.iter_mut().find(|(n, _)| n == k) {
                if overwrite {
                    slot.1 = v;
                }
            } else {
                env.push((k.into(), v));
            }
        };
        set("TETHER_CONTROL_SOCKET", paths.control_socket().display().to_string(), true);
        match paths.role {
            Role::Operator => {
                if let Some(argv) = &issuer {
                    set("TETHER_ISSUER", serde_json::to_string(argv).unwrap_or_default(), true);
                }
            }
            Role::Relay => {
                set("AWM_TETHER_ROOM_STATE", paths.room_state().display().to_string(), false);
                set("AWM_TETHER_ASSETS", paths.assets_dir.display().to_string(), false);
            }
        }
        env.sort();
        Self { env, secrets, use_manager: true }
    }

    /// `env` in systemd's EnvironmentFile syntax: each value double-quoted with `\` and `"`
    /// escaped, which systemd 249 and 255 both read back byte for byte, newlines and `$` included.
    pub fn environment_file(&self) -> String {
        let mut out = String::new();
        for (k, v) in &self.env {
            if valid_name(k) {
                out += &format!("{k}=\"{}\"\n", v.replace('\\', "\\\\").replace('"', "\\\""));
            }
        }
        out
    }
}

fn exe(pid: u32) -> Option<String> {
    std::fs::read_link(format!("/proc/{pid}/exe")).ok().map(|p| p.display().to_string())
}

/// Whether `pid` still runs `binary`, not a process that reused its number.
fn runs(pid: u32, binary: Option<&str>) -> bool {
    match std::fs::read(format!("/proc/{pid}/cmdline")) {
        Err(_) => true,
        Ok(raw) => binary.is_some_and(|b| !b.is_empty() && raw.split(|c| *c == 0).any(|a| a == b.as_bytes())),
    }
}

fn alive(pid: Option<u32>) -> bool {
    let Some(pid) = pid.filter(|p| *p > 0) else { return false };
    // SAFETY: kill with signal 0 only checks that the process exists.
    let rc = unsafe { libc::kill(pid as libc::pid_t, 0) };
    rc == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

fn signal(pid: u32, sig: i32) {
    // SAFETY: kill takes plain integers and reports failure by return value.
    unsafe {
        libc::kill(pid as libc::pid_t, sig);
    }
}

/// The environment a running process was started with, or None when it cannot be read.
fn environ(pid: u32) -> Option<Vec<(String, String)>> {
    let raw = std::fs::read(format!("/proc/{pid}/environ")).ok()?;
    let text = String::from_utf8_lossy(&raw);
    Some(text.split('\0').filter_map(|kv| kv.split_once('=')).map(|(k, v)| (k.to_string(), v.to_string())).collect())
}

fn lookup<'a>(env: &'a [(String, String)], key: &str) -> Option<&'a str> {
    env.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
}

fn live_sessions(report: &Value) -> usize {
    report.get("sessions").and_then(Value::as_array).map_or(0, |s| s.iter().filter(|s| s.get("phase").and_then(Value::as_str) != Some("ended")).count())
}

#[derive(Default)]
struct Inner {
    child: Option<tokio::process::Child>,
    error: Option<String>,
    adopted: Option<bool>,
    pending: Option<String>,
    failures: u32,
    next_try: Option<Instant>,
    started: Option<Instant>,
}

pub struct Adopted {
    pub paths: Paths,
    probe: ProbeFn,
    launch: Launch,
    inner: tokio::sync::Mutex<Inner>,
}

async fn systemctl(bus: &[(String, String)], args: &[&str], timeout: Duration) -> Option<std::process::Output> {
    let mut cmd = tokio::process::Command::new("systemctl");
    cmd.arg("--user").args(args).envs(bus.iter().cloned()).kill_on_drop(true);
    tokio::time::timeout(timeout, cmd.output()).await.ok()?.ok()
}

impl Adopted {
    pub fn new(paths: Paths, probe: ProbeFn, launch: Launch) -> Arc<Self> {
        Arc::new(Self { paths, probe, launch, inner: Default::default() })
    }

    fn bus(&self) -> Option<Vec<(String, String)>> {
        self.launch.use_manager.then(awm_plugin_sdk::proc::user_manager_env).flatten()
    }

    fn launched(&self) -> Value {
        std::fs::read_to_string(self.paths.launch_file()).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or(json!({}))
    }

    async fn unit(&self, bus: Option<&[(String, String)]>) -> Option<Value> {
        let bus = bus?;
        let name = format!("{}.service", self.paths.unit());
        let out = systemctl(bus, &["show", &name, "-p", "MainPID", "-p", "ActiveState"], Duration::from_secs(10)).await?;
        let text = String::from_utf8_lossy(&out.stdout);
        let field = |k: &str| text.lines().find_map(|l| l.strip_prefix(&format!("{k}="))).map(str::to_string);
        let pid = field("MainPID").and_then(|p| p.parse::<u32>().ok()).filter(|p| *p > 0);
        Some(json!({"name": name, "active_state": field("ActiveState"), "main_pid": pid}))
    }

    /// The binary's pid: the unit's main process, else what was launched, while it runs.
    fn pid(&self, unit: Option<&Value>) -> Option<u32> {
        if let Some(pid) = unit.and_then(|u| u["main_pid"].as_u64()) {
            return Some(pid as u32);
        }
        let launched = self.launched();
        let pid = launched["pid"].as_u64().map(|p| p as u32);
        pid.filter(|p| alive(Some(*p)) && runs(*p, launched["binary"].as_str()))
    }

    /// Why the running binary is not current: a fetch replaced its file, it runs another release
    /// than the one installed, or it was launched with another issuer or proxy than this launch's.
    pub fn stale(&self, pid: Option<u32>) -> Option<String> {
        let pid = pid?;
        if exe(pid).is_some_and(|e| e.ends_with(" (deleted)")) {
            return Some("its binary was replaced by a newer fetch".into());
        }
        let launched = self.launched();
        let release = launched["release"].as_str();
        if let (Some(r), Some(installed)) = (release, self.paths.installed()) {
            if launched["pid"].as_u64() == Some(pid as u64) && r != installed {
                return Some(format!("it runs {r} and {installed} is installed"));
            }
        }
        let has = environ(pid)?;
        PINNED.iter().find(|k| lookup(&has, k) != lookup(&self.launch.env, k)).map(|k| format!("it was launched with another {k}"))
    }

    pub async fn alive(&self) -> bool {
        (self.probe)(PROBE_TIMEOUT).await.0 != Probe::Absent
    }

    pub async fn snapshot(&self) -> Value {
        let binary = self.paths.binary();
        let bus = self.bus();
        let unit = self.unit(bus.as_deref()).await;
        let pid = self.pid(unit.as_ref());
        let launched = self.launched();
        let mine = pid.is_some() && launched["pid"].as_u64() == pid.map(u64::from);
        let started = launched["started_at"].as_f64().filter(|_| mine);
        let meta = std::fs::metadata(&binary).ok().filter(|m| m.is_file());
        let built_at = meta.and_then(|m| m.modified().ok()).and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs());
        let running = self.alive().await;
        let mut inner = self.inner.lock().await;
        let exit_code = inner.child.as_mut().and_then(|c| c.try_wait().ok().flatten()).map(|s| s.code());
        json!({
            "role": self.paths.role.as_str(),
            "binary": binary,
            "built": built_at.is_some(),
            "built_at": built_at,
            "running": running,
            "pid": pid,
            "exit_code": exit_code.flatten(),
            "uptime_s": started.map(|s| (crate::db::now() - s).round() as i64),
            "log": self.paths.daemon_log(),
            "error": inner.error,
            "unit": unit,
            "adopted": inner.adopted,
            "release": if mine { launched["release"].clone() } else { Value::Null },
            "replace_pending": inner.pending,
        })
    }

    /// Attach to the binary, launch one, or replace a stale one. Called on a loop.
    pub async fn reconcile(&self) -> Value {
        let Ok(mut inner) = self.inner.try_lock() else { return json!({"action": "busy"}) };
        let (state, report) = (self.probe)(PROBE_TIMEOUT).await;
        let now = Instant::now();
        let young = inner.started.is_some_and(|s| now - s < SETTLE);
        if state != Probe::Absent {
            if !young {
                inner.failures = 0;
            }
            let bus = self.bus();
            let pid = self.pid(self.unit(bus.as_deref()).await.as_ref());
            if inner.adopted.is_none() {
                inner.adopted = Some(true);
                tracing::info!("tether: attached to the running {} (pid {pid:?})", self.paths.role.as_str());
            }
            let Some(reason) = self.stale(pid) else {
                inner.pending = None;
                return json!({"action": "none"});
            };
            if state != Probe::Answering || live_sessions(&report) > 0 {
                if inner.pending.as_deref() != Some(&reason) {
                    tracing::info!("tether: the binary is due for replacement ({reason}); waiting for its sessions to end");
                }
                inner.pending = Some(reason.clone());
                return json!({"action": "deferred", "reason": reason});
            }
            tracing::info!("tether: replacing the binary, idle, because {reason}");
            self.stop(&mut inner, Duration::from_secs(15)).await;
            let mut out = self.start(&mut inner).await;
            if out["action"] == "started" {
                inner.started = Some(Instant::now());
                out["action"] = json!("replaced");
            }
            out["reason"] = json!(reason);
            return out;
        }
        if young {
            inner.started = None;
            inner.error = Some(format!("the binary exited within {}s of starting; see {}", SETTLE.as_secs(), self.paths.daemon_log().display()));
            Self::back_off(&mut inner);
        }
        if inner.next_try.is_some_and(|t| now < t) {
            return json!({"action": "backoff", "error": inner.error});
        }
        let out = self.start(&mut inner).await;
        if out["action"] == "started" {
            inner.started = Some(Instant::now());
        } else {
            Self::back_off(&mut inner);
        }
        out
    }

    fn back_off(inner: &mut Inner) {
        inner.failures += 1;
        let wait = TICK.saturating_mul(2u32.saturating_pow(inner.failures)).min(MAX_BACKOFF);
        inner.next_try = Some(Instant::now() + wait);
        tracing::warn!("tether: the binary is not running ({:?}); next try in {}s", inner.error, wait.as_secs());
    }

    fn private_dir(&self) -> std::io::Result<()> {
        std::fs::create_dir_all(&self.paths.state_dir)?;
        std::fs::set_permissions(&self.paths.state_dir, std::fs::Permissions::from_mode(0o700))
    }

    async fn start(&self, inner: &mut Inner) -> Value {
        let binary = self.paths.binary();
        if !binary.is_file() {
            let why = format!(
                "{} is not present on this host; the plugin fetches the release it pins into {} on start, so this usually means that fetch failed — see {}",
                binary.display(),
                self.paths.bin_dir.display(),
                self.paths.fetch_log().display()
            );
            inner.error = Some(why.clone());
            return json!({"action": "not-built", "error": why});
        }
        let bus = self.bus();
        let launched = match self.private_dir() {
            Err(e) => Err(e.to_string()),
            Ok(()) => match &bus {
                Some(bus) => self.launch_unit(&binary, bus).await,
                None => self.launch_detached(&binary, inner),
            },
        };
        if let Err(e) = launched {
            inner.error = Some(e.clone());
            return json!({"action": "failed", "error": e});
        }
        let deadline = Instant::now() + START_TIMEOUT;
        while (self.probe)(Duration::from_secs(1)).await.0 == Probe::Absent {
            let exited = inner.child.as_mut().is_some_and(|c| c.try_wait().ok().flatten().is_some());
            if Instant::now() > deadline || exited {
                let why = format!("the binary did not answer within {}s; see {}", START_TIMEOUT.as_secs(), self.paths.daemon_log().display());
                inner.error = Some(why.clone());
                return json!({"action": "failed", "error": why});
            }
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
        let pid = self.pid(self.unit(bus.as_deref()).await.as_ref()).or_else(|| inner.child.as_ref().and_then(|c| c.id()));
        let record = json!({
            "pid": pid,
            "release": self.paths.installed(),
            "binary": binary,
            "started_at": crate::db::now(),
            "unit": bus.as_ref().map(|_| format!("{}.service", self.paths.unit())),
        });
        let tmp = self.paths.launch_file().with_extension("tmp");
        if let Err(e) = std::fs::write(&tmp, record.to_string()).and_then(|_| std::fs::rename(&tmp, self.paths.launch_file())) {
            tracing::warn!("tether: could not record the launch: {e}");
        }
        inner.error = None;
        inner.adopted = Some(false);
        inner.pending = None;
        tracing::info!("tether: started {} (pid {pid:?}) {}", binary.display(), if bus.is_some() { "in a user unit" } else { "in its own session" });
        json!({"action": "started", "pid": pid})
    }

    async fn launch_unit(&self, binary: &Path, bus: &[(String, String)]) -> Result<(), String> {
        let unit = self.paths.unit();
        // Reached only when nothing answers, so a unit that still holds the name is a corpse,
        // and systemd-run refuses the name until it is cleared.
        for verb in ["stop", "reset-failed"] {
            systemctl(bus, &[verb, &unit], Duration::from_secs(30)).await;
        }
        let env_file = self.paths.env_file();
        let write = || -> std::io::Result<()> {
            use std::io::Write;
            let mut f = std::fs::OpenOptions::new().create(true).write(true).truncate(true).mode(0o600).open(&env_file)?;
            f.write_all(self.launch.environment_file().as_bytes())?;
            std::fs::set_permissions(&env_file, std::fs::Permissions::from_mode(0o600))
        };
        write().map_err(|e| format!("could not write {}: {e}", env_file.display()))?;
        let log = self.paths.daemon_log();
        let mut cmd = tokio::process::Command::new("systemd-run");
        cmd.args(["--user", "--quiet", "--collect", &format!("--unit={unit}"), "--property=Restart=no", "--property=TimeoutStopSec=15"])
            .arg(format!("--property=EnvironmentFile={}", env_file.display()))
            .arg(format!("--property=WorkingDirectory={}", self.paths.state_dir.display()))
            .arg(format!("--property=StandardOutput=append:{}", log.display()))
            .arg(format!("--property=StandardError=append:{}", log.display()));
        for (k, _) in &self.launch.secrets {
            cmd.arg(format!("--setenv={k}"));
        }
        cmd.arg("--").arg(binary).envs(bus.iter().cloned()).envs(self.launch.secrets.iter().cloned()).kill_on_drop(true);
        let out = tokio::time::timeout(Duration::from_secs(60), cmd.output())
            .await
            .map_err(|_| "systemd-run did not return within 60s".to_string())?
            .map_err(|e| format!("systemd-run: {e}"))?;
        if !out.status.success() {
            let text = String::from_utf8_lossy(if out.stderr.is_empty() { &out.stdout } else { &out.stderr }).trim().chars().take(400).collect::<String>();
            return Err(format!("systemd-run failed ({}): {text}", out.status.code().unwrap_or(-1)));
        }
        Ok(())
    }

    fn launch_detached(&self, binary: &Path, inner: &mut Inner) -> Result<(), String> {
        let log = std::fs::OpenOptions::new().create(true).append(true).open(self.paths.daemon_log()).map_err(|e| e.to_string())?;
        let mut cmd = tokio::process::Command::new(binary);
        cmd.env_clear()
            .envs(self.launch.env.iter().cloned())
            .envs(self.launch.secrets.iter().cloned())
            .current_dir(&self.paths.state_dir)
            .stdin(std::process::Stdio::null())
            .stdout(log.try_clone().map_err(|e| e.to_string())?)
            .stderr(log)
            .kill_on_drop(false);
        // SAFETY: setsid is async-signal-safe. The binary leads its own session so a signal to
        // the plugin's group does not reach it.
        unsafe {
            cmd.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        inner.child = Some(cmd.spawn().map_err(|e| e.to_string())?);
        Ok(())
    }

    async fn stop(&self, inner: &mut Inner, timeout: Duration) {
        let bus = self.bus();
        let pid = self.pid(self.unit(bus.as_deref()).await.as_ref());
        if let Some(bus) = &bus {
            systemctl(bus, &["stop", &self.paths.unit()], timeout + Duration::from_secs(15)).await;
        }
        // A binary outside the unit, launched with no user manager, is not stopped with it.
        if let Some(pid) = pid.filter(|p| alive(Some(*p))) {
            signal(pid, libc::SIGTERM);
            let deadline = Instant::now() + timeout;
            while alive(Some(pid)) && Instant::now() < deadline {
                if let Some(c) = inner.child.as_mut() {
                    let _ = c.try_wait();
                }
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
            if alive(Some(pid)) {
                signal(pid, libc::SIGKILL);
            }
        }
        if let Some(mut c) = inner.child.take() {
            let _ = tokio::time::timeout(Duration::from_secs(5), c.wait()).await;
        }
    }

    /// Reconcile every tick, for good.
    pub async fn supervise_forever(self: Arc<Self>) {
        loop {
            self.reconcile().await;
            tokio::time::sleep(TICK).await;
        }
    }
}

/// The operator daemon's probe: a `status` call on its control socket.
pub fn operator_probe(socket: std::path::PathBuf) -> ProbeFn {
    Arc::new(move |timeout| {
        let socket = socket.clone();
        Box::pin(async move {
            use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
            let Ok(Ok(mut stream)) = tokio::time::timeout(timeout, tokio::net::UnixStream::connect(&socket)).await else {
                return (Probe::Absent, json!({}));
            };
            let ask = async {
                stream.write_all(b"{\"verb\": \"status\", \"args\": {}}\n").await?;
                let mut line = String::new();
                BufReader::new(stream).read_line(&mut line).await?;
                Ok::<_, std::io::Error>(line)
            };
            match tokio::time::timeout(timeout, ask).await {
                Ok(Ok(line)) => match serde_json::from_str::<Value>(&line) {
                    Ok(v) => (Probe::Answering, v),
                    Err(_) => (Probe::Busy, json!({})),
                },
                _ => (Probe::Busy, json!({})),
            }
        })
    })
}

/// The relay's probe: `/health` says whether it runs, and `/status`, which needs the issue
/// bearer, what it holds. Without the bearer, or when `/status` fails, the relay is busy, so a
/// replacement waits rather than risk the rooms' backlog. Every held slot, poll and room counts
/// as a live session.
pub fn relay_probe(base: String, bearer: Option<String>) -> ProbeFn {
    let client = reqwest::Client::builder().no_proxy().build().unwrap_or_default();
    Arc::new(move |timeout| {
        let (client, base, bearer) = (client.clone(), base.clone(), bearer.clone());
        Box::pin(async move {
            let health = client.get(format!("{base}/health")).timeout(timeout).send().await;
            if health.is_err() {
                return (Probe::Absent, json!({}));
            }
            let Some(bearer) = bearer.filter(|b| !b.is_empty()) else { return (Probe::Busy, json!({})) };
            let status = client.get(format!("{base}/status")).bearer_auth(bearer).timeout(timeout).send().await;
            let Ok(resp) = status else { return (Probe::Busy, json!({})) };
            let Ok(body) = resp.json::<Value>().await else { return (Probe::Busy, json!({})) };
            let held: u64 = ["sessions", "polled", "rooms"].iter().map(|k| body.get(*k).and_then(Value::as_u64).unwrap_or(0)).sum();
            (Probe::Answering, json!({"sessions": vec![json!({"phase": "open"}); held as usize]}))
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_env_file_survives_quotes_dollars_and_newlines_and_holds_no_secret() {
        let launch = Launch {
            env: vec![("A".into(), "plain".into()), ("B".into(), "say \"hi\" $HOME\nnext\\line".into()), ("bad-name".into(), "x".into())],
            secrets: vec![("AWM_TETHER_ISSUE_TOKEN".into(), "s3cret".into())],
            ..Launch::default()
        };
        let text = launch.environment_file();
        assert_eq!(text, "A=\"plain\"\nB=\"say \\\"hi\\\" $HOME\nnext\\\\line\"\n");
        assert!(!text.contains("s3cret"));
    }

}
