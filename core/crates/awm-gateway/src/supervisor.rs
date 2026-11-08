//! Starts each enabled service as `bash <run>` in its folder, respawns it, stops it, and adopts
//! the ones that outlived the last gateway (see `survival.rs`).

use std::path::{Path, PathBuf};
use std::collections::{BTreeMap, HashMap};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::json;
use tokio::sync::Notify;
use tokio::task::JoinHandle;

use crate::gateway::Gateway;
use crate::loader::ServiceSpec;
use crate::survival::{self, Journal};

const GRACE: Duration = Duration::from_secs(8);
/// The gateway's own variables a service inherits. Everything else, secrets the gateway
/// was started with included, stays out of the service (R116 item 1).
pub(crate) const INHERITED: [&str; 11] =
    ["PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TZ", "TMPDIR", "XDG_RUNTIME_DIR", "TERM"];

/// A service process a past gateway started and this one found alive.
#[derive(Debug, Clone, Copy)]
pub struct Adopted {
    pub pid: i32,
    pub pgid: i32,
    pub start: u64,
}

/// A running service's loop, with the flag that retires it on a plugin change.
type Task = (Arc<AtomicBool>, JoinHandle<()>);

pub struct Supervisor {
    /// Live process group ids, by service name.
    groups: Mutex<HashMap<String, i32>>,
    tasks: Mutex<HashMap<String, Task>>,
    /// The `[env]` each running service was spawned with.
    env: Mutex<HashMap<String, BTreeMap<String, String>>>,
    /// Services that crash-looped and stay down.
    failed: Mutex<Vec<String>>,
    /// Pid, start time and breaker of each service, kept on disk across gateway restarts.
    pub journal: Journal,
    /// Hold mode: adopt the live services and spawn none.
    hold: AtomicBool,
    /// Signalled when a drain request has stood the services down and the gateway should exit.
    pub drained: Notify,
}

impl Supervisor {
    pub fn new(state: &Path) -> Self {
        Self {
            groups: Default::default(),
            env: Default::default(),
            tasks: Default::default(),
            failed: Default::default(),
            journal: Journal::load(state),
            hold: AtomicBool::new(false),
            drained: Notify::new(),
        }
    }

    fn groups(&self) -> std::sync::MutexGuard<'_, HashMap<String, i32>> {
        self.groups.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// The `[env]` of every service that has a live process.
    pub fn env_of_running(&self) -> HashMap<String, BTreeMap<String, String>> {
        let live = self.groups();
        self.env.lock().unwrap_or_else(|p| p.into_inner()).iter().filter(|(n, _)| live.contains_key(*n)).map(|(n, e)| (n.clone(), e.clone())).collect()
    }

    pub fn failed(&self) -> Vec<String> {
        self.failed.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }

    fn mark_failed(&self, name: &str) {
        let mut failed = self.failed.lock().unwrap_or_else(|p| p.into_inner());
        if !failed.iter().any(|n| n == name) {
            failed.push(name.to_string());
        }
    }

    fn tasks(&self) -> std::sync::MutexGuard<'_, HashMap<String, Task>> {
        self.tasks.lock().unwrap_or_else(|p| p.into_inner())
    }

    pub fn held(&self) -> bool {
        self.hold.load(Ordering::SeqCst)
    }

    pub fn set_hold(&self, on: bool) {
        self.hold.store(on, Ordering::SeqCst);
    }
}

/// Bring the services up. Each journaled process that is still alive is adopted first, and only
/// a service with no such process is spawned (and none in hold mode).
pub async fn start_all(gw: &Arc<Gateway>, hold: bool) {
    gw.supervisor.set_hold(hold);
    let specs: Vec<ServiceSpec> = gw.read_load().plugins.iter().flat_map(|p| p.services.iter().cloned()).collect();
    let mut adopted = rehydrate(gw, &specs).await;
    for spec in specs {
        let found = adopted.remove(&spec.name);
        start_inner(gw, spec, found, false);
    }
}

