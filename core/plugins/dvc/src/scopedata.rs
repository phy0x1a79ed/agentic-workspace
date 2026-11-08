//! The per-scope data verbs: a scope's data view, its chunk mounts and the shared cache's
//! gc. They resolve a scope from `(project, scope)` through the workspace layout and git,
//! never the scopes database, so dvc answers without the scopes service.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::process::Command;

use awm_dvcdata::cachetag::has_cache_tag;
use awm_dvcdata::gc::{collect_garbage, GcProject, GcRequest};
use awm_dvcdata::provision::{self, Layout};
use awm_plugin_sdk::VerbError;
use serde_json::{json, Map, Value};

use crate::Paths;

pub fn not_found(m: impl Into<String>) -> VerbError {
    VerbError::new("FileNotFoundError", m)
}

fn git(args: &[&str]) -> std::io::Result<std::process::Output> {
    Command::new("git").args(args).env("GIT_TERMINAL_PROMPT", "0").output()
}

/// Refuse a name that could escape `projects/` once joined onto it.
pub fn check_name(name: &str, kind: &str, nested: bool) -> Result<(), VerbError> {
    if name.is_empty() {
        return Err(VerbError::value(format!("{kind} must be a non-empty string")));
    }
    let segs: Vec<&str> = if nested { name.split('/').collect() } else { vec![name] };
    for seg in segs {
        if seg.is_empty() || seg.starts_with('.') || seg.contains('/') || seg.contains('\\') || seg.contains('\0') {
            return Err(VerbError::value(format!("{kind} is not a safe path segment (got '{name}')")));
        }
    }
    Ok(())
}

/// Every worktree of a project's bare repo with its branch, from git. A worktree missing
/// from this list is one whose pins fall out of gc's keep-set, so this fails rather than
/// returning a short list.
pub fn worktree_entries(paths: &Paths, project: &str) -> Result<Vec<(PathBuf, Option<String>)>, VerbError> {
    let bare = paths.projects().join(project).join(".bare");
    if !bare.exists() {
        return Err(not_found(format!("Project '{project}' has no bare repo at {}", bare.display())));
    }
    let o = git(&["-C", &bare.to_string_lossy(), "worktree", "list", "--porcelain"]).map_err(|e| VerbError::runtime(format!("Could not enumerate worktrees of '{project}': {e}")))?;
    if !o.status.success() {
        return Err(VerbError::runtime(format!("Could not enumerate worktrees of '{project}': {}", String::from_utf8_lossy(&o.stderr).trim())));
    }
    Ok(parse_worktrees(&String::from_utf8_lossy(&o.stdout)))
}

pub fn parse_worktrees(text: &str) -> Vec<(PathBuf, Option<String>)> {
    let mut out = vec![];
    let (mut path, mut branch): (Option<PathBuf>, Option<String>) = (None, None);
    for line in text.lines() {
        if let Some(p) = line.strip_prefix("worktree ") {
            path = Some(PathBuf::from(p));
            branch = None;
        } else if let Some(b) = line.strip_prefix("branch ") {
            branch = Some(b.strip_prefix("refs/heads/").unwrap_or(b).to_string());
        } else if line.trim() == "bare" {
            path = None;
        } else if line.trim().is_empty() {
            if let Some(p) = path.take() {
                out.push((p, branch.take()));
            }
        }
    }
    if let Some(p) = path {
        out.push((p, branch));
    }
    out
}

pub fn project_worktrees(paths: &Paths, project: &str) -> Result<Vec<PathBuf>, VerbError> {
    Ok(worktree_entries(paths, project)?.into_iter().map(|(p, _)| p).collect())
}

fn resolved(p: &Path) -> PathBuf {
    std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf())
}

/// The scope's worktree, confirmed against the bare repo's own list. Acting on a wrong
/// directory reads as "this scope has no data", so anything else is an error naming it.
pub fn scope_worktree(paths: &Paths, project: &str, scope: &str) -> Result<PathBuf, VerbError> {
    let derived = paths.projects().join(project).join(scope);
    if !derived.is_dir() {
        return Err(not_found(format!("No worktree directory for scope '{project}/{scope}' at {}", derived.display())));
    }
    let registered: Vec<(PathBuf, Option<String>)> = worktree_entries(paths, project)?.into_iter().map(|(p, b)| (resolved(&p), b)).collect();
    let me = resolved(&derived);
    let Some((_, branch)) = registered.iter().find(|(p, _)| *p == me) else {
        let mut list: Vec<String> = registered.iter().map(|(p, _)| p.display().to_string()).collect();
        list.sort();
        return Err(VerbError::value(format!("{} is not a worktree of project '{project}''s bare repo (registered: {list:?})", derived.display())));
    };
    if branch.is_none() {
        return Err(VerbError::value(format!("{} has no branch checked out (detached HEAD), so it is not a scope worktree", derived.display())));
    }
    Ok(derived)
}

