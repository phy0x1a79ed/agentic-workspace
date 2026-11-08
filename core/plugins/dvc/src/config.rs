//! The chinook endpoint config, `<AWM_DIR>/dvc.toml`. Every value is node-specific: the
//! Globus endpoint that fronts this machine and the prefix its backups live under. `prefix`
//! is load-bearing both ways: `sync` writes the cache there and `pull` reads it back.
//! `AWM_DVC_LOCAL_ENDPOINT`, `AWM_DVC_REMOTE_ENDPOINT` and `AWM_DVC_PREFIX` override the file.

use std::path::{Path, PathBuf};

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct DvcConfigError(pub String);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChinookConfig {
    pub local_endpoint: String,
    pub remote_endpoint: String,
    pub prefix: String,
}

pub fn config_file(awm_dir: &Path) -> PathBuf {
    awm_dir.join("dvc.toml")
}

pub fn load(awm_dir: &Path) -> Result<ChinookConfig, DvcConfigError> {
    let file = config_file(awm_dir);
    let table: toml::Table = match std::fs::read_to_string(&file) {
        Ok(text) => text.parse().map_err(|e| DvcConfigError(format!("could not parse {}: {e}", file.display())))?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => toml::Table::new(),
        Err(e) => return Err(DvcConfigError(format!("could not parse {}: {e}", file.display()))),
    };
    let section = table.get("chinook").and_then(toml::Value::as_table).cloned().unwrap_or_default();
    let field = |key: &str, env: &str| -> String {
        if let Some(v) = std::env::var(env).ok().filter(|v| !v.is_empty()) {
            return v.trim().to_string();
        }
        section.get(key).and_then(toml::Value::as_str).map(|s| s.trim().to_string()).unwrap_or_default()
    };
    let local = field("local_endpoint", "AWM_DVC_LOCAL_ENDPOINT");
    let remote = field("remote_endpoint", "AWM_DVC_REMOTE_ENDPOINT");
    let prefix = field("prefix", "AWM_DVC_PREFIX");
    let missing: Vec<&str> = [("local_endpoint", &local), ("remote_endpoint", &remote), ("prefix", &prefix)]
        .iter()
        .filter(|(_, v)| v.is_empty())
        .map(|(k, _)| *k)
        .collect();
    if !missing.is_empty() {
        return Err(DvcConfigError(format!(
            "{} [chinook] is missing {} — these are node-specific and have no safe default (see INSTALL.md).",
            file.display(),
            missing.join(", ")
        )));
    }
    Ok(ChinookConfig { local_endpoint: local, remote_endpoint: remote, prefix: prefix.trim_end_matches('/').to_string() })
}
