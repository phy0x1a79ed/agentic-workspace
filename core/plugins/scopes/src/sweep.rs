//! The one-shot that empties each worktree's `.awm/` folder (scopes.md R142). It removes what
//! nothing reads any more, moves the DVC hooks' local state and the build stamps into the
//! worktree's git dir, and rewrites each DVC project's hooks to write there. It leaves
//! `context.md` and any file it does not know, and reports both. Idempotent.

use std::path::Path;

use awm_dvcdata::provision;
use serde_json::{json, Map, Value};

use crate::{path_str, Fail, Op};

pub const AWM_DIRNAME: &str = ".awm";
const DROPPED_FILES: [&str; 2] = ["history.md", "mcp-opencode.json"];
const DROPPED_LINKS: [&str; 2] = ["data", "skills"];
const MOVED_STATE: [&str; 4] = [provision::MOUNTS_FILE, provision::CHECKOUT_FAILED_FILE, "dsh-build-stamp", "trilium-build-stamp"];

fn is_link(p: &Path) -> bool {
    std::fs::symlink_metadata(p).is_ok_and(|m| m.file_type().is_symlink())
}

fn io(path: &Path) -> impl Fn(std::io::Error) -> Fail + '_ {
    move |e| crate::os_fail(&e, path)
}

/// Empty one worktree's `.awm/`. Returns what it did; empty when there was nothing.
pub fn sweep_worktree(worktree: &Path, dry_run: bool) -> Result<Map<String, Value>, Fail> {
    let awm = worktree.join(AWM_DIRNAME);
    let mut did = Map::new();
    if is_link(&awm) || !awm.is_dir() {
        return Ok(did);
    }
    let mut removed = vec![];
    for name in DROPPED_LINKS {
        let link = awm.join(name);
        if is_link(&link) {
            let target = std::fs::read_link(&link).map_err(io(&link))?;
            removed.push(format!("{name}->{}", target.display()));
            if !dry_run {
                std::fs::remove_file(&link).map_err(io(&link))?;
            }
        }
    }
    for name in DROPPED_FILES {
        let f = awm.join(name);
        if f.is_file() && !is_link(&f) {
            removed.push(name.to_string());
            if !dry_run {
                std::fs::remove_file(&f).map_err(io(&f))?;
            }
        }
    }
    if !removed.is_empty() {
        did.insert("removed".into(), json!(removed));
    }
    let moved: Vec<&str> = MOVED_STATE.iter().copied().filter(|n| awm.join(n).is_file()).collect();
    if !moved.is_empty() {
        did.insert("moved".into(), json!(moved));
        if !dry_run {
            move_state(worktree, &awm, &moved)?;
        }
    }
    let mut rest: Vec<String> = if awm.is_dir() {
        std::fs::read_dir(&awm).map_err(io(&awm))?.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).collect()
    } else {
        vec![]
    };
    rest.sort();
    if dry_run {
        rest.retain(|n| !moved.contains(&n.as_str()) && !DROPPED_FILES.contains(&n.as_str()) && !(DROPPED_LINKS.contains(&n.as_str()) && is_link(&awm.join(n))));
    }
    if rest.is_empty() {
        did.insert("awm_dir".into(), json!("removed"));
        if !dry_run {
            std::fs::remove_dir(&awm).map_err(io(&awm))?;
        }
    } else {
        did.insert("left".into(), json!(rest));
    }
    Ok(did)
}

fn move_state(worktree: &Path, awm: &Path, names: &[&str]) -> Result<(), Fail> {
    let state = provision::state_dir(worktree);
    std::fs::create_dir_all(&state).map_err(io(&state))?;
    for name in names {
        let (src, dest) = (awm.join(name), state.join(name));
        if *name == provision::CHECKOUT_FAILED_FILE && dest.exists() {
            let text = std::fs::read_to_string(&src).map_err(io(&src))?;
            use std::io::Write;
            let mut f = std::fs::OpenOptions::new().append(true).open(&dest).map_err(io(&dest))?;
            f.write_all(text.as_bytes()).map_err(io(&dest))?;
            std::fs::remove_file(&src).map_err(io(&src))?;
        } else if dest.exists() {
            tracing::warn!("{}: {} already holds {name}; keeping it and dropping the old copy", worktree.display(), state.display());
            std::fs::remove_file(&src).map_err(io(&src))?;
        } else if std::fs::rename(&src, &dest).is_err() {
            std::fs::copy(&src, &dest).map_err(io(&dest))?;
            std::fs::remove_file(&src).map_err(io(&src))?;
        }
    }
    Ok(())
}

impl Op {
    /// Sweep every worktree of every project under `projects/`.
    pub fn awm_sweep(&self, dry_run: bool) -> Result<Value, Fail> {
        let root = self.paths.projects.clone();
        let mut worktrees = vec![];
        let mut hooks = Map::new();
        let mut errors = vec![];
        for project in self.bare_projects() {
            let bare = root.join(&project).join(".bare");
            self.locked(&project, || -> Result<(), Fail> {
                let mut paths: Vec<_> = match self.worktree_branches(&bare) {
                    Ok(w) => w.into_iter().map(|(p, _)| p).filter(|p| p.is_dir()).collect(),
                    Err(e) if e.class == "RuntimeError" || e.is_os_error() => {
                        errors.push(json!({"project": project, "error": e.message}));
                        return Ok(());
                    }
                    Err(e) => return Err(e),
                };
                paths.sort();
                for wt in &paths {
                    match sweep_worktree(wt, dry_run) {
                        Ok(did) if !did.is_empty() => {
                            let mut row = Map::new();
                            row.insert("project".into(), json!(project));
                            row.insert("worktree".into(), json!(path_str(wt)));
                            row.extend(did);
                            worktrees.push(Value::Object(row));
                        }
                        Ok(_) => {}
                        Err(e) if e.is_os_error() => errors.push(json!({"worktree": path_str(wt), "error": format!("{}: {}", e.class, e.message)})),
                        Err(e) => return Err(e),
                    }
                }
                if let Some(dvc_wt) = paths.iter().find(|wt| provision::is_dvc_repo(wt)) {
                    let v = if dry_run {
                        json!("would-rewrite")
                    } else {
                        awm_dvcdata::wiring::ensure_repo_wiring(dvc_wt, &self.hook_bin).get("actions").cloned().unwrap_or_else(|| json!({}))
                    };
                    hooks.insert(project.clone(), v);
                }
                Ok(())
            })?;
        }
        let left = worktrees.iter().filter(|w| w.get("left").is_some_and(crate::args::truthy)).count();
        Ok(json!({"dry_run": dry_run, "worktrees": worktrees, "hooks": hooks, "errors": errors, "left_behind": left}))
    }
}
