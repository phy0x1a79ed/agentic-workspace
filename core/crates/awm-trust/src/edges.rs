//! The edge list, a node's `edges.yml` (fleet.md R87, R96, R98).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde::Deserialize;

pub const EDGES_FILE_ENV: &str = "AWM_EDGES_FILE";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Network {
    Lan,
    Tailscale,
    Zerotier,
    Internet,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Node {
    pub role: String,
    #[serde(default)]
    pub managed: bool,
    #[serde(default)]
    pub addresses: BTreeMap<String, Option<String>>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Door {
    pub port: u16,
    pub network: Network,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Singleton {
    pub tool: String,
    pub host: String,
    #[serde(default)]
    pub after: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct EdgeList {
    #[serde(default)]
    pub nodes: BTreeMap<String, Node>,
    #[serde(default)]
    pub edges: BTreeMap<String, BTreeMap<String, Network>>,
    #[serde(default)]
    pub singleton_doors: BTreeMap<String, Door>,
    #[serde(default)]
    pub singletons: Vec<Singleton>,
}

const ROLES: [&str; 9] = [
    "laptop", "desktop", "homebase", "standby", "retiring", "work", "station", "backup", "admin",
];

/// `AWM_EDGES_FILE`, else `<awm_dir>/edges.yml`.
pub fn edges_path(awm_dir: &Path) -> PathBuf {
    match std::env::var(EDGES_FILE_ENV) {
        Ok(p) if !p.trim().is_empty() => PathBuf::from(p.trim()),
        _ => awm_dir.join("edges.yml"),
    }
}

impl EdgeList {
    /// Parse and check. A missing file is an empty fleet.
    pub fn load(path: &Path) -> Result<Self> {
        match std::fs::read_to_string(path) {
            Ok(text) => Self::parse(&text).with_context(|| path.display().to_string()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(e).with_context(|| path.display().to_string()),
        }
    }

    pub fn parse(text: &str) -> Result<Self> {
        let list: EdgeList = serde_yaml::from_str(text)?;
        for (name, node) in &list.nodes {
            if !ROLES.contains(&node.role.as_str()) {
                bail!("node {name} role {:?} is not one of {ROLES:?}", node.role);
            }
        }
        for (src, targets) in &list.edges {
            if !list.nodes.contains_key(src) {
                bail!("edge from unknown node {src}");
            }
            for dst in targets.keys() {
                if !list.nodes.contains_key(dst) {
                    bail!("edge {src} -> unknown node {dst}");
                }
                if dst == src {
                    bail!("self edge on {src}");
                }
            }
        }
        for s in &list.singletons {
            if !list.singleton_doors.contains_key(&s.host) {
                bail!("singleton {} on {}, which has no door", s.tool, s.host);
            }
        }
        Ok(list)
    }

    /// Whether `src` holds an edge to `dst`. A node always reaches itself.
    pub fn reaches(&self, src: &str, dst: &str) -> bool {
        src == dst || self.edges.get(src).is_some_and(|t| t.contains_key(dst))
    }

    pub fn reachable_from(&self, src: &str) -> Vec<String> {
        self.edges.get(src).map(|t| t.keys().cloned().collect()).unwrap_or_default()
    }

    pub fn reached_by(&self, dst: &str) -> Vec<String> {
        self.edges
            .iter()
            .filter(|(_, t)| t.contains_key(dst))
            .map(|(s, _)| s.clone())
            .collect()
    }

    pub fn network(&self, src: &str, dst: &str) -> Option<Network> {
        self.edges.get(src).and_then(|t| t.get(dst)).copied()
    }

    pub fn singleton_tools(&self) -> Vec<String> {
        self.singletons.iter().map(|s| s.tool.clone()).collect()
    }

    /// The singleton tools that `host` serves through its door.
    pub fn door_tools(&self, host: &str) -> Vec<String> {
        self.singletons.iter().filter(|s| s.host == host).map(|s| s.tool.clone()).collect()
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub(crate) const FIXTURE: &str = r#"
nodes:
  laptop: {role: laptop, managed: true}
  home:   {role: homebase, managed: true, addresses: {lan: 10.0.0.2}}
  spare:  {role: standby}
  work:   {role: work}
  relay:  {role: station}
edges:
  laptop: {home: tailscale, work: tailscale, relay: internet}
  home:   {laptop: tailscale, spare: lan, work: tailscale, relay: internet}
  spare:  {home: lan}
singleton_doors:
  spare: {port: 2201, network: tailscale}
singletons:
  - {tool: 2fa, host: spare}
"#;

    #[test]
    fn edges_are_directional() {
        let e = EdgeList::parse(FIXTURE).unwrap();
        assert!(e.reaches("home", "work"));
        assert!(!e.reaches("work", "home"));
        assert!(e.reaches("relay", "relay"));
        assert_eq!(e.reached_by("spare"), vec!["home"]);
        assert_eq!(e.network("home", "spare"), Some(Network::Lan));
        assert_eq!(e.network("spare", "work"), None);
        assert_eq!(e.door_tools("spare"), vec!["2fa"]);
    }

    #[test]
    fn bad_lists_refuse() {
        assert!(EdgeList::parse("nodes: {a: {role: homebase}}\nedges: {a: {b: lan}}\n").is_err());
        assert!(EdgeList::parse("nodes: {a: {role: homebase}, b: {role: work}}\nedges: {a: {b: pigeon}}\n").is_err());
        assert!(EdgeList::parse("nodes: {a: {role: king}}\n").is_err());
    }

    #[test]
    fn missing_file_is_empty() {
        let e = EdgeList::load(Path::new("/nonexistent/edges.yml")).unwrap();
        assert!(e.reachable_from("a").is_empty());
    }
}
