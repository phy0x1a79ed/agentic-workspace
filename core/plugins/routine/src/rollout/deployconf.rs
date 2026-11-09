//! A project's deploy config: `deploy.toml` at its repo root (routines.md R27, R31).
//!
//! Rollout reads it from the release it deploys. It names no node: the install config holds
//! the fleet.

use toml::Value;

pub const FILE: &str = "deploy.toml";

#[derive(Debug, thiserror::Error, PartialEq)]
#[error("{0}")]
pub struct ConfigError(pub String);

/// What each node runs in its checkout: `sync` with `AWM_REFRESH_TO` set, then
/// `command <release sha>`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Refresh {
    pub sync: String,
    pub command: String,
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct DeployConfig {
    pub refresh: Refresh,
    /// Pin files, each naming a built file in this node's artifact store that the rollout
    /// pushes to each push node (install.md R115 item 4).
    pub artifact_pins: Vec<String>,
}

const SECTIONS: [(&str, &[&str]); 2] = [("refresh", &["command", "sync"]), ("artifacts", &["pins"])];

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
    let (r, a) = (sec("refresh"), sec("artifacts"));
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
    Ok(DeployConfig { refresh: Refresh { sync: text(r, "sync", "[refresh] sync")?, command: text(r, "command", "[refresh] command")? }, artifact_pins: pins })
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
            ("[mesh]\n", "unknown section"),
            ("[artifacts]\npins = \"a/b.pin\"", "list of paths"),
        ] {
            let e = parse(text).unwrap_err();
            assert!(e.0.contains(msg), "{text:?}: {e}");
        }
        assert_eq!(parse("[refresh]\nnode = 'x'\nzz = 1").unwrap_err().0, "deploy.toml: unknown keys in [refresh]: ['node', 'zz']");
    }

}
