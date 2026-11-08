//! Supervision of the DeepSeek harness web server: one node process on loopback, parented to
//! dsh in its own process group, with its output appended to `dsh.log`.
//!
//! The harness persists its sessions to `$DSH_HOME`, so a dsh restart costs a browser
//! reconnect, not a conversation. `wanted` is what keeps a `stop` stopped: the health tick
//! respawns a dead harness only while it is wanted, and only `start` wants it again.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use awm_plugin_sdk::proc::{in_group, stop_group};
use serde_json::{json, Value};

use crate::paths::Paths;
use crate::settings::{self, Settings, API_KEY_ENV, PLUGIN_KEY, PROVIDER_ID};

const BUILD_STAMP: &str = "dsh-build-stamp";
const STOP_TIMEOUT: Duration = Duration::from_secs(15);

struct State {
    child: Option<tokio::process::Child>,
    pid: Option<u32>,
    started_at: Option<Instant>,
    last_error: Option<String>,
    wanted: bool,
}

pub struct Supervisor {
    pub paths: Paths,
    state: tokio::sync::Mutex<State>,
}

/// Python's rendering of a float: `60.0`, `0.5`.
fn py_float(f: f64) -> String {
    if f.fract() == 0.0 {
        format!("{f:.1}")
    } else {
        format!("{f}")
    }
}

fn exit_code(status: std::process::ExitStatus) -> i32 {
    use std::os::unix::process::ExitStatusExt;
    status.code().unwrap_or_else(|| -status.signal().unwrap_or(0))
}

/// Whether something accepts a connection on loopback `port`.
pub async fn listening(port: u16) -> bool {
    matches!(
        tokio::time::timeout(Duration::from_millis(500), tokio::net::TcpStream::connect(("127.0.0.1", port))).await,
        Ok(Ok(_))
    )
}

async fn git(dir: &Path, args: &[&str]) -> Option<String> {
    let run = tokio::process::Command::new("git").arg("-C").arg(dir).args(args).kill_on_drop(true).output();
    match tokio::time::timeout(Duration::from_secs(10), run).await {
        Ok(Ok(out)) if out.status.success() => Some(String::from_utf8_lossy(&out.stdout).trim().to_string()),
        _ => None,
    }
}

/// The build stamp's path, in the fork worktree's git dir, which never travels with the tree.
pub async fn build_stamp(fork: &Path) -> Option<PathBuf> {
    git(fork, &["rev-parse", "--absolute-git-dir"]).await.map(|d| PathBuf::from(d).join("awm").join(BUILD_STAMP))
}

/// Which harness revision is on disk, and whether the build matches it.
pub async fn source_state(fork: &Path) -> Value {
    let status = git(fork, &["status", "--porcelain"]).await;
    let stamp: Option<HashMap<String, String>> = match build_stamp(fork).await {
        Some(f) => std::fs::read_to_string(f).ok().map(|text| {
            text.split_whitespace().filter_map(|w| w.split_once('=')).map(|(k, v)| (k.to_string(), v.to_string())).collect()
        }),
        None => None,
    };
    let head = git(fork, &["rev-parse", "HEAD"]).await;
    let dirty_flag = if status.as_deref().is_some_and(|s| !s.is_empty()) { "1" } else { "0" };
    let built_current = match (&stamp, &head) {
        (Some(st), Some(h)) => json!(st.get("head") == Some(h) && st.get("dirty").map(String::as_str) == Some(dirty_flag)),
        _ => Value::Null,
    };
    json!({
        "dir": fork,
        "branch": git(fork, &["rev-parse", "--abbrev-ref", "HEAD"]).await,
        "describe": git(fork, &["describe", "--tags", "--always", "--dirty"]).await,
        "head": head,
        "dirty": status.map(|s| !s.is_empty()),
        "built_head": stamp.as_ref().and_then(|s| s.get("head")),
        "built_current": built_current,
    })
}

impl Supervisor {
    pub fn new(paths: Paths) -> Self {
        Self { paths, state: tokio::sync::Mutex::new(State { child: None, pid: None, started_at: None, last_error: None, wanted: true }) }
    }

    pub fn installed(&self) -> bool {
        self.paths.cli().is_file()
    }

