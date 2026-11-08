//! The full-workspace mirror to chinook: the nightly backup of everything else.
//!
//! It runs with `delete_destination_extra`, so a local deletion propagates. It excludes
//! every DVC checkout and shared-chunk store, which are hardlinks into the cache that the
//! archive (`sync`) carries. That exclusion is only safe while the archive also runs.
//! Every destination lies under `<prefix>/workspace/`, a sibling of the archive, so the one
//! transfer that deletes cannot reach it.
//!
//! The tree is partitioned by hand: Globus filter rules match a name at any depth, and DVC
//! output names collide with real content. Only the regenerable directory names, which
//! collide with nothing, go to Globus as filters. Symlinks are skipped: following one drags
//! the excluded checkouts back in, and naming a link to a directory wedges the whole task.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Component, Path, PathBuf};

use serde_json::{json, Value};

use awm_dvcdata::cachetag::{is_cache_tag, CACHEDIR_TAG};

use crate::config::ChinookConfig;
use crate::globus::{self, Api, Submit};
use crate::Paths;

pub const BACKUP_LABEL: &str = "agentic_workspace daily";
pub const DEST_SUBDIR: &str = "workspace";

/// Directory names rebuilt from what is already backed up. Each must never name real
/// content. `dist/` is absent on purpose: a live server serves those bundles.
pub const REGENERABLE_DIRS: [&str; 5] = [".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__", "node_modules"];

pub fn filter_rules() -> Vec<Value> {
    REGENERABLE_DIRS.iter().map(|n| json!({"DATA_TYPE": "filter_rule", "method": "exclude", "type": "dir", "name": n})).collect()
}

/// `os.path.normpath`, lexically.
pub fn normpath(p: &Path) -> PathBuf {
    let mut out: Vec<Component> = vec![];
    for c in p.components() {
        match c {
            Component::CurDir => {}
            Component::ParentDir => match out.last() {
                Some(Component::Normal(_)) => {
                    out.pop();
                }
                Some(Component::RootDir) => {}
                _ => out.push(c),
            },
            other => out.push(other),
        }
    }
    if out.is_empty() {
        return PathBuf::from(".");
    }
    out.iter().collect()
}

fn read_dir_sorted(dir: &Path) -> Option<Vec<fs::DirEntry>> {
    let mut v: Vec<fs::DirEntry> = fs::read_dir(dir).ok()?.filter_map(Result::ok).collect();
    v.sort_by_key(|e| e.file_name());
    Some(v)
}

/// Every `*.dvc` file under `root`, as Python's `rglob` finds them: symlinked directories are
/// not followed.
pub fn find_dvc_files(root: &Path) -> Vec<PathBuf> {
    let mut out = vec![];
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let Some(entries) = read_dir_sorted(&dir) else { continue };
        for e in entries {
            let Ok(ft) = e.file_type() else { continue };
            let p = e.path();
            if ft.is_dir() {
                stack.push(p.clone());
            }
            let name = e.file_name();
            let is_pin = name.to_string_lossy().ends_with(".dvc");
            if is_pin && p.is_file() {
                out.push(p);
            }
        }
    }
    out
}

/// Absolute paths of every DVC-tracked output under `root`, every out of every pin.
pub fn dvc_output_paths(root: &Path) -> Vec<String> {
    let mut out = BTreeSet::new();
    for pin in find_dvc_files(root) {
        let doc: serde_yaml::Value = match fs::read_to_string(&pin).map_err(|e| e.to_string()).and_then(|t| serde_yaml::from_str(&t).map_err(|e| e.to_string())) {
            Ok(d) => d,
            Err(e) => {
                tracing::warn!("skipping unparseable pin {}: {e}", pin.display());
                continue;
            }
        };
        let Some(outs) = doc.get("outs").and_then(serde_yaml::Value::as_sequence) else { continue };
        for o in outs {
            let Some(p) = o.get("path") else { continue };
            let p = match p {
                serde_yaml::Value::String(s) if !s.is_empty() => s.clone(),
                serde_yaml::Value::Number(n) => n.to_string(),
                serde_yaml::Value::Bool(b) => if *b { "True" } else { "False" }.to_string(),
                _ => continue,
            };
            let base = pin.parent().unwrap_or(Path::new("/"));
            out.insert(normpath(&base.join(p)).to_string_lossy().into_owned());
        }
    }
    out.into_iter().collect()
}

/// Each project's shared-chunk store, `projects/<p>/data`. No pin names it.
pub fn shared_store_paths(root: &Path) -> Vec<String> {
    let mut out: Vec<String> = vec![];
    if let Ok(rd) = fs::read_dir(root.join("projects")) {
        for e in rd.flatten() {
            let proj = e.path();
            if proj.join(".bare").exists() && proj.join("data").is_dir() {
                out.push(normpath(&proj.join("data")).to_string_lossy().into_owned());
            }
        }
    }
    out.sort();
    out
}

