//! The node config a plugin reads (`awm.config`): its state paths, the install config, the
//! edge list and the node's name.

use std::path::{Path, PathBuf};
use std::sync::RwLock;

pub use awm_trust::edges::EdgeList;

pub const INSTALL_CONFIG_FILE: &str = "config.toml";
pub const INSTALL_CONFIG_ENV: &str = "AWM_INSTALL_CONFIG";

static CANONICAL: RwLock<Option<PathBuf>> = RwLock::new(None);

/// `AWM_WORKSPACE`, else the nearest directory at or above the cwd that holds both `AGENTS.md`
/// and `projects/`, else the nearest that holds `AGENTS.md`, else the cwd.
pub fn workspace_root() -> PathBuf {
    if let Some(ws) = env_path("AWM_WORKSPACE") {
        return absolute(&ws);
    }
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    workspace_above(&cwd)
}

/// A scope worktree holds its own `AGENTS.md`, so only `projects/` tells the workspace apart.
fn workspace_above(dir: &Path) -> PathBuf {
    let agents = |p: &&Path| p.join("AGENTS.md").exists();
    dir.ancestors()
        .find(|p| agents(p) && p.join("projects").is_dir())
        .or_else(|| dir.ancestors().find(agents))
        .map(Path::to_path_buf)
        .unwrap_or_else(|| dir.to_path_buf())
}

/// The workspace the gateway named on register, else [`workspace_root`].
pub fn canonical_workspace() -> PathBuf {
    CANONICAL.read().unwrap_or_else(|p| p.into_inner()).clone().unwrap_or_else(workspace_root)
}

pub(crate) fn set_canonical_workspace(path: &str) {
    if !path.trim().is_empty() {
        *CANONICAL.write().unwrap_or_else(|p| p.into_inner()) = Some(absolute(Path::new(path.trim())));
    }
}

/// The node's state directory: `AWM_DIR`, else `<workspace>/.awm`.
pub fn awm_dir() -> PathBuf {
    env_path("AWM_DIR").map(|p| absolute(&p)).unwrap_or_else(|| workspace_root().join(".awm"))
}

/// `<AWM_DIR>/services`, where a service keeps its own files under its name.
pub fn services_dir() -> PathBuf {
    awm_dir().join("services")
}

/// `<AWM_DIR>/services/<service>`: one service's own state directory.
pub fn service_dir(service: &str) -> PathBuf {
    services_dir().join(service)
}

pub fn projects_dir() -> PathBuf {
    workspace_root().join("projects")
}

/// The log file the gateway writes a spawned service's stdout and stderr to.
pub fn log_file(service: &str) -> PathBuf {
    awm_dir().join("logs").join(format!("{service}.log"))
}

/// This node's fleet name: `AWM_NODE_NAME`, else the hostname.
pub fn node_name() -> String {
    match std::env::var("AWM_NODE_NAME") {
        Ok(n) if !n.trim().is_empty() => n.trim().to_string(),
        _ => hostname(),
    }
}

fn hostname() -> String {
    let mut buf = [0u8; 256];
    // SAFETY: gethostname writes at most buf.len() bytes into buf.
    let rc = unsafe { libc::gethostname(buf.as_mut_ptr().cast(), buf.len()) };
    if rc != 0 {
        return String::new();
    }
    let end = buf.iter().position(|b| *b == 0).unwrap_or(buf.len());
    String::from_utf8_lossy(&buf[..end]).into_owned()
}

/// A node name as the edge list writes it: the first DNS label, lowercased.
pub fn label(name: &str) -> String {
    name.trim().split('.').next().unwrap_or("").to_lowercase()
}

/// This node's name as the edge list names it.
pub fn here() -> String {
    label(&node_name())
}

/// The edge list: `AWM_EDGES_FILE`, else `<AWM_DIR>/edges.yml`. A missing file is an empty fleet.
pub fn edges() -> anyhow::Result<EdgeList> {
    EdgeList::load(&awm_trust::edges::edges_path(&awm_dir()))
}

/// The node in an edge stamp `peer:<node>`, else None.
pub fn caller_peer(as_: Option<&str>) -> Option<&str> {
    as_?.strip_prefix("peer:").map(str::trim).filter(|n| !n.is_empty())
}

/// Whether this node takes a call stamped `as_`: a `peer:<node>` stamp only from a node with
/// an edge to this one. A bare `peer` fails closed. Any other stamp passes.
pub fn admits(edges: &EdgeList, as_: Option<&str>) -> bool {
    let stamp = as_.unwrap_or("").trim();
    let lower = stamp.to_lowercase();
    if lower == "peer" {
        return false;
    }
    if !lower.starts_with("peer:") {
        return true;
    }
    let node = label(&stamp[5..]);
    !node.is_empty() && edges.reaches(&node, &here())
}

/// A peer-selector variable such as `AWM_SOCIAL_PEER`: the node to call, or None for local.
pub fn peer_env(var: &str) -> Option<String> {
    std::env::var(var).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty())
}

/// A secret the plugin's `needs.secrets` names: from the environment the gateway spawned it
/// with, else from the `secrets.env` credential systemd loads for a singleton's unit
/// (`gatewayclient/credentials.py`). The lines read as `supervisor::granted_secrets` reads them.
pub fn secret(name: &str) -> Option<String> {
    if let Ok(v) = std::env::var(name) {
        return Some(v);
    }
    let dir = std::env::var_os("CREDENTIALS_DIRECTORY")?;
    let text = std::fs::read_to_string(Path::new(&dir).join("secrets.env")).ok()?;
    parse_secrets(&text).remove(name)
}