/// Match the journal against the processes alive now. A live entry becomes an [`Adopted`] and a
/// hub record; a dead entry loses its pid and keeps its breaker; a live process of a service this
/// gateway does not run is stood down.
async fn rehydrate(gw: &Arc<Gateway>, specs: &[ServiceSpec]) -> HashMap<String, Adopted> {
    let mut out = HashMap::new();
    for (name, e) in gw.supervisor.journal.snapshot() {
        let (Some(pid), Some(start)) = (e.pid, e.start_time) else { continue };
        let pgid = e.pgid.unwrap_or(pid);
        if !survival::alive_as(pid, start) {
            tracing::info!("service {name}: journaled pid {pid} is gone");
            if survival::group_alive(pgid) && survival::group_is_ours(pgid, start) {
                tracing::warn!("service {name}: its leader is gone but its group {pgid} is not; standing the group down");
                clear_group(pgid).await;
            }
            gw.supervisor.journal.update_existing(&name, |e| (e.pid, e.pgid, e.start_time, e.service_id) = (None, None, None, None));
            continue;
        }
        let wanted = specs.iter().find(|s| s.name == name).filter(|s| s.enabled && gw.cfg().external(&s.name).is_none());
        match wanted {
            Some(spec) => {
                tracing::info!("service {name}: adopting pid {pid}");
                gw.hub.rehydrate(&name, &spec.plugin, Some(pid as i64));
                if let Some(env) = e.env.clone() {
                    gw.supervisor.env.lock().unwrap_or_else(|p| p.into_inner()).insert(name.clone(), env);
                }
                out.insert(name, Adopted { pid, pgid, start });
            }
            None => {
                tracing::warn!("service {name}: journaled pid {pid} is alive but the service is not wanted; standing it down");
                gw.supervisor.journal.remove(&name);
                clear_group(pgid).await;
            }
        }
    }
    out
}

/// Start one service's loop, unless it is disabled, external, tripped, held or already running.
pub fn start(gw: &Arc<Gateway>, spec: ServiceSpec) {
    start_inner(gw, spec, None, false);
}

/// Start a service on an operator's word: clears a tripped breaker, and runs in hold mode too.
/// The override is logged.
pub fn force_start(gw: &Arc<Gateway>, spec: ServiceSpec) {
    let name = spec.name.clone();
    if gw.supervisor.journal.tripped(&name) {
        tracing::warn!("service {name}: forced start; breaker cleared");
    } else if gw.supervisor.held() {
        tracing::warn!("service {name}: forced start in hold mode");
    }
    gw.supervisor.journal.update_existing(&name, |e| {
        e.tripped = false;
        e.exits.clear();
    });
    gw.supervisor.failed.lock().unwrap_or_else(|p| p.into_inner()).retain(|n| n != &name);
    start_inner(gw, spec, None, true);
}

/// Leave hold mode: spawn every enabled service that has no running loop.
pub fn leave_hold(gw: &Arc<Gateway>) -> bool {
    let was = gw.supervisor.hold.swap(false, Ordering::SeqCst);
    survival::clear_start_log(&gw.state);
    tracing::warn!("hold mode left by request (was {was}); starting the services that are down");
    let specs: Vec<ServiceSpec> = gw.read_load().plugins.iter().flat_map(|p| p.services.iter().cloned()).collect();
    for spec in specs {
        start(gw, spec);
    }
    was
}

fn start_inner(gw: &Arc<Gateway>, spec: ServiceSpec, adopted: Option<Adopted>, forced: bool) {
    if !spec.enabled {
        return;
    }
    if gw.cfg().external(&spec.name).is_some() {
        tracing::info!("service {}: external, waiting for it to register", spec.name);
        return;
    }
    let mut tasks = gw.supervisor.tasks();
    if tasks.get(&spec.name).is_some_and(|(_, h)| !h.is_finished()) {
        return;
    }
    if adopted.is_none() && !forced {
        if gw.supervisor.journal.tripped(&spec.name) {
            tracing::error!("service {}: breaker tripped; left down until a forced start", spec.name);
            gw.supervisor.mark_failed(&spec.name);
            return;
        }
        if gw.supervisor.held() {
            tracing::error!("service {}: hold mode; not spawned", spec.name);
            return;
        }
    }
    gw.supervisor.failed.lock().unwrap_or_else(|p| p.into_inner()).retain(|n| n != &spec.name);
    let retired = Arc::new(AtomicBool::new(false));
    let name = spec.name.clone();
    let (gw2, flag) = (gw.clone(), retired.clone());
    tasks.insert(name, (retired, tokio::spawn(async move { run_service(gw2, spec, flag, adopted).await })));
}

