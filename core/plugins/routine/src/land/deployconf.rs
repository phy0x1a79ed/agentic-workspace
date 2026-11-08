//! A project's deploy config: `deploy.toml` at its repo root (routines.md R27, R31).
//!
//! The land batch reads it from the tree it is about to land, so a change to the check lands
//! with the code that needs it. It names no node: the install config holds the fleet.

use toml::Value;

pub const FILE: &str = "deploy.toml";
pub const CHECK_BUDGET: f64 = 60.0;
pub const REFRESH_BUDGET: f64 = 1800.0;

#[derive(Debug, thiserror::Error, PartialEq)]
#[error("{0}")]
pub struct ConfigError(pub String);

/// What each node runs in its checkout after a land: `sync` with `AWM_REFRESH_TO` set, then
/// `command <release sha>`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Refresh {
    pub sync: String,
    pub command: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DeployConfig {
    pub check: String,
    pub check_timeout: f64,
    /// A remote of the canonical bare repo that takes `release` after each promote,
    /// fast-forward only (install.md R114).
    pub backup: String,
    pub refresh: Refresh,
    /// Pin files, each naming a built file in this node's artifact store that the rollout
    /// pushes to each push node (install.md R115 item 4).
    pub artifact_pins: Vec<String>,
    /// A command that rebuilds every stale pin in the batch's tree.
    pub artifact_refresh: String,
    pub artifact_refresh_timeout: f64,
}

impl Default for DeployConfig {
    fn default() -> Self {
        Self {
            check: String::new(),
            check_timeout: CHECK_BUDGET,
            backup: String::new(),
            refresh: Refresh::default(),
            artifact_pins: vec![],
            artifact_refresh: String::new(),
            artifact_refresh_timeout: REFRESH_BUDGET,
        }
    }
}

const SECTIONS: [(&str, &[&str]); 4] = [
    ("deploy", &["check", "check_timeout"]),
    ("promote", &["backup"]),
    ("refresh", &["command", "sync"]),
    ("artifacts", &["pins", "refresh", "refresh_timeout"]),
];

/// A Python list repr of sorted names: `['a', 'b']`.
pub fn py_list(mut names: Vec<&str>) -> String {
    names.sort();
    format!("[{}]", names.iter().map(|n| format!("'{n}'")).collect::<Vec<_>>().join(", "))
}

fn text(t: Option<&toml::Table>, key: &str, what: &str) -> Result<String, ConfigError> {
    match t.and_then(|t| t.get(key)) {
        None => Ok(String::new()),
        Some(Value::String(s)) => Ok(s.clone()),
        Some(_) => Err(ConfigError(format!("{FILE}: {what} must be a string"))),
    }
}

fn number(t: Option<&toml::Table>, key: &str, default: f64, what: &str) -> Result<f64, ConfigError> {
    match t.and_then(|t| t.get(key)) {
        None => Ok(default),
        Some(Value::Integer(i)) => Ok(*i as f64),
        Some(Value::Float(f)) => Ok(*f),
        Some(Value::String(s)) => s.trim().parse().map_err(|_| ConfigError(format!("{FILE}: {what} must be a number"))),
        Some(_) => Err(ConfigError(format!("{FILE}: {what} must be a number"))),
    }
}

pub fn parse(src: &str) -> Result<DeployConfig, ConfigError> {
    let raw: toml::Table = src.parse().map_err(|e| ConfigError(format!("{FILE}: {e}")))?;
    for (sec, body) in &raw {
        if sec == "fleet" {
            return Err(ConfigError(format!("{FILE}: [fleet] belongs in the node's install config")));
        }
        let Some((_, keys)) = SECTIONS.iter().find(|(s, _)| s == sec) else {
            return Err(ConfigError(format!("{FILE}: unknown section [{sec}]")));
        };
        let Some(body) = body.as_table() else {
            return Err(ConfigError(format!("{FILE}: [{sec}] must be a table")));
        };
        let extra: Vec<&str> = body.keys().map(String::as_str).filter(|k| !keys.contains(k)).collect();
        if !extra.is_empty() {
            return Err(ConfigError(format!("{FILE}: unknown keys in [{sec}]: {}", py_list(extra))));
        }
    }
    let sec = |name: &str| raw.get(name).and_then(Value::as_table);
    let (d, r, a) = (sec("deploy"), sec("refresh"), sec("artifacts"));
    let pins = match a.and_then(|a| a.get("pins")) {
        None => vec![],
        Some(Value::Array(items)) => {
            let pins: Vec<String> = items.iter().filter_map(|p| p.as_str().filter(|p| !p.is_empty()).map(str::to_string)).collect();
            if pins.len() != items.len() {
                return Err(ConfigError(format!("{FILE}: [artifacts] pins must be a list of paths")));
            }
            pins
        }
        Some(_) => return Err(ConfigError(format!("{FILE}: [artifacts] pins must be a list of paths"))),
    };
    let artifact_refresh = match a.and_then(|a| a.get("refresh")) {
        None => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(_) => return Err(ConfigError(format!("{FILE}: [artifacts] refresh must be a command"))),
    };
    if !artifact_refresh.is_empty() && pins.is_empty() {
        return Err(ConfigError(format!("{FILE}: [artifacts] refresh needs pins to commit")));
    }
    Ok(DeployConfig {
        check: text(d, "check", "[deploy] check")?,
        check_timeout: number(d, "check_timeout", CHECK_BUDGET, "[deploy] check_timeout")?,
        backup: text(sec("promote"), "backup", "[promote] backup")?,
        refresh: Refresh { sync: text(r, "sync", "[refresh] sync")?, command: text(r, "command", "[refresh] command")? },
        artifact_pins: pins,
        artifact_refresh,
        artifact_refresh_timeout: number(a, "refresh_timeout", REFRESH_BUDGET, "[artifacts] refresh_timeout")?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects() {
        for (text, msg) in [
            ("[refresh]\nkind = 'restart'", "unknown keys"),
            ("[fixer]\npeer = 'x'", "unknown section"),
            ("[fleet]\nnodes = ['a']", "install config"),
            ("[deploy]\nnode = 'x'", "unknown keys"),
            ("[mesh]\n", "unknown section"),
            ("[artifacts]\npins = \"a/b.pin\"", "list of paths"),
            ("[artifacts]\nrefresh = \"x\"", "needs pins"),
        ] {
            let e = parse(text).unwrap_err();
            assert!(e.0.contains(msg), "{text:?}: {e}");
        }
        assert_eq!(parse("[deploy]\nnode = 'x'\nzz = 1").unwrap_err().0, "deploy.toml: unknown keys in [deploy]: ['node', 'zz']");
    }

    #[test]
    fn reads_refresh_and_pins() {
        let r = parse("[refresh]\nsync = \"s\"\ncommand = \"c\"\n").unwrap().refresh;
        assert_eq!((r.sync.as_str(), r.command.as_str()), ("s", "c"));
        assert_eq!(parse("[artifacts]\npins = [\"a/b.pin\"]").unwrap().artifact_pins, ["a/b.pin"]);
        let d = parse("").unwrap();
        assert_eq!((d.check_timeout, d.artifact_refresh_timeout), (CHECK_BUDGET, REFRESH_BUDGET));
    }
}
