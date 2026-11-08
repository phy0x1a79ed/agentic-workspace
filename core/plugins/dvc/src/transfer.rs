//! Hash-selective pull and push of one scope's cache objects against chinook: the inverse
//! of the whole-cache sync. A cold restore needs the `.dir` manifests before their leaves
//! can be named, so `pull` advances one phase per call: same call, no state, safe to repeat.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use awm_dvcdata::odb::object_relpath;
use awm_dvcdata::provision;
use awm_dvcdata::scope::{self, ScopeError};
use awm_plugin_sdk::VerbError;
use serde_json::{json, Value};

use crate::config::{self, ChinookConfig};
use crate::globus::{self, Api, Submit};
use crate::Paths;

pub fn scope_err(m: impl Into<String>) -> VerbError {
    VerbError::new("DvcScopeError", m)
}

pub fn config_err(e: config::DvcConfigError) -> VerbError {
    VerbError::new("DvcConfigError", e.0)
}

pub fn globus_err(e: globus::GlobusError) -> VerbError {
    VerbError::new("GlobusError", e.0)
}

fn open(paths: &Paths, scope_path: &str) -> Result<(PathBuf, PathBuf), VerbError> {
    scope::open_scope(Path::new(scope_path), &paths.workspace).map_err(|e: ScopeError| scope_err(e.to_string()))
}

/// One transfer item per object, by explicit path. Never filter rules: they match a name at
/// any depth and cannot say "this exact object".
pub fn transfer_items(paths: &Paths, hashes: &BTreeSet<String>, cache: &Path, cfg: &ChinookConfig, pull: bool) -> Vec<Value> {
    let rel_cache = cache.strip_prefix(&paths.workspace).unwrap_or(cache).to_string_lossy().into_owned();
    hashes
        .iter()
        .map(|h| {
            let rel = format!("{rel_cache}/{}", object_relpath(h));
            let local = paths.workspace.join(&rel).to_string_lossy().into_owned();
            let remote = format!("{}/{rel}", cfg.prefix);
            let (src, dst) = if pull { (remote, local) } else { (local, remote) };
            json!({"DATA_TYPE": "transfer_item", "source_path": src, "destination_path": dst, "recursive": false})
        })
        .collect()
}

fn submit(api: &dyn Api, cfg: &ChinookConfig, items: &[Value], pull: bool, label: &str) -> Result<String, VerbError> {
    let (src, dst) = if pull { (&cfg.remote_endpoint, &cfg.local_endpoint) } else { (&cfg.local_endpoint, &cfg.remote_endpoint) };
    let opts = Submit {
        source: src,
        destination: dst,
        label,
        sync_level: 1,
        // Never: chinook is a remote, not a mirror, and a restore least of all gets to delete.
        delete_destination_extra: false,
        skip_source_errors: false,
        filter_rules: None,
    };
    globus::submit(api, items, &opts).map_err(globus_err)
}