/// Stand some services down for a plugin change: `shutdown`, a grace period, then signals.
/// Their journaled processes go first, so nothing re-adopts them. The breaker stays, so only a
/// forced start passes a tripped one.
pub async fn stop(gw: &Arc<Gateway>, names: &[String]) {
    for name in names {
        gw.supervisor.journal.update_existing(name, |e| (e.pid, e.pgid, e.start_time, e.service_id) = (None, None, None, None));
    }
    gw.supervisor.failed.lock().unwrap_or_else(|p| p.into_inner()).retain(|n| !names.contains(n));
    let mut handles = Vec::new();
    {
        let mut tasks = gw.supervisor.tasks();
        for name in names {
            if let Some((flag, handle)) = tasks.remove(name) {
                flag.store(true, Ordering::SeqCst);
                handles.push(handle);
            }
        }
    }
    // Only a service on its control connection hears `shutdown`; any other goes straight to signals.
    let told: Vec<String> = names.iter().filter(|n| gw.hub.send(n, &json!({"kind": "shutdown"}))).cloned().collect();
    let live = |of: &[String]| of.iter().any(|n| gw.supervisor.groups().contains_key(n));
    let deadline = Instant::now() + GRACE;
    while live(&told) && Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    for signal in [libc::SIGTERM, libc::SIGKILL] {
        for name in names {
            if let Some(pgid) = gw.supervisor.groups().get(name) {
                kill_group(*pgid, signal);
            }
        }
        let deadline = Instant::now() + Duration::from_secs(2);
        while live(names) && Instant::now() < deadline {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }
    for h in handles {
        let _ = tokio::time::timeout(Duration::from_secs(2), h).await;
    }
}

/// What a service's loop watches: a child it spawned, or a process it adopted.
async fn run_service(gw: Arc<Gateway>, spec: ServiceSpec, retired: Arc<AtomicBool>, mut adopted: Option<Adopted>) {
    let mut backoff = Duration::from_secs(1);
    let done = || gw.stopping() || retired.load(Ordering::SeqCst);
    while !done() {
        let spawned = Instant::now();
        let group = match adopted.take() {
            Some(a) => {
                gw.supervisor.groups().insert(spec.name.clone(), a.pgid);
                watch_adopted(&gw, &spec, a, &done).await;
                a.pgid
            }
            None => {
                if let Err(why) = crate::hooks::ensure(&gw, &spec.plugin).await {
                    tracing::error!("plugin {}: refused: {why}", spec.plugin);
                    // Its own task stands this one down, so the reload never waits on itself.
                    let gw = gw.clone();
                    tokio::spawn(async move {
                        if let Err(e) = gw.reload_needs().await {
                            tracing::warn!("reload after a failed install hook: {}", e.detail);
                        }
                    });
                    break;
                }
                if done() {
                    break;
                }
                let env = gw.cfg().env.clone();
                let mut child = match spawn(&gw, &spec, &env) {
                    Ok(c) => c,
                    Err(e) => {
                        tracing::error!("service {}: spawn failed: {e}", spec.name);
                        tokio::time::sleep(Duration::from_secs(5)).await;
                        continue;
                    }
                };
                let pid = child.id().map(|p| p as i32);
                if let Some(pid) = pid {
                    gw.supervisor.groups().insert(spec.name.clone(), pid);
                    gw.supervisor.env.lock().unwrap_or_else(|p| p.into_inner()).insert(spec.name.clone(), env.clone());
                    let start = survival::start_time(pid);
                    gw.supervisor.journal.upsert(&spec.name, |e| {
                        e.pid = Some(pid);
                        e.pgid = Some(pid);
                        e.start_time = start;
                        e.service_id = None;
                        e.env = Some(env);
                    });
                }
                tracing::info!("service {} started, pid {:?}", spec.name, pid);
                let window = Duration::from_secs_f64(gw.cfg().reconnect_window_s);
                let mut signalled: Option<Instant> = None;
                let status = loop {
                    tokio::select! {
                        status = child.wait() => break status,
                        _ = tokio::time::sleep(Duration::from_secs(1)) => {
                            // A connection lost before this spawn is the last process's: this one
                            // may still be in a long first start and has not connected yet.
                            let silent = gw.hub.down_for(&spec.name).is_some_and(|d| d > window && d < spawned.elapsed());
                            if !done() && silent {
                                match (signalled, pid) {
                                    (None, Some(pid)) => {
                                        tracing::warn!("service {} off its control connection past {:?}; restarting", spec.name, window);
                                        kill_group(pid, libc::SIGTERM);
                                        signalled = Some(Instant::now());
                                    }
                                    (Some(at), Some(pid)) if at.elapsed() > GRACE => kill_group(pid, libc::SIGKILL),
                                    _ => {}
                                }
                            }
                        }
                    }
                };
                tracing::info!("service {} exited: {:?}", spec.name, status);
                pid.unwrap_or(0)
            }
        };
        gw.supervisor.groups().remove(&spec.name);
        if group > 0 && survival::group_alive(group) {
            // A respawn never doubles a process: the old group is gone before the next spawn.
            clear_group(group).await;
        }
        gw.supervisor.journal.update_existing(&spec.name, |e| (e.pid, e.pgid, e.start_time, e.service_id) = (None, None, None, None));
        if done() {
            break;
        }
        if gw.supervisor.held() {
            tracing::error!("service {} exited in hold mode; left down", spec.name);
            break;
        }
        if gw.supervisor.journal.record_exit(&spec.name, survival::unix_now()) {
            tracing::error!(
                "service {} exited {} times in {}s; breaker tripped, left down",
                spec.name,
                survival::CRASH_LIMIT,
                survival::CRASH_WINDOW_S
            );
            gw.supervisor.mark_failed(&spec.name);
            break;
        }
        if spawned.elapsed() > Duration::from_secs(survival::CRASH_WINDOW_S as u64) {
            backoff = Duration::from_secs(1);
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(30));
    }
}

/// Wait for an adopted process to end. It cannot be `wait`ed on, so poll its pid and start time.
/// One that stays off its control connection past the adoption window is killed by group.
async fn watch_adopted(gw: &Gateway, spec: &ServiceSpec, a: Adopted, done: &(dyn Fn() -> bool + Sync)) {
    let window = Duration::from_secs_f64(gw.cfg().adopt_window_s);
    let mut signalled: Option<Instant> = None;
    while survival::alive_as(a.pid, a.start) {
        tokio::time::sleep(Duration::from_secs(1)).await;
        let silent = gw.hub.down_for(&spec.name).is_some_and(|d| d > window);
        if done() || !silent {
            continue;
        }
        match signalled {
            None => {
                tracing::warn!("service {}: adopted pid {} silent past {:?}; killing its group", spec.name, a.pid, window);
                kill_group(a.pgid, libc::SIGTERM);
                signalled = Some(Instant::now());
            }
            Some(at) if at.elapsed() > GRACE => kill_group(a.pgid, libc::SIGKILL),
            Some(_) => {}
        }
    }
    tracing::info!("service {}: adopted pid {} exited", spec.name, a.pid);
}

/// Signal a process group down and wait until none of its processes is left.
async fn clear_group(pgid: i32) {
    for (signal, wait) in [(libc::SIGTERM, GRACE), (libc::SIGKILL, Duration::from_secs(5))] {
        kill_group(pgid, signal);
        let deadline = Instant::now() + wait;
        while survival::group_alive(pgid) && Instant::now() < deadline {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        if !survival::group_alive(pgid) {
            return;
        }
    }
    tracing::error!("process group {pgid} survived SIGKILL");
}

fn spawn(gw: &Gateway, spec: &ServiceSpec, env: &BTreeMap<String, String>) -> std::io::Result<tokio::process::Child> {
    let logs = gw.state.join("logs");
    std::fs::create_dir_all(&logs)?;
    let log = std::fs::OpenOptions::new().create(true).append(true).open(logs.join(format!("{}.log", spec.name)))?;
    let token = new_service_token(&gw.state, &spec.name)?;
    let mut cmd = tokio::process::Command::new("bash");
    cmd.env_clear();
    for name in INHERITED {
        if let Some(v) = std::env::var_os(name) {
            cmd.env(name, v);
        }
    }
    cmd.arg(&spec.run)
        .current_dir(&spec.cwd)
        .envs(env)
        .envs(granted_secrets(&gw.cfg().secrets_path(&gw.state), &spec.secrets))
        .env("AWM_HUB_URL", gw.hub_url())
        .env("AWM_SERVICE_NAME", &spec.name)
        .env("AWM_PLUGIN", &spec.plugin)
        .env("AWM_NODE_NAME", &gw.node)
        .env("AWM_DIR", &gw.state)
        .env("AWM_HUB_TOKEN_FILE", token)
        .env_remove("AWM_SERVICE_ID")
        .stdin(std::process::Stdio::null())
        .stdout(log.try_clone()?)
        .stderr(log)
        .process_group(0)
        .kill_on_drop(false);
    if let Some(port) = gw.hub_url().rsplit(':').next() {
        cmd.env("AWM_PORT", port);
    }
    if let Some(ws) = &gw.cfg().workspace {
        cmd.env("AWM_WORKSPACE", ws);
    }
    {
        let port = gw.egress.port_for(gw.weak(), &spec.plugin)?;
        let exempt = crate::egress::exempt_hosts(&gw.cfg().edges_path(&gw.state), &gw.hub_url());
        cmd.envs(crate::egress::proxy_env(port, &exempt));
    }
    cmd.spawn()
}

/// The values of the secrets a plugin declares: from the secrets file, else from the
/// gateway's own environment. A secret the plugin does not declare is never read.
pub fn granted_secrets(file: &Path, wanted: &[String]) -> Vec<(String, String)> {
    if wanted.is_empty() {
        return vec![];
    }
    let text = std::fs::read_to_string(file).unwrap_or_default();
    let from_file: HashMap<&str, &str> = text
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .filter_map(|l| l.strip_prefix("export ").unwrap_or(l).split_once('='))
        .map(|(k, v)| (k.trim(), v.trim().trim_matches('"')))
        .collect();
    wanted
        .iter()
        .filter_map(|name| {
            let value = from_file.get(name.as_str()).map(|v| v.to_string()).or_else(|| std::env::var(name).ok())?;
            Some((name.clone(), value))
        })
        .collect()
}

/// The token file a spawned service registers with. Each spawn gets a fresh token,
/// so no other local user can take over the service's name while it is down.
pub fn service_token_path(state: &Path, service: &str) -> PathBuf {
    state.join("tokens").join(format!("{service}.token"))
}

fn new_service_token(state: &Path, service: &str) -> std::io::Result<PathBuf> {
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
    let dir = state.join("tokens");
    std::fs::DirBuilder::new().recursive(true).mode(0o700).create(&dir)?;
    let path = service_token_path(state, service);
    let _ = std::fs::remove_file(&path);
    let mut f = std::fs::OpenOptions::new().create_new(true).write(true).mode(0o600).open(&path)?;
    std::io::Write::write_all(&mut f, uuid::Uuid::new_v4().simple().to_string().as_bytes())?;
    Ok(path)
}

fn kill_group(pgid: i32, signal: i32) {
    // SAFETY: kill(2) with a negative pid signals a process group we started.
    unsafe {
        libc::kill(-pgid, signal);
    }
}

/// A deliberate stop: the in-band `shutdown` frame, a grace period, then SIGTERM and SIGKILL, and
/// the journal emptied so no later gateway adopts what is gone. An external service has its own
/// unit and only reconnects.
pub async fn stop_all(gw: &Arc<Gateway>) {
    gw.stopping.store(true, Ordering::SeqCst);
    gw.supervisor.journal.clear();
    survival::clear_start_log(&gw.state);
    gw.hub.broadcast(&json!({"kind": "shutdown"}), |name| gw.cfg().external(name).is_none());
    let deadline = Instant::now() + GRACE;
    while !gw.supervisor.groups().is_empty() && Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    for pgid in gw.supervisor.groups().values() {
        kill_group(*pgid, libc::SIGTERM);
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    while !gw.supervisor.groups().is_empty() && Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    for pgid in gw.supervisor.groups().values() {
        kill_group(*pgid, libc::SIGKILL);
    }
    let tasks: Vec<_> = gw.supervisor.tasks().drain().map(|(_, (_, h))| h).collect();
    for t in tasks {
        let _ = tokio::time::timeout(Duration::from_secs(2), t).await;
    }
}

/// A restart, not a stop: the gateway exits and the services keep running for the next gateway to
/// adopt. Their journal stays as it is.
pub fn leave_running(gw: &Arc<Gateway>) {
    gw.stopping.store(true, Ordering::SeqCst);
    tracing::info!("stopping: {} services left running for the next gateway", gw.supervisor.groups().len());
}
