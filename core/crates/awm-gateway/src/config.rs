//! The install config, `<state>/config.toml`. The node's installer writes it (fleet.md R140):
//! awm itself names no host, so which plugins a node runs lives here, not in a plugin.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub const CONFIG_FILE: &str = "config.toml";
pub const DEFAULT_LISTEN: &str = "127.0.0.1:7819";
/// The awm tree's plugin roots, below its `awm/` folder. Every in-tree plugin is in one of them.
pub const TREE_ROOTS: [&str; 2] = ["services", "plugins"];
pub const DEFAULT_RESERVED: [&str; 6] = ["awm", "auth", "2fa", "approver", "queue-store", "journal"];

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// This node's name. Default: `AWM_NODE_NAME`, else the hostname.
    #[serde(default)]
    pub node: Option<String>,
    #[serde(default = "default_listen")]
    pub listen: String,
    /// A second address that serves only the isolated-session door, `/mcp`, for containers
    /// that cannot reach loopback. `8765` is the port cch's container config names.
    #[serde(default)]
    pub isolated_listen: Option<String>,
    /// The workspace services see as `AWM_WORKSPACE`.
    #[serde(default)]
    pub workspace: Option<PathBuf>,
    /// Core plugin roots, in order. Only these may hold reserved tool names. Default: the awm
    /// tree's own roots under the workspace, `awm/services` and `awm/plugins` ([`TREE_ROOTS`]).
    #[serde(default)]
    pub core_roots: Vec<PathBuf>,
    /// Further plugin roots, after the core roots.
    #[serde(default)]
    pub plugin_roots: Vec<PathBuf>,
    /// Gateway profiles that a service's `profiles` match against.
    #[serde(default)]
    pub profiles: Vec<String>,
    /// Plugin names this node loads. `"*"` loads every plugin in the roots.
    #[serde(default = "every_plugin")]
    pub plugins: Vec<String>,
    /// Plugin names this node never loads.
    #[serde(default)]
    pub exclude: Vec<String>,
    #[serde(default = "default_reserved")]
    pub reserved: Vec<String>,
    /// The node's edge list. Default: `AWM_EDGES_FILE`, else `<state>/edges.yml`.
    /// Its `singletons[].tool` names join the reserved list. A missing file is an empty fleet.
    #[serde(default)]
    pub edges_file: Option<PathBuf>,
    /// Extra environment for every service, such as `PYTHONPATH`. Never a secret:
    /// every service sees it.
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// The node's secrets, as `NAME=value` lines. Default: `<state>/secrets.env`.
    /// A service gets only the names its plugin's `needs.secrets` lists.
    #[serde(default)]
    pub secrets_file: Option<PathBuf>,
    #[serde(default)]
    pub peer: Vec<PeerConfig>,
    /// Services this gateway serves but does not spawn: another user's systemd unit runs them (R99).
    #[serde(default)]
    pub external: Vec<ExternalService>,
    /// The fleet rollout table. The routine plugin reads it. The gateway ignores it.
    #[serde(default)]
    pub fleet: Option<toml::Value>,
    /// Seconds a call waits for a tool that is not up yet (R117 item 7).
    #[serde(default = "default_wait")]
    pub tool_wait_s: f64,
    /// Seconds a positive `reflection session_of` answer is kept. A failed lookup is never kept.
    #[serde(default = "default_session_cache")]
    pub session_cache_s: f64,
    /// Seconds a service may stay off its control WebSocket before a respawn.
    #[serde(default = "default_reconnect")]
    pub reconnect_window_s: f64,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PeerConfig {
    pub name: String,
    /// The peer gateway's base URL, for the HTTP transport.
    pub url: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ExternalService {
    pub service: String,
    /// A file holding the token the service must send as `X-Awm-Service-Token` to register.
    #[serde(default)]
    pub token_file: Option<PathBuf>,
}

fn default_listen() -> String {
    DEFAULT_LISTEN.into()
}
fn every_plugin() -> Vec<String> {
    vec!["*".into()]
}
fn default_reserved() -> Vec<String> {
    DEFAULT_RESERVED.iter().map(|s| s.to_string()).collect()
}
fn default_wait() -> f64 {
    10.0
}
fn default_session_cache() -> f64 {
    5.0
}
fn default_reconnect() -> f64 {
    10.0
}

impl Default for Config {
    fn default() -> Self {
        toml::from_str("").expect("empty config parses")
    }
}

impl Config {
    pub fn load(state: &Path) -> anyhow::Result<Self> {
        let path = state.join(CONFIG_FILE);
        if !path.exists() {
            return Ok(Self::default());
        }
        let text = std::fs::read_to_string(&path)?;
        toml::from_str(&text).map_err(|e| anyhow::anyhow!("{}: {e}", path.display()))
    }

    pub fn node_name(&self) -> String {
        self.node
            .clone()
            .or_else(|| std::env::var("AWM_NODE_NAME").ok().filter(|s| !s.trim().is_empty()))
            .unwrap_or_else(hostname)
    }

    pub fn wants(&self, plugin: &str) -> bool {
        !self.exclude.iter().any(|p| p == plugin) && self.plugins.iter().any(|p| p == "*" || p == plugin)
    }

    pub fn external(&self, service: &str) -> Option<&ExternalService> {
        self.external.iter().find(|e| e.service == service)
    }

    pub fn secrets_path(&self, state: &Path) -> PathBuf {
        self.secrets_file.clone().unwrap_or_else(|| state.join("secrets.env"))
    }

    pub fn edges_path(&self, state: &Path) -> PathBuf {
        self.edges_file
            .clone()
            .or_else(|| std::env::var_os("AWM_EDGES_FILE").map(PathBuf::from))
            .unwrap_or_else(|| state.join("edges.yml"))
    }

    /// The workspace: the config's, else the parent of the state dir, `<workspace>/.awm`.
    pub fn workspace_dir(&self, state: &Path) -> PathBuf {
        self.workspace.clone().unwrap_or_else(|| state.parent().map(Path::to_path_buf).unwrap_or_default())
    }

    /// The core roots the loader scans: the config's, else the tree's own under the workspace.
    pub fn core_root_dirs(&self, state: &Path) -> Vec<PathBuf> {
        if !self.core_roots.is_empty() {
            return self.core_roots.clone();
        }
        tree_roots(&self.workspace_dir(state).join("awm"))
    }

    /// The reserved list plus the singleton tools of the edges file.
    pub fn reserved_names(&self, state: &Path) -> Vec<String> {
        let mut names = self.reserved.clone();
        let path = self.edges_path(state);
        match awm_trust::edges::EdgeList::load(&path) {
            Ok(edges) => names.extend(edges.singleton_tools()),
            Err(e) => tracing::warn!("edges file {}: {e:#}", path.display()),
        }
        names.sort();
        names.dedup();
        names
    }
}

/// The plugin roots of the awm tree at `awm`.
pub fn tree_roots(awm: &Path) -> Vec<PathBuf> {
    TREE_ROOTS.iter().map(|r| awm.join(r)).collect()
}

pub fn hostname() -> String {
    let mut buf = [0u8; 256];
    // SAFETY: gethostname writes at most buf.len() bytes into a buffer we own.
    let rc = unsafe { libc::gethostname(buf.as_mut_ptr().cast(), buf.len()) };
    if rc != 0 {
        return "localhost".into();
    }
    let end = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    String::from_utf8_lossy(&buf[..end]).into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_core_roots_default_to_the_tree_under_the_workspace() {
        let none: Config = toml::from_str("").unwrap();
        let state = Path::new("/w/.awm");
        assert_eq!(none.core_root_dirs(state), vec![PathBuf::from("/w/awm/services"), PathBuf::from("/w/awm/plugins")]);
        let named: Config = toml::from_str("workspace = \"/x\"\ncore_roots = [\"/r\"]").unwrap();
        assert_eq!(named.core_root_dirs(state), vec![PathBuf::from("/r")]);
        let ws: Config = toml::from_str("workspace = \"/x\"").unwrap();
        assert_eq!(ws.core_root_dirs(state)[0], PathBuf::from("/x/awm/services"));
    }

    #[test]
    fn defaults_and_singletons() {
        let dir = tempfile::tempdir().unwrap();
        let edges = dir.path().join("edges.yml");
        std::fs::write(
            &edges,
            "nodes:\n  alcor: {role: standby}\nsingleton_doors:\n  alcor: {port: 2201, network: lan}\n\
             singletons:\n  - {tool: browser, host: alcor, callers: []}\n  - {tool: 2fa, host: alcor, callers: []}\n",
        )
        .unwrap();
        let cfg = Config { exclude: vec!["engcyc".into()], ..Config::default() };
        assert_eq!(cfg.listen, DEFAULT_LISTEN);
        assert!(cfg.wants("journal") && !cfg.wants("engcyc"));
        assert_eq!(cfg.edges_path(dir.path()), edges);
        let names = cfg.reserved_names(dir.path());
        assert!(names.contains(&"browser".to_string()));
        assert_eq!(names.iter().filter(|n| *n == "2fa").count(), 1);
    }
}
