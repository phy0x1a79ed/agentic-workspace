//! The fleet as this node's install config states it (fleet.md R140, routines.md R31).
//!
//! awm names no host. The cloud project writes `<AWM_DIR>/config.toml` per node, and its
//! `[fleet]` table says which node is home base and the order nodes refresh in:
//!
//! ```toml
//! [fleet]
//! home = "<node>"
//! nodes = ["<home>", "<node>", ...]
//! sleepers = ["<node>", ...]    # optional: nodes that may be asleep and are never woken
//!
//! [fleet.project.<name>]        # optional, for a project not landed at home
//! canonical = "<node>"
//! nodes = ["<canonical>", ...]
//!
//! [fleet.push.<node>]           # optional
//! url = "<node>:<workspace>/projects/{project}/.bare"   # with projects, for a node
//! projects = ["<name>", ...]                             # that cannot fetch from home
//! artifacts = "<node>:<awm dir>/artifacts"               # for a node that builds nothing
//! refresh = "<command>"                                  # optional, with url
//! ```
//!
//! The node that lands a project leads its rollout. Before a node refreshes, the landing node
//! pushes the new release into its repo as `refs/<landing node>/release`, fast-forward only,
//! when the node names a url, and the files the release pins when it names `artifacts`. A push
//! node needs no routine plugin: home refreshes it by running its `refresh` command, an ssh
//! into the node, with `AWM_REFRESH_NODE`, `AWM_REFRESH_PROJECT`, `AWM_REFRESH_FROM` and
//! `AWM_REFRESH_TO` set. A sleeper that does not answer is skipped and reported, and the
//! rollout goes on. An absent file or table is a fleet of this node alone.

use std::collections::BTreeMap;
use std::path::Path;

use toml::Value;

use super::deployconf::py_list;

pub const FILE: &str = "config.toml";

#[derive(Debug, thiserror::Error, PartialEq)]
#[error("{0}")]
pub struct FleetConfigError(pub String);