    pub fn version(&self) -> Option<String> {
        let text = std::fs::read_to_string(self.paths.pkg_json()).ok()?;
        serde_json::from_str::<Value>(&text).ok()?.get("version")?.as_str().map(str::to_string)
    }

    /// The absolute node bin directory recorded in `node-bin`: the service respawns under
    /// systemd's minimal PATH, where no node is.
    pub fn node_bin_dir(&self) -> Option<String> {
        let v = std::fs::read_to_string(self.paths.node_bin_file()).ok()?;
        Some(v.trim().to_string()).filter(|v| !v.is_empty())
    }

    /// The OpenRouter key from opencode's auth store. Never logged or written anywhere.
    pub fn openrouter_key(&self) -> Option<String> {
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(&self.paths.auth_json).ok()?).ok()?;
        let key = doc.get(PROVIDER_ID)?.get("key")?.as_str()?.trim().to_string();
        Some(key).filter(|k| !k.is_empty())
    }

    /// The model route in the three parts that fail alike: provider declared, key available, and
    /// the default selection pointing at it.
    pub fn route_state(&self) -> Value {
        let file = self.paths.settings_file();
        let doc = Settings::new(file.clone()).read().unwrap_or_default();
        let none = serde_yaml::Value::Null;
        let plugin = doc.get(PLUGIN_KEY).unwrap_or(&none);
        let providers = plugin.get("providers").and_then(|p| p.as_mapping());
        let selected = settings::selection(&doc);
        let text = |k: &str| selected.get(k).and_then(Value::as_str).map(str::to_string).unwrap_or_else(|| "None".into());
        let has_selection = selected.as_object().is_some_and(|m| !m.is_empty());
        json!({
            "settings": file,
            "provider_declared": providers.is_some_and(|p| p.contains_key(PROVIDER_ID)),
            "models": settings::declared_models(&doc),
            "default_model": has_selection.then(|| format!("{}/{}", text("provider"), text("model"))),
            "default_model_ours": selected.get("provider").and_then(Value::as_str) == Some(PROVIDER_ID),
            "key_env": API_KEY_ENV,
            "key_available": self.openrouter_key().is_some(),
            "key_source": self.paths.auth_json,
        })
    }

    fn alive(st: &mut State) -> bool {
        st.child.as_mut().is_some_and(|c| matches!(c.try_wait(), Ok(None)))
    }

    /// The pid of the harness while it runs.
    pub async fn pid(&self) -> Option<u32> {
        let mut st = self.state.lock().await;
        if Self::alive(&mut st) {
            st.pid
        } else {
            None
        }
    }

    pub async fn snapshot(&self) -> Value {
        let (running, pid, code, uptime, error) = {
            let mut st = self.state.lock().await;
            let running = Self::alive(&mut st);
            let code = st.child.as_mut().and_then(|c| c.try_wait().ok().flatten()).map(exit_code);
            let uptime = st.started_at.filter(|_| running).map(|t| (t.elapsed().as_secs_f64() * 10.0).round() / 10.0);
            (running, st.pid.filter(|_| running), code, uptime, st.last_error.clone())
        };
        json!({
            "installed": self.installed(),
            "version": self.version(),
            "running": running,
            "pid": pid,
            "exit_code": code,
            "port": self.paths.port,
            "listening": listening(self.paths.port).await,
            "uptime_s": uptime,
            "home": self.paths.home,
            "runtime": self.paths.fork_dir,
            "workdir": self.paths.workdir,
            "node_bin": self.node_bin_dir(),
            "source": source_state(&self.paths.fork_dir).await,
            "model_route": self.route_state(),
            "error": error,
        })
    }

    /// Launch the harness. The caller holds the lock.
    fn spawn(&self, st: &mut State) -> Result<(), String> {
        let p = &self.paths;
        if !self.installed() {
            return Err(format!(
                "FileNotFoundError: the harness fork at {} has no built CLI at {} — build the fork (core/plugins/dsh/INSTALL.md)",
                p.fork_dir.display(),
                p.cli().display()
            ));
        }
        let node_bin = self.node_bin_dir();
        let node = node_bin.as_ref().map(|d| Path::new(d).join("node")).unwrap_or_else(|| PathBuf::from("node"));
        let mut cmd = tokio::process::Command::new(node);
        cmd.arg(p.cli())
            .args(["--profile", "web", "--host", "127.0.0.1", "--port", &p.port.to_string(), "--no-open"])
            .current_dir(&p.workdir)
            .env("DSH_HOME", &p.home);
        if let Some(home) = std::env::var_os("HOME") {
            cmd.env("HOME", home);
        }
        if let Some(nb) = &node_bin {
            let path = std::env::var("PATH").unwrap_or_default();
            cmd.env("PATH", format!("{nb}:{path}"));
        }
        match self.openrouter_key() {
            Some(key) => {
                cmd.env(API_KEY_ENV, key);
            }
            None => tracing::warn!(
                "dsh: no OpenRouter key in {} — the harness will start but its model picker will be empty",
                p.auth_json.display()
            ),
        }
        let err = |e: std::io::Error| format!("{}: {e}", if e.kind() == std::io::ErrorKind::NotFound { "FileNotFoundError" } else { "OSError" });
        std::fs::create_dir_all(&p.home).map_err(err)?;
        std::fs::create_dir_all(&p.state_dir).map_err(err)?;
        // Append: the log is the only account of a crash between two `status` calls.
        let log = std::fs::OpenOptions::new().create(true).append(true).open(p.log_file()).map_err(err)?;
        let out = log.try_clone().map_err(err)?;
        in_group(&mut cmd).stdin(std::process::Stdio::null()).stdout(out).stderr(log);
        tracing::info!("dsh: launching {} (cwd={})", p.cli().display(), p.workdir.display());
        let child = cmd.spawn().map_err(err)?;
        st.pid = child.id();
        st.child = Some(child);
        st.started_at = Some(Instant::now());
        st.last_error = None;
        Ok(())
    }

    /// Start the harness unless it runs, and wait until it binds its port.
    pub async fn start(&self) -> Result<Value, String> {
        {
            let mut st = self.state.lock().await;
            st.wanted = true;
            if Self::alive(&mut st) {
                drop(st);
                return Ok(with_action(self.snapshot().await, "already-running"));
            }
            if let Err(e) = self.spawn(&mut st) {
                st.last_error = Some(e.clone());
                return Err(e);
            }
        }
        self.await_listening().await;
        Ok(with_action(self.snapshot().await, "started"))
    }

    /// Wait until the harness binds its port, it exits, or the start timeout passes.
    pub async fn await_listening(&self) {
        let p = &self.paths;
        let deadline = Instant::now() + p.start_timeout;
        while Instant::now() < deadline {
            if listening(p.port).await {
                return;
            }
            {
                let mut st = self.state.lock().await;
                if !Self::alive(&mut st) {
                    let code = st.child.as_mut().and_then(|c| c.try_wait().ok().flatten()).map(exit_code);
                    let code = code.map(|c| c.to_string()).unwrap_or_else(|| "?".into());
                    st.last_error =
                        Some(format!("the harness exited with {code} before binding {}; see {}", p.port, p.log_file().display()));
                    return;
                }
            }
            tokio::time::sleep(Duration::from_millis(400)).await;
        }
        self.state.lock().await.last_error =
            Some(format!("the harness did not bind {} within {}s", p.port, py_float(p.start_timeout.as_secs_f64())));
    }

    /// Stop the harness group and keep it down until the next `start`.
    pub async fn stop(&self) -> Value {
        let mut st = self.state.lock().await;
        st.wanted = false;
        st.started_at = None;
        let child = st.child.take();
        st.pid = None;
        let Some(mut child) = child else { return json!({"action": "not-running", "running": false}) };
        if !matches!(child.try_wait(), Ok(None)) {
            return json!({"action": "not-running", "running": false});
        }
        // The whole group: the CLI, node and anything node spawned would hold the port.
        stop_group(&mut child, STOP_TIMEOUT).await;
        json!({"action": "stopped", "running": false})
    }

    pub async fn restart(&self) -> Result<Value, String> {
        self.stop().await;
        Ok(with_action(self.start().await?, "restarted"))
    }

    /// Respawn the harness when it died while wanted. Cheap; the health loop calls it.
    pub async fn reconcile(&self) -> Value {
        let mut st = self.state.lock().await;
        if !st.wanted || Self::alive(&mut st) || !self.installed() {
            return json!({"action": "none"});
        }
        let code = st.child.as_mut().and_then(|c| c.try_wait().ok().flatten()).map(exit_code);
        match self.spawn(&mut st) {
            Ok(()) => json!({"action": "respawned", "previous_exit": code}),
            Err(e) => {
                st.last_error = Some(e.clone());
                json!({"action": "respawn-failed", "error": e})
            }
        }
    }

    /// The last `tail` lines of `dsh.log`, line endings kept.
    pub fn logs(&self, tail: usize) -> String {
        let f = self.paths.log_file();
        match std::fs::read(&f) {
            Ok(bytes) => {
                let text = String::from_utf8_lossy(&bytes);
                let lines: Vec<&str> = text.split_inclusive('\n').collect();
                lines[lines.len().saturating_sub(tail)..].concat()
            }
            Err(e) => format!("(no log at {}: {e})", f.display()),
        }
    }
}

