//! Each plugin's `install` hook (plugin-contract § Runtime): it runs before the plugin's first
//! spawn, and again only when the plugin's install stamp moves.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::gateway::Gateway;
use crate::loader::{stamp, Plugin};

pub const STAMP_FILE: &str = "install.stamp";
const HOOK_TIMEOUT: Duration = Duration::from_secs(30 * 60);
const TAIL_LINES: usize = 20;

#[derive(Default)]
pub struct Hooks {
    /// One hook at a time across plugins: a hook may still write a shared env.
    running: tokio::sync::Mutex<()>,
    /// `{plugin: (stamp, reason)}`: the hooks that failed, on the stamp they failed on.
    failed: Mutex<BTreeMap<String, (String, String)>>,
    /// The external plugins whose hooks are queued or running.
    installing: Mutex<BTreeSet<String>>,
}

impl Hooks {
    pub fn failed(&self) -> BTreeMap<String, (String, String)> {
        self.failed.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }

    pub fn installing(&self, plugin: &str) -> bool {
        self.installing.lock().unwrap_or_else(|p| p.into_inner()).contains(plugin)
    }


    /// Drop the kept failures of `plugins`, so their hooks run again at the next start.
    pub fn forget(&self, plugins: &[String]) {
        self.failed.lock().unwrap_or_else(|p| p.into_inner()).retain(|n, _| !plugins.contains(n));
    }
}

/// `plugin` reads as installing while this lives.
pub struct Installing {
    gw: Arc<Gateway>,
    plugin: String,
}

impl Installing {
    /// None when `plugin` is installing already.
    pub fn begin(gw: &Arc<Gateway>, plugin: &str) -> Option<Self> {
        let fresh = gw.hooks.installing.lock().unwrap_or_else(|p| p.into_inner()).insert(plugin.to_string());
        fresh.then(|| Self { gw: gw.clone(), plugin: plugin.to_string() })
    }
}

impl Drop for Installing {
    fn drop(&mut self) {
        self.gw.hooks.installing.lock().unwrap_or_else(|p| p.into_inner()).remove(&self.plugin);
    }
}

/// A plugin's own state folder under the gateway's state.
pub fn plugin_state(state: &Path, plugin: &str) -> PathBuf {
    state.join("plugins").join(plugin)
}

pub fn stamp_path(state: &Path, plugin: &str) -> PathBuf {
    plugin_state(state, plugin).join(STAMP_FILE)
}

async fn stamp_of(dir: &Path) -> String {
    let dir = dir.to_path_buf();
    tokio::task::spawn_blocking(move || stamp(&dir)).await.unwrap_or_default()
}

/// Run `plugin`'s install hooks unless its stamp matches the last success. A failure is kept,
/// so the loader refuses the plugin until its folder changes; the error names the hook's last
/// output lines. `Ok(true)` when the hooks ran.
pub async fn ensure(gw: &Gateway, plugin: &str) -> Result<bool, String> {
    let loaded = gw.read_load().plugins.iter().find(|p| p.name == plugin).cloned();
    let Some(p) = loaded else { return Err(format!("plugin {plugin:?} is no longer loaded")) };
    if p.install.is_empty() {
        return Ok(false);
    }
    let _one_at_a_time = gw.hooks.running.lock().await;
    let path = stamp_path(&gw.state, plugin);
    let before = stamp_of(&p.dir).await;
    if std::fs::read_to_string(&path).is_ok_and(|s| s.trim() == before) {
        return Ok(false);
    }
    if let Some((_, reason)) = gw.hooks.failed().get(plugin).filter(|(at, _)| *at == before) {
        return Err(reason.clone());
    }
    for hook in &p.install {
        if let Err(why) = run(gw, &p, hook).await {
            let reason = format!("install hook {} failed: {why}", hook.display());
            // The hook may have written into the folder: the stamp it leaves is the one refused.
            let after = stamp_of(&p.dir).await;
            gw.hooks.failed.lock().unwrap_or_else(|p| p.into_inner()).insert(plugin.to_string(), (after, reason.clone()));
            return Err(reason);
        }
    }
    gw.hooks.failed.lock().unwrap_or_else(|p| p.into_inner()).remove(plugin);
    // Taken after the hooks, which may write into the folder, such as an editable install.
    let after = stamp_of(&p.dir).await;
    std::fs::create_dir_all(plugin_state(&gw.state, plugin)).map_err(|e| e.to_string())?;
    crate::survival::write_atomic(&path, &after).map_err(|e| format!("{}: {e}", path.display()))?;
    tracing::info!("plugin {plugin}: installed");
    Ok(true)
}

async fn run(gw: &Gateway, p: &Plugin, hook: &Path) -> Result<(), String> {
    let logs = gw.state.join("logs");
    std::fs::create_dir_all(&logs).map_err(|e| e.to_string())?;
    let log_path = logs.join(format!("{}.install.log", p.name));
    let log = std::fs::File::create(&log_path).map_err(|e| e.to_string())?;
    let cfg = gw.cfg();
    let mut cmd = tokio::process::Command::new("bash");
    cmd.env_clear();
    for name in crate::supervisor::INHERITED {
        if let Some(v) = std::env::var_os(name) {
            cmd.env(name, v);
        }
    }
    cmd.arg(hook)
        .current_dir(&p.dir)
        .envs(&cfg.env)
        .env("AWM_DIR", &gw.state)
        .env("AWM_PLUGIN", &p.name)
        .env("AWM_NODE_NAME", &gw.node)
        .stdin(std::process::Stdio::null())
        .stdout(log.try_clone().map_err(|e| e.to_string())?)
        .stderr(log)
        .process_group(0)
        .kill_on_drop(true);
    if let Some(ws) = &cfg.workspace {
        cmd.env("AWM_WORKSPACE", ws);
    }
    tracing::info!("plugin {}: running install hook {}", p.name, hook.display());
    let mut child = cmd.spawn().map_err(|e| e.to_string())?;
    let pgid = child.id().map(|id| id as i32);
    let status = match tokio::time::timeout(HOOK_TIMEOUT, child.wait()).await {
        Ok(status) => status.map_err(|e| e.to_string())?,
        Err(_) => {
            if let Some(pgid) = pgid {
                // SAFETY: kill(2) on the process group this hook leads.
                unsafe { libc::kill(-pgid, libc::SIGKILL) };
            }
            return Err(format!("no exit after {HOOK_TIMEOUT:?}{}", tail(&log_path)));
        }
    };
    if status.success() {
        return Ok(());
    }
    Err(format!("{status}{}", tail(&log_path)))
}

/// The last output lines of a hook, for its refusal.
fn tail(log: &Path) -> String {
    let text = std::fs::read_to_string(log).unwrap_or_default();
    let lines: Vec<&str> = text.lines().filter(|l| !l.trim().is_empty()).collect();
    let last = &lines[lines.len().saturating_sub(TAIL_LINES)..];
    if last.is_empty() {
        return String::new();
    }
    format!("; last output:\n{}", last.join("\n"))
}