fn parse_secrets(text: &str) -> std::collections::BTreeMap<String, String> {
    text.lines()
        .map(str::trim)
        .filter(|l| !l.is_empty() && !l.starts_with('#'))
        .filter_map(|l| l.strip_prefix("export ").unwrap_or(l).split_once('='))
        .map(|(k, v)| (k.trim().to_string(), v.trim().trim_matches('"').to_string()))
        .collect()
}

/// `<project>, <scope>` of a directory under `projects`. The scope is the deepest enclosing
/// directory that holds a `.git`; a directory under none falls back to its own path.
pub fn scope_of(cwd: &Path, projects: &Path) -> Option<(String, Option<String>)> {
    let root = absolute(projects);
    let dir = absolute(cwd);
    let parts: Vec<String> = dir.strip_prefix(&root).ok()?.iter().map(|p| p.to_string_lossy().into_owned()).collect();
    let project = parts.first()?.clone();
    for depth in (2..=parts.len()).rev() {
        if root.join(parts[..depth].join("/")).join(".git").exists() {
            return Some((project, Some(parts[1..depth].join("/"))));
        }
    }
    let rest = parts[1..].join("/");
    Some((project, (!rest.is_empty()).then_some(rest)))
}

/// The URL of this node's own edge as another device reaches it: `AWM_EDGE_URL`, else its
/// mesh address and `AWM_HTTPS_PORT`. None when neither is known.
pub fn edge_url() -> Option<String> {
    if let Some(url) = peer_env("AWM_EDGE_URL") {
        return Some(url.trim_end_matches('/').to_string());
    }
    let addr = mesh_address()?;
    Some(format!("https://{addr}:{}", std::env::var("AWM_HTTPS_PORT").unwrap_or_else(|_| "8443".into())))
}

/// This host's own address on the fleet mesh (`AWM_MESH_SUBNET`, default `10.74.81.0/24`).
pub fn mesh_address() -> Option<String> {
    let subnet = peer_env("AWM_MESH_SUBNET").unwrap_or_else(|| "10.74.81.0/24".into());
    let (net, bits) = subnet.split_once('/')?;
    let net: std::net::Ipv4Addr = net.parse().ok()?;
    let bits: u32 = bits.parse().ok().filter(|b| *b <= 32)?;
    let mask = if bits == 0 { 0 } else { u32::MAX << (32 - bits) };
    let out = std::process::Command::new("hostname").arg("-I").output().ok()?;
    String::from_utf8_lossy(&out.stdout)
        .split_whitespace()
        .filter_map(|t| t.parse::<std::net::Ipv4Addr>().ok())
        .find(|a| u32::from(*a) & mask == u32::from(net) & mask)
        .map(|a| a.to_string())
}

/// The install config `<AWM_DIR>/config.toml`, read leniently: a plugin reads its own keys
/// and ignores the gateway's.
#[derive(Debug, Clone, Default)]
pub struct InstallConfig {
    pub path: PathBuf,
    pub table: toml::Table,
}

impl InstallConfig {
    /// `AWM_INSTALL_CONFIG`, else `<AWM_DIR>/config.toml`. A missing file is an empty table.
    pub fn load() -> anyhow::Result<Self> {
        let path = env_path(INSTALL_CONFIG_ENV).unwrap_or_else(|| awm_dir().join(INSTALL_CONFIG_FILE));
        Self::load_from(&path)
    }

    pub fn load_from(path: &Path) -> anyhow::Result<Self> {
        let table = match std::fs::read_to_string(path) {
            Ok(text) => text.parse::<toml::Table>().map_err(|e| anyhow::anyhow!("{}: {e}", path.display()))?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => toml::Table::new(),
            Err(e) => return Err(anyhow::anyhow!("{}: {e}", path.display())),
        };
        Ok(Self { path: path.to_path_buf(), table })
    }

    pub fn get(&self, key: &str) -> Option<&toml::Value> {
        self.table.get(key)
    }

    /// The `[fleet]` table, which the routine plugin reads.
    pub fn fleet(&self) -> Option<&toml::Table> {
        self.table.get("fleet").and_then(toml::Value::as_table)
    }

    /// The `[env]` table every service sees.
    pub fn env(&self) -> std::collections::BTreeMap<String, String> {
        self.table
            .get("env")
            .and_then(toml::Value::as_table)
            .map(|t| t.iter().filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_string()))).collect())
            .unwrap_or_default()
    }
}

fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var(name).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty()).map(PathBuf::from)
}

fn absolute(p: &Path) -> PathBuf {
    p.canonicalize().unwrap_or_else(|_| std::path::absolute(p).unwrap_or_else(|_| p.to_path_buf()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stamps_and_labels() {
        assert_eq!(caller_peer(Some("peer:mira ")), Some("mira"));
        assert_eq!(caller_peer(Some("tony")), None);
        assert_eq!(label("Mizar.local"), "mizar");
        let edges = EdgeList::default();
        assert!(!admits(&edges, Some("peer")) && admits(&edges, Some("user:tony")) && admits(&edges, None));
        assert!(!admits(&edges, Some("peer:far")));
        let s = parse_secrets("# c\nexport A=\"1\"\nB = two\nbad\n");
        assert_eq!((s["A"].as_str(), s["B"].as_str(), s.len()), ("1", "two", 2));
    }

}