fn with_action(mut snap: Value, action: &str) -> Value {
    snap["action"] = json!(action);
    snap
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fake_fork(dir: &Path, script: &str) -> Paths {
        let paths = Paths::under(dir, 1);
        std::fs::create_dir_all(paths.cli().parent().unwrap()).unwrap();
        std::fs::write(paths.cli(), "").unwrap();
        // A fake `node` that runs `script`: the supervisor launches `<node-bin>/node <cli> ...`.
        let bin = dir.join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        std::fs::write(bin.join("node"), format!("#!/bin/sh\n{script}\n")).unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(bin.join("node"), std::fs::Permissions::from_mode(0o755)).unwrap();
        std::fs::create_dir_all(&paths.state_dir).unwrap();
        std::fs::write(paths.node_bin_file(), format!("{}\n", bin.display())).unwrap();
        paths
    }

    #[tokio::test]
    async fn stop_is_not_undone_by_the_health_tick() {
        let dir = tempfile::tempdir().unwrap();
        let mut paths = fake_fork(dir.path(), "echo up; exec sleep 30");
        paths.start_timeout = Duration::from_millis(300);
        let sup = Supervisor::new(paths);
        sup.start().await.unwrap();
        assert!(sup.pid().await.is_some());
        assert_eq!(sup.stop().await["action"], "stopped");
        assert_eq!(sup.reconcile().await["action"], "none", "a stopped harness stays stopped");
        assert!(sup.pid().await.is_none());
        sup.start().await.unwrap();
        assert!(sup.pid().await.is_some(), "start wants it again");
        sup.stop().await;
    }

    #[tokio::test]
    async fn a_crashed_harness_is_respawned_while_wanted() {
        let dir = tempfile::tempdir().unwrap();
        let mut paths = fake_fork(dir.path(), "echo up; exit 3");
        paths.start_timeout = Duration::from_secs(5);
        let sup = Supervisor::new(paths);
        sup.start().await.unwrap();
        let snap = sup.snapshot().await;
        assert_eq!(snap["running"], false);
        assert_eq!(snap["exit_code"], 3);
        assert!(snap["error"].as_str().unwrap().contains("exited with 3 before binding"));
        let res = sup.reconcile().await;
        assert_eq!(res["action"], "respawned");
        assert_eq!(res["previous_exit"], 3);
        assert!(sup.logs(5).contains("up\n"));
        sup.stop().await;
    }

    #[test]
    fn the_route_reports_its_parts_apart() {
        let dir = tempfile::tempdir().unwrap();
        let paths = Paths::under(dir.path(), 1);
        let sup = Supervisor::new(paths.clone());
        let r = sup.route_state();
        assert_eq!((r["provider_declared"].clone(), r["default_model"].clone(), r["key_available"].clone()), (json!(false), Value::Null, json!(false)));
        Settings::new(paths.settings_file()).ensure();
        std::fs::write(&paths.auth_json, r#"{"openrouter": {"key": " test-not-a-key "}}"#).unwrap();
        let r = sup.route_state();
        assert_eq!(r["provider_declared"], true);
        assert_eq!(r["default_model"], format!("openrouter/{}", crate::settings::MODELS[0]));
        assert_eq!(r["default_model_ours"], true);
        assert_eq!(r["key_available"], true);
        assert_eq!(sup.openrouter_key().as_deref(), Some("test-not-a-key"));
    }

}