/// Every directory under `root` holding a valid `CACHEDIR.TAG`. Prunes at each match, at the
/// regenerable names, and at `skip`.
pub fn cache_tagged_paths(root: &Path, skip: &[String]) -> Vec<String> {
    let skip: BTreeSet<PathBuf> = skip.iter().map(|p| normpath(Path::new(p))).collect();
    let mut found = vec![];
    fn walk(dir: &Path, skip: &BTreeSet<PathBuf>, found: &mut Vec<PathBuf>) {
        let Ok(rd) = fs::read_dir(dir) else { return };
        let entries: Vec<fs::DirEntry> = rd.filter_map(Result::ok).collect();
        for e in &entries {
            if e.file_name() == CACHEDIR_TAG && e.file_type().is_ok_and(|t| t.is_file()) && is_cache_tag(&e.path()) {
                found.push(normpath(dir));
                return;
            }
        }
        for e in &entries {
            if !e.file_type().is_ok_and(|t| t.is_dir()) || REGENERABLE_DIRS.contains(&e.file_name().to_string_lossy().as_ref()) {
                continue;
            }
            let full = normpath(&e.path());
            if !skip.contains(&full) {
                walk(&full, skip, found);
            }
        }
    }
    let root = normpath(root);
    walk(&root, &skip, &mut found);
    let mut out: Vec<String> = found.into_iter().filter(|p| *p != root).map(|p| p.to_string_lossy().into_owned()).collect();
    out.sort();
    out
}

fn item(src: String, dst: String, recursive: bool) -> Value {
    json!({"DATA_TYPE": "transfer_item", "source_path": src, "destination_path": dst, "recursive": recursive})
}

/// The fewest transfer items that cover `root` except `excluded`.
pub fn partition(root: &str, excluded: &[String], dest_prefix: &str) -> Vec<Value> {
    let root = normpath(Path::new(root));
    let excluded: BTreeSet<PathBuf> = excluded.iter().map(|p| normpath(Path::new(p))).collect();
    let mut ancestors: BTreeSet<PathBuf> = BTreeSet::from([root.clone()]);
    for p in &excluded {
        let mut d = p.parent().map(Path::to_path_buf);
        while let Some(dir) = d {
            if dir.as_os_str().is_empty() || dir == root || ancestors.contains(&dir) {
                break;
            }
            ancestors.insert(dir.clone());
            d = dir.parent().map(Path::to_path_buf);
        }
    }
    let mut items = vec![];
    fn walk(dir: &Path, root: &Path, excluded: &BTreeSet<PathBuf>, ancestors: &BTreeSet<PathBuf>, dest_prefix: &str, items: &mut Vec<Value>) {
        let Some(entries) = read_dir_sorted(dir) else { return };
        for e in entries {
            let full = normpath(&e.path());
            let Ok(ft) = e.file_type() else { continue };
            if excluded.contains(&full) || ft.is_symlink() {
                continue;
            }
            let rel = full.strip_prefix(root).unwrap_or(&full);
            let dest = Path::new(dest_prefix).join(rel).to_string_lossy().into_owned();
            let full_s = full.to_string_lossy().into_owned();
            if ft.is_dir() {
                if REGENERABLE_DIRS.contains(&e.file_name().to_string_lossy().as_ref()) {
                    continue;
                }
                if ancestors.contains(&full) {
                    walk(&full, root, excluded, ancestors, dest_prefix, items);
                } else {
                    items.push(item(format!("{full_s}/"), format!("{dest}/"), true));
                }
            } else {
                items.push(item(full_s, dest, false));
            }
        }
    }
    walk(&root, &root, &excluded, &ancestors, dest_prefix, &mut items);
    items
}

pub fn destination_root(cfg: &ChinookConfig) -> String {
    format!("{}/{DEST_SUBDIR}", cfg.prefix)
}

/// The mirror's items, and what was excluded. The pin scan walks the whole workspace and
/// takes seconds to minutes.
pub fn build_items(paths: &Paths, cfg: &ChinookConfig) -> (Vec<Value>, Value) {
    let ws = &paths.workspace;
    let outs = dvc_output_paths(ws);
    let stores = shared_store_paths(ws);
    let cache = normpath(&paths.shared_cache()).to_string_lossy().into_owned();
    let mut skip = outs.clone();
    skip.extend(stores.iter().cloned());
    skip.push(cache.clone());
    let tagged = cache_tagged_paths(ws, &skip);
    let mut excluded = skip;
    excluded.extend(tagged.iter().cloned());
    let items = partition(&ws.to_string_lossy(), &excluded, &destination_root(cfg));
    let counts = json!({
        "dvc_outputs": outs.len(),
        "shared_stores": stores.len(),
        "shared_cache": 1,
        "cache_tagged_dirs": tagged.len(),
        "total": excluded.len(),
    });
    (items, counts)
}

/// The destructive flags, in one place.
pub fn options(cfg: &ChinookConfig) -> Submit<'_> {
    Submit {
        source: &cfg.local_endpoint,
        destination: &cfg.remote_endpoint,
        label: BACKUP_LABEL,
        // mtime and size: workspace files change in place, so size alone misses an edit.
        sync_level: 2,
        delete_destination_extra: true,
        skip_source_errors: true,
        filter_rules: Some(filter_rules()),
    }
}

pub fn backup(paths: &Paths, cfg: &ChinookConfig, api: Option<&dyn Api>, dry_run: bool) -> anyhow::Result<Value> {
    let (items, excluded) = build_items(paths, cfg);
    let mut out = json!({
        "items": items.len(),
        "excluded": excluded,
        "destination": format!("{}:{}", cfg.remote_endpoint, destination_root(cfg)),
        "mirror": true,
    });
    if dry_run {
        out["dry_run"] = json!(true);
        out["transfer_items"] = json!(items);
        return Ok(out);
    }
    let api = api.ok_or_else(|| anyhow::anyhow!("no Globus client"))?;
    out["submitted"] = json!(true);
    out["task_id"] = json!(globus::submit(api, &items, &options(cfg))?);
    Ok(out)
}
