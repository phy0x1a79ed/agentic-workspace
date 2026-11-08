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

/// Which key an edge's caller logs in with (fleet.md R92). A user key is a shell login as
/// Tony. A service key runs the forced command that the target's `authorized_keys` names.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum EdgeKey {
    #[default]
    User,
    Service,
}

/// One directed edge: the network it rides, and the key it logs in with. edges.yml writes it
/// as the bare network name, or as `{network: <net>, key: service}`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Edge {
    pub network: Network,
    pub key: EdgeKey,
}

impl<'de> Deserialize<'de> for Edge {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Full {
            network: Network,
            #[serde(default)]
            key: EdgeKey,
        }
        #[derive(Deserialize)]
        #[serde(untagged)]
        enum Form {
            Bare(Network),
            Full(Full),
        }
        Ok(match Form::deserialize(d)? {
            Form::Bare(network) => Edge { network, key: EdgeKey::User },
            Form::Full(Full { network, key }) => Edge { network, key },
        })
    }
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Node {
    pub role: String,
    /// `false` marks a node that runs no awm. Absent is not `false`.
    #[serde(default)]
    pub managed: Option<bool>,
    #[serde(default)]
    pub addresses: BTreeMap<String, Option<String>>,
    /// The node does not exist yet. Nothing it names takes effect.
    #[serde(default)]
    pub planned: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Door {
    pub port: u16,
    pub network: Network,
    /// The account a caller logs in as. Absent, ssh uses the caller's own login name.
    #[serde(default)]
    pub user: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Singleton {
    pub tool: String,
    pub host: String,
    /// The nodes that may call it besides its host, as directed edges into it (R98 item 3).
    pub callers: Vec<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct EdgeList {
    #[serde(default)]
    pub nodes: BTreeMap<String, Node>,
    #[serde(default)]
    pub edges: BTreeMap<String, BTreeMap<String, Edge>>,
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
        let mut tools = std::collections::BTreeSet::new();
        for s in &list.singletons {
            if !list.singleton_doors.contains_key(&s.host) {
                bail!("singleton {} on {}, which has no door", s.tool, s.host);
            }
            if !tools.insert(&s.tool) {
                bail!("singleton {} is listed twice", s.tool);
            }
            for c in &s.callers {
                if !list.nodes.contains_key(c) {
                    bail!("singleton {} caller {c} is no node", s.tool);
                }
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

    /// Whether `node` has the role `retiring`. No outgoing call dials such a node.
    pub fn retiring(&self, node: &str) -> bool {
        self.nodes.get(node).is_some_and(|n| n.role == "retiring")
    }

    /// Whether `node` is marked `managed: false`: it runs no awm, so no call dials it.
    pub fn runs_no_awm(&self, node: &str) -> bool {
        self.nodes.get(node).is_some_and(|n| n.managed == Some(false))
    }

    /// The nodes `src` may dial: its edges, less every retiring node and every node that runs no awm.
    pub fn dialable_from(&self, src: &str) -> Vec<String> {
        self.reachable_from(src).into_iter().filter(|n| !self.retiring(n) && !self.runs_no_awm(n)).collect()
    }

    pub fn reached_by(&self, dst: &str) -> Vec<String> {
        self.edges
            .iter()
            .filter(|(_, t)| t.contains_key(dst))
            .map(|(s, _)| s.clone())
            .collect()
    }

    pub fn network(&self, src: &str, dst: &str) -> Option<Network> {
        self.edges.get(src).and_then(|t| t.get(dst)).map(|e| e.network)
    }

    /// The key the edge from `src` to `dst` logs in with, when that edge exists.
    pub fn key(&self, src: &str, dst: &str) -> Option<EdgeKey> {
        self.edges.get(src).and_then(|t| t.get(dst)).map(|e| e.key)
    }

    /// Whether `node` may call `tool`. A tool that is no singleton is not held here. A
    /// singleton admits its host and its callers, less any planned node, and no one else.
    pub fn may_call(&self, node: &str, tool: &str) -> bool {
        match self.singletons.iter().find(|s| s.tool == tool) {
            None => true,
            Some(s) => {
                node == s.host
                    || (s.callers.iter().any(|c| c == node) && !self.nodes.get(node).is_some_and(|n| n.planned))
            }
        }
    }

    pub fn singleton(&self, tool: &str) -> Option<&Singleton> {
        self.singletons.iter().find(|s| s.tool == tool)
    }

    /// The singletons `node` reaches through another host's door: each it may call that it
    /// does not host itself (R89, R98 item 3).
    pub fn door_singletons(&self, node: &str) -> Vec<&Singleton> {
        self.singletons.iter().filter(|s| s.host != node && self.may_call(node, &s.tool)).collect()
    }

    /// Where `host`'s singleton door listens: its address on the door's network, and the port.
    /// An `internet` door listens on the node's `public` address.
    pub fn door_address(&self, host: &str) -> Option<(String, u16)> {
        let door = self.singleton_doors.get(host)?;
        let key = match door.network {
            Network::Lan => "lan",
            Network::Tailscale => "tailscale",
            Network::Zerotier => "zerotier",
            Network::Internet => "public",
        };
        let address = self.nodes.get(host)?.addresses.get(key)?.clone()?;
        Some((address, door.port))
    }

    /// The account a caller logs in as at `host`'s singleton door, when the door names one.
    pub fn door_user(&self, host: &str) -> Option<String> {
        self.singleton_doors.get(host)?.user.clone()
    }

    /// The singleton tools that `host` serves through its door to `caller`.
    pub fn door_tools(&self, host: &str, caller: &str) -> Vec<String> {
        self.singletons
            .iter()
            .filter(|s| s.host == host && self.may_call(caller, &s.tool))
            .map(|s| s.tool.clone())
            .collect()
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
  - {tool: 2fa, host: spare, callers: [home, laptop]}
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
        assert_eq!(e.door_tools("spare", "home"), vec!["2fa"]);
    }

    #[test]
    fn a_caller_reaches_each_singleton_through_its_hosts_door() {
        let e = EdgeList::parse(FIXTURE).unwrap();
        let tools = |n: &str| e.door_singletons(n).iter().map(|s| s.tool.clone()).collect::<Vec<_>>();
        assert_eq!(tools("laptop"), vec!["2fa"], "laptop holds no edge to spare and still calls 2fa");
        assert!(tools("spare").is_empty(), "the host calls its own singleton, not its door");
        assert!(tools("work").is_empty(), "a node no singleton names reaches none");
        assert_eq!(e.door_address("spare"), None, "spare holds no tailscale address");
        let e = EdgeList::parse(
            "nodes:\n  station: {role: station, addresses: {public: 203.0.113.7, tailscale: 100.1.1.1}}\n\
             singleton_doors:\n  station: {port: 2201, network: internet}\n",
        )
        .unwrap();
        assert_eq!(e.door_address("station"), Some(("203.0.113.7".into(), 2201)), "an internet door listens on the public address");
        assert_eq!(e.door_user("station"), None, "a door without a user leaves the login to ssh");
        let e = EdgeList::parse(
            "nodes:\n  station: {role: station, addresses: {tailscale: 100.1.1.1}}\n\
             singleton_doors:\n  station: {port: 2201, network: tailscale, user: awm}\n",
        )
        .unwrap();
        assert_eq!(e.door_user("station").as_deref(), Some("awm"));
    }

    #[test]
    fn a_singleton_admits_only_its_host_and_callers() {
        let e = EdgeList::parse(FIXTURE).unwrap();
        assert!(e.may_call("spare", "2fa") && e.may_call("home", "2fa") && e.may_call("laptop", "2fa"));
        assert!(!e.may_call("work", "2fa") && !e.may_call("relay", "2fa") && !e.may_call("nowhere", "2fa"));
        assert!(e.may_call("work", "journal"), "a tool that is no singleton is not held");
        assert!(e.door_tools("spare", "work").is_empty());
        assert!(e.door_tools("home", "home").is_empty(), "home has no singleton");
    }

    #[test]
    fn retiring_nodes_are_not_dialed_but_still_trusted() {
        let e = EdgeList::parse(
            "nodes:\n  home: {role: homebase}\n  old: {role: retiring}\n  work: {role: work}\n\
             edges:\n  home: {old: lan, work: lan}\n  old: {home: lan}\n",
        )
        .unwrap();
        assert!(e.retiring("old"));
        assert!(!e.retiring("work"));
        assert_eq!(e.reachable_from("home"), vec!["old", "work"]);
        assert_eq!(e.dialable_from("home"), vec!["work"]);
        assert!(e.reaches("home", "old"));
        assert_eq!(e.reached_by("home"), vec!["old"]);
    }

    #[test]
    fn a_node_that_runs_no_awm_is_not_dialable() {
        let e = EdgeList::parse(
            "nodes:\n  home: {role: homebase, managed: true}\n  hpc: {role: station, managed: false}\n  work: {role: work}\n\
             edges:\n  home: {hpc: internet, work: lan}\n",
        )
        .unwrap();
        assert!(e.runs_no_awm("hpc") && !e.runs_no_awm("work") && !e.runs_no_awm("home"));
        assert_eq!(e.reachable_from("home"), vec!["hpc", "work"]);
        assert_eq!(e.dialable_from("home"), vec!["work"]);
    }

    #[test]
    fn bad_lists_refuse() {
        assert!(EdgeList::parse("nodes: {a: {role: homebase}}\nedges: {a: {b: lan}}\n").is_err());
        assert!(EdgeList::parse("nodes: {a: {role: homebase}, b: {role: work}}\nedges: {a: {b: pigeon}}\n").is_err());
        assert!(EdgeList::parse("nodes: {a: {role: king}}\n").is_err());
        let door = "nodes: {a: {role: standby}}\nsingleton_doors: {a: {port: 2201, network: lan}}\n";
        assert!(EdgeList::parse(&format!("{door}singletons: [{{tool: t, host: a}}]\n")).is_err(), "callers is required");
        assert!(EdgeList::parse(&format!("{door}singletons: [{{tool: t, host: a, callers: [b]}}]\n")).is_err());
        assert!(EdgeList::parse(&format!("{door}singletons: [{{tool: t, host: a, callers: []}}, {{tool: t, host: a, callers: []}}]\n")).is_err());
    }

    #[test]
    fn an_edge_is_a_network_name_or_a_map_with_its_key() {
        let e = EdgeList::parse(
            "nodes:\n  altair: {role: station}\n  alcor: {role: standby}\n  mizar: {role: homebase}\n\
             edges:\n  altair: {alcor: {network: tailscale, key: service}, mizar: tailscale}\n  mizar: {alcor: {network: lan}}\n",
        )
        .unwrap();
        assert_eq!(e.network("altair", "alcor"), Some(Network::Tailscale));
        assert_eq!(e.key("altair", "alcor"), Some(EdgeKey::Service));
        assert_eq!(e.key("altair", "mizar"), Some(EdgeKey::User), "the bare form is a user-key edge");
        assert_eq!(e.key("mizar", "alcor"), Some(EdgeKey::User), "a map without a key is a user-key edge");
        assert_eq!(e.network("mizar", "alcor"), Some(Network::Lan));
        assert_eq!(e.key("alcor", "altair"), None);
        let bad = "nodes: {a: {role: homebase}, b: {role: work}}\nedges: {a: {b: ";
        assert!(EdgeList::parse(&format!("{bad}{{network: lan, key: admin}}}}}}\n")).is_err(), "no admin edge");
        assert!(EdgeList::parse(&format!("{bad}{{key: service}}}}}}\n")).is_err(), "the map needs its network");
        assert!(EdgeList::parse(&format!("{bad}{{network: lan, kee: service}}}}}}\n")).is_err(), "a misspelt key is refused");
    }
}
