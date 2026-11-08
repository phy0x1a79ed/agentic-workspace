//! Where dsh's state, the fork and the model route live, read once from the environment.

use std::path::PathBuf;
use std::time::Duration;

/// The harness's loopback port. The `[[service.front]]` upstream in `awm-plugin.toml` names it too.
pub const PORT: u16 = 12311;

/// The paths and knobs the Python service read from its environment, with the same names and
/// defaults (`DSH_STATE_DIR`, `DSH_HOME`, `DSH_FORK_DIR`, `DSH_WORKDIR`,
/// `DSH_HEALTH_INTERVAL_S`, `DSH_START_TIMEOUT_S`, `DSH_AUTH_JSON`).
#[derive(Debug, Clone)]
pub struct Paths {
    pub state_dir: PathBuf,
    pub home: PathBuf,
    pub fork_dir: PathBuf,
    pub port: u16,
    pub workdir: PathBuf,
    pub workspace: PathBuf,
    pub health_interval: Duration,
    pub start_timeout: Duration,
    pub auth_json: PathBuf,
}

fn var(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|v| !v.is_empty())
}

fn secs(name: &str, default: f64) -> Duration {
    Duration::from_secs_f64(var(name).and_then(|v| v.trim().parse().ok()).filter(|s: &f64| *s > 0.0).unwrap_or(default))
}

fn user_home() -> PathBuf {
    var("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

impl Paths {
    pub fn from_env() -> Self {
        let awm_dir = awm_plugin_sdk::config::awm_dir();
        let workspace = awm_plugin_sdk::config::workspace_root();
        let state_dir = var("DSH_STATE_DIR").map(PathBuf::from).unwrap_or_else(|| awm_dir.join("services").join("dsh"));
        Self {
            home: var("DSH_HOME").map(PathBuf::from).unwrap_or_else(|| state_dir.join("home")),
            fork_dir: var("DSH_FORK_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|| workspace.join("projects").join("deepseek-harness").join("release")),
            port: PORT,
            workdir: var("DSH_WORKDIR").map(PathBuf::from).unwrap_or_else(|| workspace.clone()),
            health_interval: secs("DSH_HEALTH_INTERVAL_S", 20.0),
            start_timeout: secs("DSH_START_TIMEOUT_S", 60.0),
            auth_json: var("DSH_AUTH_JSON")
                .map(PathBuf::from)
                .unwrap_or_else(|| user_home().join(".local/share/opencode/auth.json")),
            state_dir,
            workspace,
        }
    }

    /// Paths rooted in `dir`, for tests: state in `dir/state`, the fork in `dir/fork`.
    #[cfg(test)]
    pub fn under(dir: &std::path::Path, port: u16) -> Self {
        let state_dir = dir.join("state");
        Self {
            home: state_dir.join("home"),
            fork_dir: dir.join("fork"),
            port,
            workdir: dir.to_path_buf(),
            workspace: dir.join("ws"),
            health_interval: Duration::from_secs(20),
            start_timeout: Duration::from_secs(60),
            auth_json: dir.join("auth.json"),
            state_dir,
        }
    }

    pub fn node_bin_file(&self) -> PathBuf {
        self.state_dir.join("node-bin")
    }

    pub fn log_file(&self) -> PathBuf {
        self.state_dir.join("dsh.log")
    }

    pub fn settings_file(&self) -> PathBuf {
        self.home.join("settings.yaml")
    }

    pub fn spool(&self) -> PathBuf {
        self.state_dir.join("unsent.jsonl")
    }

    /// The CLI the fork's build emits: `apps/cli` declares `bin: lib/bin.js`.
    pub fn cli(&self) -> PathBuf {
        self.fork_dir.join("apps/cli/lib/bin.js")
    }

    pub fn pkg_json(&self) -> PathBuf {
        self.fork_dir.join("package.json")
    }
}
