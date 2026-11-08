//! A DVC scope resolved to the exact set of cache objects it depends on, for the
//! transfers. Pure local filesystem reasoning.
//!
//! A directory pin names a `.dir` manifest, not the files under it, and on a cold cache
//! the leaf ids are unknowable until the manifest is in hand. [`resolve`] reports those
//! manifests as `unresolved`, which is what makes a pull run in two transfers.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use crate::odb::{self, Manifest, DIR_SUFFIX};
use crate::pin::Pin;

#[derive(Debug, thiserror::Error)]
pub enum ScopeError {
    #[error("not a DVC scope (no .dvc/): {0}")]
    NotDvc(PathBuf),
    #[error("cache dir does not exist: {0}")]
    NoCache(PathBuf),
    #[error("cache {cache} is outside {root}; chinook paths are addressed workspace-relative and cannot name it")]
    Outside { cache: PathBuf, root: PathBuf },
}

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Resolution {
    /// Every object id the scope needs, as far as is knowable now.
    pub objects: BTreeSet<String>,
    /// The `.dir` ids among the top-level pins.
    pub manifests: BTreeSet<String>,
    /// Manifests absent here. Their leaves are not in `objects`.
    pub unresolved: BTreeSet<String>,
    pub present: BTreeSet<String>,
    pub missing: BTreeSet<String>,
    pub pins: usize,
}

/// One `[section] key` of a DVC config file, unquoted.
pub fn config_value(text: &str, section: &str, key: &str) -> Option<String> {
    let mut current = None::<String>;
    let mut found = None;
    for raw in text.lines() {
        let line = raw.trim();
        if line.starts_with('[') && line.ends_with(']') {
            current = Some(line[1..line.len() - 1].trim().trim_matches('"').to_string());
        } else if current.as_deref() == Some(section) {
            let (k, v) = line.split_once('=').unwrap_or((line, ""));
            if k.trim() == key {
                found = Some(v.trim().trim_matches('"').to_string());
            }
        }
    }
    found.filter(|v| !v.is_empty())
}

fn config_layers(scope: &Path, section: &str, key: &str) -> Option<String> {
    ["config.local", "config"]
        .iter()
        .filter_map(|name| std::fs::read_to_string(scope.join(".dvc").join(name)).ok())
        .filter_map(|text| config_value(&text, section, key))
        .next()
}

/// A scope's cache dir. `config.local` wins over `config`, as in DVC.
pub fn cache_dir_for(scope: &Path) -> PathBuf {
    match config_layers(scope, "cache", "dir") {
        Some(found) if Path::new(&found).is_absolute() => PathBuf::from(found),
        Some(found) => std::fs::canonicalize(scope.join(".dvc").join(&found)).unwrap_or_else(|_| crate::pin::normalize(&scope.join(".dvc").join(found))),
        None => scope.join(".dvc").join("cache"),
    }
}

/// The link types a checkout tries, in order. DVC's default is `reflink,copy`.
pub fn cache_types(scope: &Path) -> Vec<String> {
    config_layers(scope, "cache", "type")
        .unwrap_or_else(|| "reflink,copy".into())
        .split(',')
        .map(|t| t.trim().to_string())
        .filter(|t| !t.is_empty())
        .collect()
}

/// Every `*.dvc` pin under the scope, never entering a symlinked dir. A `.dvc` path
/// component excludes the config dir, which shares the extension but holds no pins.
pub fn find_pins(scope: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let mut stack = vec![scope.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&dir) else { continue };
        for d in rd.flatten() {
            let Ok(ft) = d.file_type() else { continue };
            let p = d.path();
            let name = d.file_name();
            if ft.is_dir() {
                if name != ".dvc" {
                    stack.push(p);
                }
            } else if ft.is_file() && name.to_string_lossy().ends_with(".dvc") {
                out.push(p);
            }
        }
    }
    out.sort();
    out
}

/// The top-level object ids a pin's outputs declare. An unparseable pin counts as none.
pub fn pin_hashes(pin: &Path) -> Vec<String> {
    match Pin::load(pin) {
        Ok(p) => p.outs.into_iter().filter_map(|o| o.md5).collect(),
        Err(e) => {
            eprintln!("skipping unparseable pin {}: {e}", pin.display());
            vec![]
        }
    }
}

/// The leaf ids inside a manifest present here.
pub fn expand_manifest(oid: &str, cache: &Path) -> Vec<String> {
    match Manifest::load(cache, oid) {
        Ok(m) => m.entries.into_iter().map(|e| e.md5).collect(),
        Err(e) => {
            eprintln!("cannot read manifest {oid}: {e}");
            vec![]
        }
    }
}

pub fn resolve(scope: &Path, cache: &Path) -> Resolution {
    let pins = find_pins(scope);
    let tops: BTreeSet<String> = pins.iter().flat_map(|p| pin_hashes(p)).collect();
    let mut res = Resolution { objects: tops.clone(), pins: pins.len(), ..Default::default() };
    res.manifests = tops.iter().filter(|h| h.ends_with(DIR_SUFFIX)).cloned().collect();
    for h in res.manifests.clone() {
        if odb::object_path(cache, &h).exists() {
            res.objects.extend(expand_manifest(&h, cache));
        } else {
            res.unresolved.insert(h);
        }
    }
    for h in &res.objects {
        if odb::object_path(cache, h).exists() {
            res.present.insert(h.clone());
        } else {
            res.missing.insert(h.clone());
        }
    }
    res
}

/// Validate a scope path and return `(scope, cache)`. A cache outside the workspace has
/// no workspace-relative name on the collection, so it is refused.
pub fn open_scope(scope_path: &Path, workspace_root: &Path) -> Result<(PathBuf, PathBuf), ScopeError> {
    let scope = if scope_path.is_absolute() { scope_path.to_path_buf() } else { workspace_root.join(scope_path) };
    let scope = std::fs::canonicalize(&scope).unwrap_or(scope);
    if !scope.join(".dvc").is_dir() {
        return Err(ScopeError::NotDvc(scope));
    }
    let cache = cache_dir_for(&scope);
    if !cache.is_dir() {
        return Err(ScopeError::NoCache(cache));
    }
    let root = std::fs::canonicalize(workspace_root).unwrap_or_else(|_| workspace_root.to_path_buf());
    let cache_abs = std::fs::canonicalize(&cache).unwrap_or(cache);
    if !cache_abs.starts_with(&root) {
        return Err(ScopeError::Outside { cache: cache_abs, root });
    }
    Ok((scope, cache_abs))
}