fn name_of(p: &Path) -> String {
    p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

/// A scope's pin and object state against the local cache. No network.
pub fn status(paths: &Paths, scope_path: &str) -> Result<Value, VerbError> {
    let (scope, cache) = open(paths, scope_path)?;
    let res = scope::resolve(&scope, &cache);
    let mut out = json!({
        "scope": scope.to_string_lossy(),
        "cache": cache.to_string_lossy(),
        "pins": res.pins,
        "manifests": res.manifests.len(),
        "unresolved_manifests": res.unresolved.len(),
        "objects_known": res.objects.len(),
        "present": res.present.len(),
        "missing": res.missing.len(),
        "complete": res.missing.is_empty() && res.unresolved.is_empty(),
    });
    if !res.unresolved.is_empty() {
        out["note"] = json!(format!(
            "{} manifest(s) absent locally — their leaves are not counted above. pull fetches manifests first, then the leaves.",
            res.unresolved.len()
        ));
    }
    Ok(out)
}

/// The explicit object set a scope depends on. No network.
pub fn resolve(paths: &Paths, scope_path: &str) -> Result<Value, VerbError> {
    let (scope, cache) = open(paths, scope_path)?;
    let res = scope::resolve(&scope, &cache);
    Ok(json!({
        "scope": scope.to_string_lossy(),
        "cache": cache.to_string_lossy(),
        "objects": res.objects,
        "present": res.present,
        "missing": res.missing,
        "unresolved_manifests": res.unresolved,
        "complete": res.unresolved.is_empty(),
    }))
}

fn open_remote(paths: &Paths, scope_path: &str) -> Result<(PathBuf, PathBuf), VerbError> {
    let (scope, cache) = open(paths, scope_path)?;
    if provision::no_remote(&scope) {
        let dir = provision::project_dir(&scope).map(|p| p.display().to_string()).unwrap_or_default();
        return Err(scope_err(format!("{dir} keeps no remote by decision (CACHEDIR.TAG): its data never leaves this machine")));
    }
    Ok((scope, cache))
}

fn with_items(mut v: Value, items: Vec<Value>) -> Value {
    v["dry_run"] = json!(true);
    v["items"] = json!(items);
    v
}

/// Fetch what a scope needs and lacks: the absent manifests first, then the leaves they name.
pub fn pull(paths: &Paths, api: &dyn Api, scope_path: &str, dry_run: bool) -> Result<Value, VerbError> {
    let cfg = config::load(&paths.awm_dir).map_err(config_err)?;
    let (scope, cache) = open_remote(paths, scope_path)?;
    let res = scope::resolve(&scope, &cache);
    let s = scope.to_string_lossy().into_owned();
    if !res.unresolved.is_empty() {
        let items = transfer_items(paths, &res.unresolved, &cache, &cfg, true);
        let mut out = json!({
            "scope": s,
            "phase": "manifests",
            "objects": items.len(),
            "note": "leaves are unknowable until these manifests are local — call pull again once this task completes to fetch them",
        });
        if dry_run {
            return Ok(with_items(out, items));
        }
        out["task_id"] = json!(submit(api, &cfg, &items, true, &format!("dvc pull manifests {}", name_of(&scope)))?);
        return Ok(out);
    }
    if res.missing.is_empty() {
        return Ok(json!({"scope": s, "phase": "complete", "objects": 0, "note": "nothing to pull — every object this scope pins is already local"}));
    }
    let items = transfer_items(paths, &res.missing, &cache, &cfg, true);
    let mut out = json!({
        "scope": s,
        "phase": "objects",
        "objects": items.len(),
        "note": "on completion, run `dvc checkout` in the scope to materialize",
    });
    if dry_run {
        return Ok(with_items(out, items));
    }
    out["task_id"] = json!(submit(api, &cfg, &items, true, &format!("dvc pull {}", name_of(&scope)))?);
    Ok(out)
}

/// Send the objects a scope pins up to chinook. Never deletes there.
pub fn push(paths: &Paths, api: &dyn Api, scope_path: &str, dry_run: bool) -> Result<Value, VerbError> {
    let cfg = config::load(&paths.awm_dir).map_err(config_err)?;
    let (scope, cache) = open_remote(paths, scope_path)?;
    let res = scope::resolve(&scope, &cache);
    if !res.unresolved.is_empty() {
        return Err(scope_err(format!("{} manifest(s) missing locally — cannot push what isn't here; pull first", res.unresolved.len())));
    }
    let s = scope.to_string_lossy().into_owned();
    if res.present.is_empty() {
        return Ok(json!({"scope": s, "objects": 0, "note": "nothing to push"}));
    }
    let items = transfer_items(paths, &res.present, &cache, &cfg, false);
    let mut out = json!({"scope": s, "objects": items.len(), "skipped_absent": res.missing.len()});
    if dry_run {
        return Ok(with_items(out, items));
    }
    out["task_id"] = json!(submit(api, &cfg, &items, false, &format!("dvc push {}", name_of(&scope)))?);
    Ok(out)
}