#[derive(Debug, Clone, Default, PartialEq)]
pub struct ProjectFleet {
    pub canonical: String,
    pub nodes: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Push {
    pub url: String,
    pub projects: Vec<String>,
    pub artifacts: String,
    pub refresh: String,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Fleet {
    pub home: String,
    pub nodes: Vec<String>,
    pub projects: BTreeMap<String, ProjectFleet>,
    pub push: BTreeMap<String, Push>,
    pub sleepers: Vec<String>,
}

impl Fleet {
    /// Where `node` takes `project`'s release by push. Empty means it fetches.
    pub fn push_url(&self, node: &str, project: &str) -> String {
        match self.push.get(node) {
            Some(p) if p.projects.iter().any(|x| x == project) => p.url.replace("{project}", project),
            _ => String::new(),
        }
    }

    /// Where `node` takes pinned artifacts by push. Empty means it takes none.
    pub fn artifacts_dest(&self, node: &str) -> String {
        self.push.get(node).map(|p| p.artifacts.clone()).unwrap_or_default()
    }

    /// The command home runs to refresh push node `node`.
    pub fn refresh_command(&self, node: &str) -> String {
        self.push.get(node).map(|p| p.refresh.clone()).unwrap_or_default()
    }

    /// The node that lands `project`. Empty means wherever the batch runs.
    pub fn canonical(&self, project: &str) -> String {
        match self.projects.get(project) {
            Some(p) if !p.canonical.is_empty() => p.canonical.clone(),
            _ => self.home.clone(),
        }
    }

    /// The nodes that refresh after `project` lands, in order.
    pub fn rollout(&self, project: &str) -> Vec<String> {
        match self.projects.get(project) {
            Some(p) if !p.nodes.is_empty() => p.nodes.clone(),
            _ => self.nodes.clone(),
        }
    }
}

fn nodes(raw: Option<&Value>, where_: &str, lead: &str) -> Result<Vec<String>, FleetConfigError> {
    let Some(raw) = raw else { return Ok(vec![]) };
    let list: Option<Vec<String>> = raw.as_array().and_then(|a| a.iter().map(|n| n.as_str().filter(|n| !n.is_empty()).map(str::to_string)).collect());
    let Some(list) = list else {
        return Err(FleetConfigError(format!("{where_} nodes must be a list of node names")));
    };
    let mut seen = std::collections::HashSet::new();
    if !list.iter().all(|n| seen.insert(n)) {
        return Err(FleetConfigError(format!("{where_} nodes lists a node twice")));
    }
    if !list.is_empty() && !lead.is_empty() && list[0] != lead {
        return Err(FleetConfigError(format!("{where_} nodes must start with '{lead}', the node that lands")));
    }
    Ok(list)
}

fn py_str(v: &Value) -> String {
    v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())
}

/// Read the `[fleet]` table. Every other key of the install config is the gateway's.
pub fn parse(text: &str, source: &str) -> Result<Fleet, FleetConfigError> {
    let doc: toml::Table = text.parse().map_err(|e| FleetConfigError(format!("{source}: {e}")))?;
    let empty = toml::Table::new();
    let raw = doc.get("fleet").and_then(Value::as_table).unwrap_or(&empty);
    let extra: Vec<&str> = raw.keys().map(String::as_str).filter(|k| !["home", "nodes", "project", "push", "sleepers"].contains(k)).collect();
    if !extra.is_empty() {
        return Err(FleetConfigError(format!("{source}: unknown keys in [fleet]: {}", py_list(extra))));
    }
    let home = raw.get("home").map(py_str).unwrap_or_default();
    let fleet_nodes = nodes(raw.get("nodes"), &format!("{source}: [fleet]"), &home)?;
    let sleepers = nodes(raw.get("sleepers"), &format!("{source}: [fleet] sleepers"), "")?;
    if sleepers.contains(&home) {
        return Err(FleetConfigError(format!("{source}: [fleet] sleepers names home, '{home}'")));
    }
    let mut projects = BTreeMap::new();
    for (name, body) in raw.get("project").and_then(Value::as_table).unwrap_or(&empty) {
        let where_ = format!("{source}: [fleet.project.{name}]");
        let Some(body) = body.as_table().filter(|b| b.keys().all(|k| k == "canonical" || k == "nodes")) else {
            return Err(FleetConfigError(format!("{where_} takes only canonical and nodes")));
        };
        let canonical = body.get("canonical").map(py_str).unwrap_or_default();
        let lead = if canonical.is_empty() { home.clone() } else { canonical.clone() };
        projects.insert(name.clone(), ProjectFleet { nodes: nodes(body.get("nodes"), &where_, &lead)?, canonical });
    }
    let mut push = BTreeMap::new();
    for (node, body) in raw.get("push").and_then(Value::as_table).unwrap_or(&empty) {
        let where_ = format!("{source}: [fleet.push.{node}]");
        let keys: Vec<&str> = body.as_table().map(|b| b.keys().map(String::as_str).collect()).unwrap_or_default();
        let has = |k: &str| keys.contains(&k);
        let shape_ok = body.is_table()
            && keys.iter().all(|k| ["url", "projects", "artifacts", "refresh"].contains(k))
            && ((has("url") && has("projects")) || keys == ["artifacts"]);
        if !shape_ok {
            return Err(FleetConfigError(format!("{where_} takes url and projects, and optionally refresh and artifacts, or artifacts alone")));
        }
        let b = body.as_table().unwrap_or(&empty);
        let artifacts = match b.get("artifacts") {
            None => String::new(),
            Some(Value::String(s)) if !s.is_empty() => s.clone(),
            Some(_) => return Err(FleetConfigError(format!("{where_} artifacts must be a directory as <host>:<path>"))),
        };
        let refresh = match b.get("refresh") {
            None => String::new(),
            Some(Value::String(s)) if !s.trim().is_empty() => s.clone(),
            Some(_) => return Err(FleetConfigError(format!("{where_} refresh must be a command"))),
        };
        let url = match b.get("url") {
            None => String::new(),
            Some(Value::String(s)) if s.contains("{project}") => s.clone(),
            Some(_) => return Err(FleetConfigError(format!("{where_} url must name the repo with {{project}}"))),
        };
        let names: Option<Vec<String>> = match b.get("projects") {
            None => Some(vec![]),
            Some(Value::Array(a)) => a.iter().map(|n| n.as_str().filter(|n| !n.is_empty()).map(str::to_string)).collect(),
            Some(_) => None,
        };
        let Some(names) = names else {
            return Err(FleetConfigError(format!("{where_} projects must be a list of project names")));
        };
        push.insert(node.clone(), Push { url, projects: names, artifacts, refresh });
    }
    Ok(Fleet { home, nodes: fleet_nodes, projects, push, sleepers })
}

pub fn load(path: &Path) -> Result<Fleet, FleetConfigError> {
    if !path.is_file() {
        return Ok(Fleet::default());
    }
    let text = std::fs::read_to_string(path).map_err(|e| FleetConfigError(format!("{}: {e}", path.display())))?;
    parse(&text, &path.display().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    const INSTALL: &str = "plugins = [\"*\"]\n\n[fleet]\nhome = \"mizar\"\nnodes = [\"mizar\", \"alcor\", \"capella\"]\n";

    fn p(text: &str) -> Result<Fleet, FleetConfigError> {
        parse(text, FILE)
    }

    #[test]
    fn reads_home_and_overrides() {
        let f = p(&format!("{INSTALL}[fleet.project.engcyc]\ncanonical = \"altair\"\nnodes = [\"altair\"]\n")).unwrap();
        assert_eq!(f.canonical("awm"), "mizar");
        assert_eq!(f.rollout("awm"), ["mizar", "alcor", "capella"]);
        assert_eq!((f.canonical("engcyc"), f.rollout("engcyc")), ("altair".to_string(), vec!["altair".to_string()]));
        let none = load(Path::new("/nonexistent/config.toml")).unwrap();
        assert_eq!((none.canonical("awm"), none.rollout("awm")), (String::new(), vec![]));
    }

    #[test]
    fn rejects() {
        for (text, msg) in [
            ("[fleet]\nhome = \"a\"\nnodes = [\"b\", \"a\"]", "must start with 'a'"),
            ("[fleet]\nnodes = [\"a\", \"a\"]", "twice"),
            ("[fleet]\nhomebase = \"a\"", "unknown keys"),
            ("[fleet.project.p]\ncanonical = \"b\"\nnodes = [\"a\", \"b\"]", "must start with 'b'"),
            ("[fleet.project.p]\npeer = \"b\"", "only canonical and nodes"),
            ("[fleet.push.a]\nurl = \"a:/x\"\nprojects = [\"p\"]\nrefresh = \"true\"", "with {project}"),
            ("[fleet.push.a]\nurl = \"a:/{project}\"\nrefresh = \"true\"", "url and projects"),
            ("[fleet.push.a]\nurl = \"a:/{project}\"\nprojects = \"p\"\nrefresh = \"true\"", "list of project names"),
            ("[fleet]\nhome = \"mizar\"\nsleepers = [\"mizar\"]\n", "names home"),
            ("[fleet.push.a]\nprojects = [\"p\"]\nartifacts = \"a:/x\"", "url and projects"),
            ("[fleet.push.a]\nartifacts = \"\"", "artifacts must be"),
            ("[fleet.push.a]\n", "url and projects"),
        ] {
            let e = p(text).unwrap_err();
            assert!(e.0.contains(msg), "{text:?}: {e}");
        }
    }

    #[test]
    fn push_and_artifacts() {
        let push = format!("{INSTALL}\n[fleet.push.alcor]\nurl = \"altair:/w/projects/{{project}}/.bare\"\nprojects = [\"proj\"]\nrefresh = \"true\"\n");
        let f = p(&push).unwrap();
        assert_eq!(f.push_url("alcor", "proj"), "altair:/w/projects/proj/.bare");
        assert_eq!((f.push_url("alcor", "other"), f.push_url("capella", "proj")), (String::new(), String::new()));
        let f = p("[fleet.push.a]\nurl = \"a:/{project}\"\nprojects = [\"p\"]\n").unwrap();
        assert_eq!((f.push_url("a", "p"), f.refresh_command("a")), ("a:/p".to_string(), String::new()));
        let f = p(&format!("{INSTALL}\n[fleet.push.alcor]\nartifacts = \"alcor:/w/.awm/artifacts\"\n")).unwrap();
        assert_eq!((f.artifacts_dest("alcor"), f.push_url("alcor", "proj")), ("alcor:/w/.awm/artifacts".to_string(), String::new()));
        let f = p(&format!("{push}artifacts = \"d:/w/.awm/artifacts\"\n")).unwrap();
        assert_eq!((f.artifacts_dest("alcor"), f.artifacts_dest("capella")), ("d:/w/.awm/artifacts".to_string(), String::new()));
        let f = p("[fleet]\nhome = \"mizar\"\nnodes = [\"mizar\", \"capella\"]\nsleepers = [\"capella\"]\n").unwrap();
        assert_eq!(f.sleepers, ["capella"]);
    }
}
