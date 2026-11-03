//! The install config, `<state>/config.toml`. The node's installer writes it (fleet.md R140):
//! awm itself names no host, so which plugins a node runs lives here, not in a plugin.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

pub const CONFIG_FILE: &str = "config.toml";
pub const DEFAULT_LISTEN: &str = "127.0.0.1:7819";
pub const DEFAULT_RESERVED: [&str; 5] = ["awm", "2fa", "approver", "queue-store", "journal"];

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
    /// Core plugin roots, in order. Only these may hold reserved tool names.
    #[serde(default)]
    pub core_roots: Vec<PathBuf>,
    /// Further plugin roots, after the core roots.
    #[serde(default)]
    pub plugin_roots: Vec<PathBuf>,
    /// Where a page with no `dir` is found by name, as `<root>/<name>/dist`.
    #[serde(default)]
    pub page_roots: Vec<PathBuf>,
    /// Gateway profiles that legacy `service.toml` `profiles` match against.
    #[serde(default)]
    pub profiles: Vec<String>,
    /// Plugin names this node loads. `"*"` loads every plugin in the roots.
    #[serde(default = "every_plugin")]
    pub plugins: Vec<String>,
    /// Plugin names this node never loads.
    #[serde(default)]
    pub exclude: Vec<String>,
    /// Tool renames that settle a name clash (R116 item 5): `{"<declared>" = "<public>"}`.
    #[serde(default)]
    pub rename: BTreeMap<String, String>,
    #[serde(default = "default_reserved")]
    pub reserved: Vec<String>,
    /// The node's edge list. Default: `AWM_EDGES_FILE`, else `<state>/edges.yml`.
    /// Its `singletons[].tool` names join the reserved list. A missing file is an empty fleet.
    #[serde(default)]
    pub edges_file: Option<PathBuf>,
    /// Extra environment for every service, such as `DEV_PYTHONPATH`.
    #[serde(default)]
    pub env: BTreeMap<String, String>,
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

    pub fn edges_path(&self, state: &Path) -> PathBuf {
        self.edges_file
            .clone()
            .or_else(|| std::env::var_os("AWM_EDGES_FILE").map(PathBuf::from))
            .unwrap_or_else(|| state.join("edges.yml"))
    }

    /// The reserved list plus the singleton tools of the edges file.
    pub fn reserved_names(&self, state: &Path) -> Vec<String> {
        let mut names = self.reserved.clone();
        let path = self.edges_path(state);
        if path.exists() {
            match singleton_tools(&path) {
                Ok(extra) => names.extend(extra),
                Err(e) => tracing::warn!("edges file {}: {e}", path.display()),
            }
        }
        names.sort();
        names.dedup();
        names
    }
}

fn singleton_tools(path: &Path) -> anyhow::Result<Vec<String>> {
    #[derive(Deserialize)]
    struct Edges {
        #[serde(default)]
        singletons: Vec<Singleton>,
    }
    #[derive(Deserialize)]
    struct Singleton {
        tool: String,
    }
    let edges: Edges = serde_yaml::from_str(&std::fs::read_to_string(path)?)?;
    Ok(edges.singletons.into_iter().map(|s| s.tool).collect())
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
    fn defaults_and_singletons() {
        let dir = tempfile::tempdir().unwrap();
        let edges = dir.path().join("edges.yml");
        std::fs::write(&edges, "singletons:\n  - {tool: browser, host: alcor}\n  - {tool: 2fa, host: alcor}\n").unwrap();
        let cfg = Config { exclude: vec!["engcyc".into()], ..Config::default() };
        assert_eq!(cfg.listen, DEFAULT_LISTEN);
        assert!(cfg.wants("journal") && !cfg.wants("engcyc"));
        assert_eq!(cfg.edges_path(dir.path()), edges);
        let names = cfg.reserved_names(dir.path());
        assert!(names.contains(&"browser".to_string()));
        assert_eq!(names.iter().filter(|n| *n == "2fa").count(), 1);
    }
}
