//! The `[account.*]` and `[bucket.*]` file helpers both plugins read their toml with: a secret is
//! inline or a `<key>_file`, relative to the file's folder, with all whitespace stripped.

use std::path::{Path, PathBuf};

use toml::{Table, Value};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ConfigError {}

pub type R<T> = Result<T, ConfigError>;

pub fn err<T>(msg: impl Into<String>) -> R<T> {
    Err(ConfigError(msg.into()))
}

pub fn parse(path: &Path) -> R<Option<Table>> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return err(format!("could not parse {}: {e}", path.display())),
    };
    text.parse::<Table>().map(Some).map_err(|e| ConfigError(format!("could not parse {}: {e}", path.display())))
}

/// A secret from an inline value or a `<label>_file`.
pub fn secret(section: &Table, key: &str, cfg: &Path, label: &str, required: bool) -> R<Option<String>> {
    let p = cfg.display();
    if let Some(inline) = section.get(key) {
        return match inline.as_str().map(str::trim) {
            Some(s) if !s.is_empty() => Ok(Some(s.to_string())),
            _ => err(format!("{p}: {label} must be a non-empty string")),
        };
    }
    if let Some(file) = section.get(&format!("{key}_file")) {
        let Some(f) = file.as_str().map(str::trim).filter(|s| !s.is_empty()) else {
            return err(format!("{p}: {label}_file must be a path string"));
        };
        let mut file = PathBuf::from(f);
        if !file.is_absolute() {
            file = cfg.parent().unwrap_or(Path::new(".")).join(file);
        }
        let raw = std::fs::read_to_string(&file).map_err(|e| ConfigError(format!("{p}: {label}_file {} could not be read: {e}", file.display())))?;
        let s: String = raw.split_whitespace().collect();
        if s.is_empty() {
            return err(format!("{p}: {label}_file {} is empty", file.display()));
        }
        return Ok(Some(s));
    }
    if required {
        return err(format!("{p}: {label} (or {label}_file) is required"));
    }
    Ok(None)
}

pub fn opt_str(section: &Table, key: &str, cfg: &Path, label: &str) -> R<Option<String>> {
    match section.get(key) {
        None => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.trim().to_string())),
        Some(_) => err(format!("{}: {label}.{key} must be a string when present", cfg.display())),
    }
}

pub fn opt_bool(section: &Table, key: &str, default: bool, cfg: &Path, label: &str) -> R<bool> {
    match section.get(key) {
        None => Ok(default),
        Some(Value::Boolean(b)) => Ok(*b),
        Some(_) => err(format!("{}: {label}.{key} must be a boolean", cfg.display())),
    }
}

pub fn tables<'a>(data: &'a Table, key: &str, cfg: &Path, what: &str) -> R<Vec<(&'a String, &'a Table)>> {
    let Some(v) = data.get(key) else { return Ok(vec![]) };
    let Some(t) = v.as_table() else {
        return err(format!("{}: [{key}] must be a table of named {what}", cfg.display()));
    };
    t.iter()
        .map(|(name, s)| s.as_table().map(|s| (name, s)).ok_or_else(|| ConfigError(format!("{}: [{key}.{name}] must be a table", cfg.display()))))
        .collect()
}