fn unresolved_worktrees(wts: &[PathBuf]) -> Vec<String> {
    wts.iter().filter(|w| !w.is_dir() || !w.join(".git").exists()).map(|w| w.display().to_string()).collect()
}

/// Every project whose objects may live in the shared cache: its common git config carries
/// awm's merge driver, or a worktree tracks `.dvc/config`. A project whose worktrees cannot
/// be listed counts as wired. A no-remote project keeps its own cache and is never wired.
pub fn dvc_wired_projects(paths: &Paths) -> BTreeSet<String> {
    let mut wired = BTreeSet::new();
    let mut bares: Vec<PathBuf> = std::fs::read_dir(paths.projects())
        .map(|rd| rd.flatten().map(|e| e.path().join(".bare")).filter(|b| b.exists()).collect())
        .unwrap_or_default();
    bares.sort();
    for bare in bares {
        let Some(dir) = bare.parent() else { continue };
        let project = dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        if has_cache_tag(dir) {
            continue;
        }
        if let Ok(o) = git(&["-C", &bare.to_string_lossy(), "config", "--get", "merge.dvc.driver"]) {
            if o.status.success() && !String::from_utf8_lossy(&o.stdout).trim().is_empty() {
                wired.insert(project);
                continue;
            }
        }
        match project_worktrees(paths, &project) {
            Err(_) => {
                wired.insert(project);
            }
            Ok(wts) => {
                if wts.iter().any(|w| provision::is_dvc_repo(w)) {
                    wired.insert(project);
                }
            }
        }
    }
    wired
}

pub fn layout(paths: &Paths) -> Layout {
    Layout::new(paths.workspace.clone())
}

pub fn scope_status(paths: &Paths, project: &str, scope: &str) -> Result<Value, VerbError> {
    check_name(project, "project name", false)?;
    check_name(scope, "scope name", true)?;
    let wt = scope_worktree(paths, project, scope)?;
    Ok(provision::data_status(&layout(paths), project, scope, &wt))
}

/// Choose which chunks this scope materialises; no chunks means all. Every chunk the branch
/// pins stays pinned, hashed and backed up regardless.
pub fn mount(paths: &Paths, hook_bin: &Path, project: &str, scope: &str, chunks: Option<Vec<String>>) -> Result<Value, VerbError> {
    check_name(project, "project name", false)?;
    check_name(scope, "scope name", true)?;
    let wt = scope_worktree(paths, project, scope)?;
    let chunks = chunks.filter(|c| !c.is_empty());
    provision::write_mounts(&wt, chunks.as_deref()).map_err(|e| VerbError::runtime(e.to_string()))?;
    let report = provision::provision_scope_data(&layout(paths), project, scope, &wt, hook_bin);
    let mut out = Map::new();
    out.insert("project".into(), json!(project));
    out.insert("scope".into(), json!(scope));
    if let Value::Object(m) = report {
        out.extend(m);
    }
    Ok(Value::Object(out))
}

pub struct GcArgs {
    pub projects: Vec<String>,
    pub dry_run: bool,
    pub keep: String,
    pub exclude: Vec<String>,
    pub accept_missing: Vec<String>,
}

/// Reclaim the objects no listed project references. Dry by default.
pub fn gc(paths: &Paths, a: GcArgs) -> Result<Value, VerbError> {
    let mut listed = vec![];
    for p in &a.projects {
        check_name(p, "project name", false)?;
        let mut wts = project_worktrees(paths, p)?;
        wts.sort();
        let bad = unresolved_worktrees(&wts);
        if !bad.is_empty() {
            return Ok(json!({"result": "refused", "detail": format!(
                "project '{p}' has worktrees that cannot be read: {bad:?}. Their pins would fall out of the keep-set and their objects would be collected. Run `scope heal` or `git worktree prune` on the project, then retry."
            )}));
        }
        listed.push(GcProject { name: p.clone(), bare: paths.projects().join(p).join(".bare"), worktrees: wts });
    }
    for p in &a.exclude {
        check_name(p, "project name", false)?;
    }
    let mut req = GcRequest::new(listed, dvc_wired_projects(paths));
    req.exclude = a.exclude;
    req.accept_missing = a.accept_missing;
    req.dry_run = a.dry_run;
    req.keep = a.keep;
    Ok(collect_garbage(&layout(paths), &req))
}
