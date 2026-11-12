//! A project's deploy config: `deploy.toml` at its repo root (routines.md R27, R31).
//!
//! Rollout reads it from the release it deploys. It names no node: the install config holds
//! the fleet.

use toml::Value;

pub const FILE: &str = "deploy.toml";

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

pub fn sorted_names(mut names: Vec<&str>) -> String {
    names.sort();
    names.join(", ")
}

fn text(t: Option<&toml::Table>, key: &str, what: &str) -> Result<String, String> {
    match t.and_then(|t| t.get(key)) {
        None => Ok(String::new()),
        Some(Value::String(s)) => Ok(s.clone()),
        Some(_) => Err(format!("{FILE}: {what} must be a string")),
    }
}

pub fn parse(src: &str) -> Result<DeployConfig, String> {
    let raw: toml::Table = src.parse().map_err(|e| format!("{FILE}: {e}"))?;
    for (sec, body) in &raw {
        if sec == "fleet" {
            return Err(format!("{FILE}: [fleet] belongs in the node's install config"));
        }
        let Some((_, keys)) = SECTIONS.iter().find(|(s, _)| s == sec) else {
            return Err(format!("{FILE}: unknown section [{sec}]"));
        };
        let Some(body) = body.as_table() else {
            return Err(format!("{FILE}: [{sec}] must be a table"));
        };
        let extra: Vec<&str> = body.keys().map(String::as_str).filter(|k| !keys.contains(k)).collect();
        if !extra.is_empty() {
            return Err(format!("{FILE}: unknown keys in [{sec}]: {}", sorted_names(extra)));
        }
    }
    let sec = |name: &str| raw.get(name).and_then(Value::as_table);
    let (r, a) = (sec("refresh"), sec("artifacts"));
    let pins = match a.and_then(|a| a.get("pins")) {
        None => vec![],
        Some(Value::Array(items)) => {
            let pins: Vec<String> = items.iter().filter_map(|p| p.as_str().filter(|p| !p.is_empty()).map(str::to_string)).collect();
            if pins.len() != items.len() {
                return Err(format!("{FILE}: [artifacts] pins must be a list of paths"));
            }
            pins
        }
        Some(_) => return Err(format!("{FILE}: [artifacts] pins must be a list of paths")),
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
            ("[other]\n", "unknown section"),
            ("[fleet]\nnodes = ['a']", "install config"),
            ("[artifacts]\npins = \"a/b.pin\"", "list of paths"),
        ] {
            let e = parse(text).unwrap_err();
            assert!(e.contains(msg), "{text:?}: {e}");
        }
        assert_eq!(parse("[refresh]\nnode = 'x'\nzz = 1").unwrap_err(), "deploy.toml: unknown keys in [refresh]: node, zz");
    }

}
