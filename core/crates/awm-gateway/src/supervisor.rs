//! Starts each enabled service as `bash <run>` in its folder, respawns it, and stops it.

use std::path::{Path, PathBuf};
use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::json;
use tokio::task::JoinHandle;

use crate::gateway::Gateway;
use crate::loader::ServiceSpec;

const CRASH_WINDOW: Duration = Duration::from_secs(60);
const CRASH_LIMIT: usize = 5;
const GRACE: Duration = Duration::from_secs(8);

#[derive(Default)]
pub struct Supervisor {
    /// Live process group ids, by service name.
    groups: Mutex<HashMap<String, i32>>,
    /// Each running service's loop, with the flag that retires it on a plugin change.
    tasks: Mutex<HashMap<String, (Arc<AtomicBool>, JoinHandle<()>)>>,
    /// Services that crash-looped and stay down.
    failed: Mutex<Vec<String>>,
}

impl Supervisor {
    fn groups(&self) -> std::sync::MutexGuard<'_, HashMap<String, i32>> {
        self.groups.lock().unwrap_or_else(|p| p.into_inner())
    }

    pub fn failed(&self) -> Vec<String> {
        self.failed.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }

    fn tasks(&self) -> std::sync::MutexGuard<'_, HashMap<String, (Arc<AtomicBool>, JoinHandle<()>)>> {
        self.tasks.lock().unwrap_or_else(|p| p.into_inner())
    }
}

pub fn start_all(gw: &Arc<Gateway>) {
    let specs: Vec<ServiceSpec> = gw.read_load().plugins.iter().flat_map(|p| p.services.iter().cloned()).collect();
    for spec in specs {
        start(gw, spec);
    }
}

/// Start one service's loop, unless it is disabled, external or already running.
pub fn start(gw: &Arc<Gateway>, spec: ServiceSpec) {
    if !spec.enabled {
        return;
    }
    if gw.cfg.external(&spec.name).is_some() {
        tracing::info!("service {}: external, waiting for it to register", spec.name);
        return;
    }
    let mut tasks = gw.supervisor.tasks();
    if tasks.get(&spec.name).is_some_and(|(_, h)| !h.is_finished()) {
        return;
    }
    gw.supervisor.failed.lock().unwrap_or_else(|p| p.into_inner()).retain(|n| n != &spec.name);
    let retired = Arc::new(AtomicBool::new(false));
    let name = spec.name.clone();
    let (gw2, flag) = (gw.clone(), retired.clone());
    tasks.insert(name, (retired, tokio::spawn(async move { run_service(gw2, spec, flag).await })));
}

/// Stand some services down for a plugin change: `shutdown`, a grace period, then signals.
pub async fn stop(gw: &Arc<Gateway>, names: &[String]) {
    let mut handles = Vec::new();
    {
        let mut tasks = gw.supervisor.tasks();
        for name in names {
            if let Some((flag, handle)) = tasks.remove(name) {
                flag.store(true, Ordering::SeqCst);
                handles.push(handle);
            }
            gw.hub.send(name, &json!({"kind": "shutdown"}));
        }
    }
    let live = |gw: &Gateway| names.iter().any(|n| gw.supervisor.groups().contains_key(n));
    let deadline = Instant::now() + GRACE;
    while live(gw) && Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    for signal in [libc::SIGTERM, libc::SIGKILL] {
        for name in names {
            if let Some(pgid) = gw.supervisor.groups().get(name) {
                kill_group(*pgid, signal);
            }
        }
        let deadline = Instant::now() + Duration::from_secs(2);
        while live(gw) && Instant::now() < deadline {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }
    for h in handles {
        let _ = tokio::time::timeout(Duration::from_secs(2), h).await;
    }
}

async fn run_service(gw: Arc<Gateway>, spec: ServiceSpec, retired: Arc<AtomicBool>) {
    let mut exits: VecDeque<Instant> = VecDeque::new();
    let mut backoff = Duration::from_secs(1);
    let window = Duration::from_secs_f64(gw.cfg.reconnect_window_s);
    let done = || gw.stopping() || retired.load(Ordering::SeqCst);
    while !done() {
        let mut child = match spawn(&gw, &spec) {
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
        }
        tracing::info!("service {} started, pid {:?}", spec.name, pid);
        let spawned = Instant::now();
        let mut signalled = false;
        let status = loop {
            tokio::select! {
                status = child.wait() => break status,
                _ = tokio::time::sleep(Duration::from_secs(1)) => {
                    let silent = gw.hub.down_for(&spec.name).is_some_and(|d| d > window);
                    if !done() && !signalled && silent && spawned.elapsed() > window {
                        tracing::warn!("service {} off its control connection past {:?}; restarting", spec.name, window);
                        if let Some(pid) = pid { kill_group(pid, libc::SIGTERM); }
                        signalled = true;
                    }
                }
            }
        };
        gw.supervisor.groups().remove(&spec.name);
        tracing::info!("service {} exited: {:?}", spec.name, status);
        if done() {
            break;
        }
        let now = Instant::now();
        exits.push_back(now);
        while exits.front().is_some_and(|t| now.duration_since(*t) > CRASH_WINDOW) {
            exits.pop_front();
        }
        if exits.len() >= CRASH_LIMIT {
            tracing::error!("service {} exited {CRASH_LIMIT} times in {CRASH_WINDOW:?}; left down", spec.name);
            gw.supervisor.failed.lock().unwrap_or_else(|p| p.into_inner()).push(spec.name.clone());
            break;
        }
        if spawned.elapsed() > CRASH_WINDOW {
            backoff = Duration::from_secs(1);
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(30));
    }
}

fn spawn(gw: &Gateway, spec: &ServiceSpec) -> std::io::Result<tokio::process::Child> {
    let logs = gw.state.join("logs");
    std::fs::create_dir_all(&logs)?;
    let log = std::fs::OpenOptions::new().create(true).append(true).open(logs.join(format!("{}.log", spec.name)))?;
    let token = new_service_token(&gw.state, &spec.name)?;
    let mut cmd = tokio::process::Command::new("bash");
    cmd.arg(&spec.run)
        .current_dir(&spec.cwd)
        .envs(&gw.cfg.env)
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
    if let Some(ws) = &gw.cfg.workspace {
        cmd.env("AWM_WORKSPACE", ws);
    }
    cmd.spawn()
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

/// Stand every service down: the in-band `shutdown` frame, a grace period, then SIGTERM and SIGKILL.
pub async fn stop_all(gw: &Arc<Gateway>) {
    gw.stopping.store(true, Ordering::SeqCst);
    gw.hub.broadcast(&json!({"kind": "shutdown"}));
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
