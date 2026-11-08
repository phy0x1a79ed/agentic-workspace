//! The scope lifecycle: create, repair, complete, sync, gather, scatter, delete and heal.
//!
//! A scope is a row in `agents` (allocated, active or retired), a worktree of its project's
//! bare repo at `projects/<project>/<scope>`, and a data view. Every check that can refuse a
//! removal runs before anything changes.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use awm_dvcdata::provision::{self, Layout};
use awm_dvcdata::status::State;
use awm_pycompat::repr::str_repr;
use awm_store::rusqlite::OptionalExtension;
use serde_json::{json, Map, Value};

use crate::db::{self, NewAgent};
use crate::names::{validate_project, validate_scope};
use crate::{is_relative_to, path_str, Fail, Op};

pub const VAGRANT_PROJECT: &str = "_vagrant";

/// `ScopeActionResponse`, in the Python's key order.
#[derive(Debug, Clone, PartialEq)]
pub struct Action {
    pub project: String,
    pub scope: String,
    pub status: String,
    pub message: String,
    pub session: Option<i64>,
    pub discarded: Option<Value>,
}

impl Action {
    pub fn new(project: &str, scope: &str, status: &str, message: impl Into<String>) -> Self {
        Self { project: project.into(), scope: scope.into(), status: status.into(), message: message.into(), session: None, discarded: None }
    }

    pub fn to_json(&self) -> Value {
        json!({"project": self.project, "scope": self.scope, "status": self.status, "message": self.message,
               "session": self.session, "discarded": self.discarded})
    }
}

/// `ScopeInfo`: one scope as `search` lists it.
#[derive(Debug, Clone, PartialEq)]
pub struct ScopeInfo {
    pub project: String,
    pub scope: String,
    pub status: String,
    pub branch: String,
    pub worktree: String,
    pub repo_path: Option<String>,
    pub session: i64,
    pub private: bool,
    pub tags: Vec<String>,
}

impl ScopeInfo {
    pub fn to_json(&self) -> Value {
        json!({"project": self.project, "scope": self.scope, "status": self.status, "branch": self.branch,
               "worktree": self.worktree, "repo_path": self.repo_path, "session": self.session,
               "private": self.private, "tags": self.tags})
    }
}

/// `create`'s request.
#[derive(Debug, Clone, Default)]
pub struct CreateReq {
    pub project: String,
    pub scope: Option<String>,
    pub from_branch: Option<String>,
    pub branch_name: Option<String>,
    pub context: Option<String>,
    pub new_project: bool,
    pub clone_url: Option<String>,
    pub fork_url: Option<String>,
    pub tags: Option<Vec<String>>,
}

/// What a forced removal threw away, or `None`.
fn discard_list(uncommitted: &[String], removal: &Removal) -> Option<Value> {
    let uncommitted: Vec<&str> = uncommitted.iter().map(|p| awm_pycompat::pystr::strip(p)).collect();
    if uncommitted.is_empty() && removal.dvc_drift.is_empty() && removal.commits.is_empty() {
        return None;
    }
    Some(json!({"uncommitted": uncommitted, "dvc_drift": removal.dvc_drift, "commits": removal.commits}))
}

/// The R44 checks' findings that a forced removal discards.
#[derive(Debug, Default)]
pub struct Removal {
    pub dvc_drift: Vec<String>,
    pub commits: Vec<String>,
}

const LOSABLE: [State; 3] = [State::Modified, State::New, State::NotInCache];

impl Op {
    fn layout(&self) -> Layout {
        Layout::new(&self.paths.workspace)
    }

    /// git's per-worktree admin directory under `.bare/worktrees/`, asked of git: a nested
    /// `fabfos/dev` and a flat `dev` share a basename but own different admin dirs.
    pub fn worktree_admin_dir(&self, bare: &Path, worktree: &Path) -> Result<Option<PathBuf>, Fail> {
        let r = self.git_in(worktree, &["rev-parse", "--absolute-git-dir"])?;
        let dir = r.stdout.trim();
        if !r.ok() || dir.is_empty() {
            return Ok(None);
        }
        let admin = PathBuf::from(dir);
        Ok(is_relative_to(&admin, &bare.join("worktrees")).then_some(admin))
    }

    /// Registered worktrees of `bare` that live beneath `path`: a container of nested scopes.
    pub fn live_worktrees_under(&self, bare: &Path, path: &Path) -> Result<Vec<PathBuf>, Fail> {
        Ok(self.worktree_paths(bare)?.unwrap_or_default().into_iter().filter(|wt| wt != path && is_relative_to(wt, path)).collect())
    }

    /// The branch a scope is on: its row's, else its worktree's HEAD, else `feat/<scope>`.
    pub fn scope_branch(&self, project: &str, scope: &str, worktree: Option<&Path>) -> Result<String, Fail> {
        if let Some(rec) = self.agent_record(project, scope, false)? {
            if !rec.branch.is_empty() {
                return Ok(rec.branch);
            }
        }
        if let Some(wt) = worktree.filter(|w| w.exists()) {
            let r = self.git_in(wt, &["branch", "--show-current"])?;
            if r.ok() && !r.stdout.trim().is_empty() {
                return Ok(r.stdout.trim().to_string());
            }
        }
        Ok(format!("feat/{scope}"))
    }

    /// Refuse a branch name git cannot hold: a malformed ref, or one that clashes with an
    /// existing branch as a path (`feat/x` against `feat/x/y`).
    pub fn assert_branch_available(&self, bare: &Path, branch: &str) -> Result<(), Fail> {
        if !self.git(&["check-ref-format", &format!("refs/heads/{branch}")])?.ok() {
            return Err(Fail::value(format!("'{branch}' is not a valid git branch name")));
        }
        let r = self.git_in(bare, &["for-each-ref", "--format=%(refname:short)", "refs/heads/"])?;
        if !r.ok() {
            return Ok(());
        }
        let existing: BTreeSet<&str> = r.stdout.split_whitespace().collect();
        if existing.contains(branch) {
            return Ok(());
        }
        let parts: Vec<&str> = branch.split('/').collect();
        for i in 1..parts.len() {
            let prefix = parts[..i].join("/");
            if existing.contains(prefix.as_str()) {
                return Err(Fail::value(format!(
                    "Cannot create branch '{branch}': branch '{prefix}' already exists and git cannot hold both (refs are stored as paths). Rename or delete '{prefix}' first."
                )));
            }
        }
        let beneath: Vec<&str> = existing.iter().copied().filter(|b| b.starts_with(&format!("{branch}/"))).collect();
        if !beneath.is_empty() {
            let more = if beneath.len() > 3 { " …" } else { "" };
            return Err(Fail::value(format!(
                "Cannot create branch '{branch}': {}{more} already exist beneath it and git cannot hold both (refs are stored as paths).",
                beneath[..beneath.len().min(3)].join(", ")
            )));
        }
        Ok(())
    }

    /// Delete a local branch and return the SHA it held, or `None` when nothing went. With
    /// `expected` the ref goes only while it still points there, compared by `update-ref`.
    fn delete_branch(&self, bare: &Path, branch: &str, expected: Option<&str>) -> Result<Option<String>, Fail> {
        let refname = format!("refs/heads/{branch}");
        let Some(sha) = self.ref_sha(bare, &refname)? else { return Ok(None) };
        if expected.is_some_and(|e| e != sha) {
            return Ok(None);
        }
        if let Some(holder) = self.branch_checkouts(bare)?.get(branch) {
            return Err(Fail::runtime(format!("branch '{branch}' is checked out at {}", holder.display())));
        }
        let r = self.git_in(bare, &["update-ref", "-d", &refname, &sha])?;
        if !r.ok() {
            if expected.is_some() {
                return Ok(None);
            }
            return Err(Fail::runtime(format!("could not delete branch '{branch}': {}", r.stderr.trim())));
        }
        self.git_in(bare, &["config", "--remove-section", &format!("branch.{branch}")])?;
        Ok(Some(sha))
    }

    /// Delete the branch when asked, and say what became of it.
    pub fn settle_branch(&self, bare: &Path, branch: &str, delete: bool) -> Result<String, Fail> {
        if !delete {
            return Ok(format!("branch {branch} kept"));
        }
        match self.delete_branch(bare, branch, None) {
            Err(e) if e.class == "RuntimeError" => Ok(format!("branch {branch} kept: {}", e.message)),
            Err(e) => Err(e),
            Ok(None) => Ok(format!("branch {branch} not found, nothing deleted")),
            Ok(Some(sha)) => Ok(format!("branch {branch} deleted at {sha} (restore: git branch {branch} {sha})")),
        }
    }

    /// Check, then remove, a scope's worktree. Returns the uncommitted lines a force discarded.
    pub fn cleanup_worktree(&self, bare: &Path, worktree: &Path, force: bool) -> Result<Vec<String>, Fail> {
        let pending = self.worktree_guard(bare, worktree, force)?;
        self.remove_worktree(bare, worktree)?;
        Ok(pending)
    }

    /// Refuse to remove a worktree whose removal would lose uncommitted work, or that is the
    /// directory of nested scopes rather than a scope. Changes nothing. Returns the
    /// uncommitted `git status` lines `force` discards.
    fn worktree_guard(&self, bare: &Path, worktree: &Path, force: bool) -> Result<Vec<String>, Fail> {
        let admin = self.worktree_admin_dir(bare, worktree)?;
        let contained = self.live_worktrees_under(bare, worktree)?;
        if !contained.is_empty() {
            let names: Vec<String> = contained.iter().take(3).map(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()).collect();
            return Err(Fail::runtime(format!(
                "Refusing to remove {}: it is not a scope, it contains {} live worktree(s) ({}). Delete the nested scopes individually; the directory goes when the last one does.",
                worktree.display(),
                contained.len(),
                names.join(", ")
            )));
        }
        // Only probe the top of a real worktree: elsewhere git walks up to an enclosing repo.
        if worktree.is_dir() && admin.is_some() {
            let st = self.git_in(worktree, &["status", "--porcelain"])?;
            let pending: Vec<String> = st.stdout.lines().filter(|l| !l.trim().is_empty()).map(str::to_string).collect();
            if !pending.is_empty() {
                if !force {
                    let sample: Vec<&str> = pending.iter().take(3).map(|p| awm_pycompat::pystr::strip(p)).collect();
                    return Err(Fail::runtime(format!(
                        "Refusing to remove {}: {} uncommitted change(s) would be lost (e.g. {}). Commit them — `dvc add` any new data chunk first — or pass force=true.",
                        worktree.display(),
                        pending.len(),
                        sample.join(", ")
                    )));
                }
                return Ok(pending);
            }
        }
        Ok(vec![])
    }

    /// Remove a worktree the guard passed, and its git admin dir. The fallback makes
    /// directories writable, never files: a materialised file shares its inode with the
    /// shared cache object.
    fn remove_worktree(&self, bare: &Path, worktree: &Path) -> Result<(), Fail> {
        let admin = self.worktree_admin_dir(bare, worktree)?;
        let r = self.git_in(bare, &["worktree", "remove", &path_str(worktree), "--force"])?;
        if !r.ok() && worktree.exists() {
            provision::chmod_dirs_writable(worktree);
            let _ = std::fs::remove_dir_all(worktree);
        }
        if let Some(admin) = admin.filter(|a| a.exists()) {
            let _ = std::fs::remove_dir_all(admin);
        }
        // Catches the admin dir of a worktree whose directory was already gone.
        self.git_in(bare, &["worktree", "prune"])?;
        if let (Some(project_dir), Some(parent)) = (bare.parent(), worktree.parent()) {
            prune_empty_parents(project_dir, parent);
        }
        Ok(())
    }

    /// Commits on `branch` that no other branch or remote ref holds (R44 item 4).
    pub fn unique_commits(&self, bare: &Path, branch: &str) -> Result<Vec<String>, Fail> {
        let own = format!("refs/heads/{branch}");
        if self.ref_sha(bare, &own)?.is_none() {
            return Ok(vec![]);
        }
        let refs = self.git_in(bare, &["for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"])?;
        let others: Vec<&str> = refs.stdout.split_whitespace().filter(|r| *r != own).collect();
        let mut args = vec!["rev-list", "--oneline", own.as_str(), "--not"];
        args.extend(others);
        let r = self.git_in(bare, &args)?;
        Ok(if r.ok() { r.stdout.lines().filter(|l| !l.trim().is_empty()).map(str::to_string).collect() } else { vec![] })
    }

    /// The R44 checks before a scope's worktree goes. A live session's claim refuses always;
    /// data that differs from its pin and commits no other ref holds refuse unless `force`.
    #[allow(clippy::too_many_arguments)]
    fn removal_guard(&self, bare: &Path, worktree: &Path, project: &str, scope: &str, branch: &str, force: bool, commits: bool) -> Result<Removal, Fail> {
        if let Some(holder) = self.live_holder(project, scope)? {
            return Err(Fail::runtime(format!(
                "Refusing to remove {project}/{scope}: live session {} holds it. force does not override a live session (scopes.md R44).",
                crate::args::py_str(&holder["session_id"])
            )));
        }
        let drift = if worktree.is_dir() { dvc_drift(worktree) } else { vec![] };
        let commits = if commits { self.unique_commits(bare, branch)? } else { vec![] };
        let mut blockers = vec![];
        if !drift.is_empty() {
            blockers.push(format!("{} pinned path(s) whose data differs from its pin ({})", drift.len(), drift[..drift.len().min(3)].join(", ")));
        }
        if !commits.is_empty() {
            let shas: Vec<&str> = commits.iter().take(3).map(|c| c.split_whitespace().next().unwrap_or("")).collect();
            blockers.push(format!("{} commit(s) on {branch} that no other branch or remote holds ({})", commits.len(), shas.join(", ")));
        }
        if !blockers.is_empty() && !force {
            return Err(Fail::runtime(format!(
                "Refusing to remove {}: {}. Save them, or pass force=true to discard them.",
                worktree.display(),
                blockers.join("; ")
            )));
        }
        Ok(Removal { dvc_drift: drift, commits })
    }

    pub fn is_tracked(&self, worktree: &Path, path: &str) -> Result<bool, Fail> {
        Ok(self.git_in(worktree, &["ls-files", "--error-unmatch", path])?.ok())
    }

    /// Drop an untracked AGENTS.md or CLAUDE.md, and bring a DVC project's data view to what
    /// its checkout implies.
    pub fn heal_worktree(&self, worktree: &Path, project: &str, scope: &str, dry_run: bool) -> Result<Value, Fail> {
        let mut agents_md = Value::Null;
        let mut claude_md = Value::Null;
        let mut data = Value::Null;
        let rm = |p: &Path| std::fs::remove_file(p).map_err(|e| crate::os_fail(&e, p));

        let agents = worktree.join("AGENTS.md");
        let agents_meta = std::fs::symlink_metadata(&agents).ok();
        if agents.is_file() && !agents_meta.is_some_and(|m| m.file_type().is_symlink()) && !self.is_tracked(worktree, "AGENTS.md")? {
            agents_md = json!("deleted:untracked");
            if !dry_run {
                rm(&agents)?;
            }
        }

        let claude = worktree.join("CLAUDE.md");
        let is_link = std::fs::symlink_metadata(&claude).is_ok_and(|m| m.file_type().is_symlink());
        if is_link {
            let target = std::fs::read_link(&claude).map(|t| t.to_string_lossy().into_owned()).unwrap_or_default();
            if target == "AGENTS.md" {
                claude_md = json!("deleted:symlink");
                if !dry_run {
                    rm(&claude)?;
                }
            } else if !self.is_tracked(worktree, "CLAUDE.md")? {
                claude_md = json!("deleted:untracked-symlink");
                if !dry_run {
                    rm(&claude)?;
                }
            }
        } else if claude.exists() && !self.is_tracked(worktree, "CLAUDE.md")? {
            claude_md = json!("deleted:untracked");
            if !dry_run {
                rm(&claude)?;
            }
        }

        if !dry_run && provision::is_dvc_project(worktree) {
            let report = provision::provision_scope_data(&self.layout(), project, scope, worktree, &self.hook_bin);
            if report.get("mode").and_then(Value::as_str) == Some("unknown") {
                let detail = report.get("detail").and_then(Value::as_str).unwrap_or("");
                data = json!(format!("error:{}", awm_pycompat::pystr::head(detail, 120)));
            } else if report.get("stale_checkout").is_some_and(crate::args::truthy) {
                data = json!("stale-checkout");
            }
        }
        Ok(json!({"agents_md": agents_md, "claude_md": claude_md, "data": data}))
    }

    /// A scope's worktree as an absolute path. An empty or relative legacy row falls back to
    /// `projects/<project>/<scope>` or anchors on the workspace, never on the cwd.
    pub fn resolve_worktree(&self, project: &str, scope: &str, recorded: Option<&str>) -> PathBuf {
        let raw = recorded.map(awm_pycompat::pystr::strip).unwrap_or("");
        if raw.is_empty() {
            return self.paths.projects.join(project).join(scope);
        }
        let p = PathBuf::from(raw);
        if p.is_absolute() {
            p
        } else {
            self.paths.projects.parent().map(Path::to_path_buf).unwrap_or_default().join(p)
        }
    }

    /// The repair pass over the active scopes, one report row each.
    pub fn heal_scopes(&self, project: Option<&str>, dry_run: bool) -> Result<Vec<Value>, Fail> {
        let mut sql = "SELECT p.name AS project, a.scope, a.worktree FROM agents a JOIN projects p ON p.id = a.project_id WHERE a.status IN ('allocated','active')".to_string();
        if project.is_some() {
            sql.push_str(" AND p.name=?");
        }
        let rows: Vec<(String, String, String)> = self.read(|c| {
            let mut stmt = c.prepare(&sql)?;
            let it = stmt.query_map(awm_store::rusqlite::params_from_iter(project), |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
            Ok(it.collect::<Result<_, _>>()?)
        })?;
        let mut report = vec![];
        for (p, s, wt) in rows {
            let worktree = self.resolve_worktree(&p, &s, Some(&wt));
            let shown = path_str(&worktree);
            if !worktree.is_dir() {
                report.push(json!({"project": p, "scope": s, "worktree": shown, "ok": false, "error": format!("worktree missing: {shown}")}));
                continue;
            }
            let healed = self.locked(&p, || self.heal_worktree(&worktree, &p, &s, dry_run));
            match healed {
                Ok(actions) => report.push(json!({"project": p, "scope": s, "worktree": shown, "ok": true, "dry_run": dry_run, "actions": actions})),
                Err(e) if e.is_os_error() => {
                    report.push(json!({"project": p, "scope": s, "worktree": shown, "ok": false, "error": format!("{}: {}", e.class, e.message)}))
                }
                Err(e) => return Err(e),
            }
        }
        Ok(report)
    }

    /// The projects under `projects/` with a bare repo, sorted.
    pub fn bare_projects(&self) -> Vec<String> {
        let mut out: Vec<String> = std::fs::read_dir(&self.paths.projects)
            .map(|rd| rd.flatten().filter(|e| e.path().join(".bare").is_dir()).map(|e| e.file_name().to_string_lossy().into_owned()).collect())
            .unwrap_or_default();
        out.sort();
        out
    }

    /// Each project's main checkout where no agents row names it: the worktree of the bare
    /// repo's HEAD branch at `projects/<p>/<branch>`.
    pub fn main_checkouts(&self, project: Option<&str>) -> Result<BTreeMap<String, ScopeInfo>, Fail> {
        let names = match project {
            Some(p) => vec![p.to_string()],
            None => self.bare_projects(),
        };
        let named: BTreeSet<(String, String)> = self.read(|c| {
            let mut stmt = c.prepare("SELECT p.name AS project, a.scope FROM agents a JOIN projects p ON p.id = a.project_id")?;
            let it = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?;
            Ok(it.collect::<Result<_, _>>()?)
        })?;
        let mut found = BTreeMap::new();
        for name in names {
            let bare = self.paths.bare(&name);
            if !bare.is_dir() {
                continue;
            }
            let Some(branch) = crate::git::head_branch(&bare) else { continue };
            let worktree = self.paths.projects.join(&name).join(&branch);
            if worktree.is_dir() && !named.contains(&(name.clone(), branch.clone())) {
                let wt = path_str(&worktree);
                found.insert(
                    name.clone(),
                    ScopeInfo { project: name, scope: branch.clone(), status: "active".into(), branch, worktree: wt.clone(), repo_path: Some(wt), session: 1, private: false, tags: vec![] },
                );
            }
        }
        Ok(found)
    }

    /// Mismatches between the scope rows and the worktrees on disk. Reads only.
    pub fn audit_worktrees(&self, project: Option<&str>) -> Result<Value, Fail> {
        let projects = match project {
            Some(p) => vec![p.to_string()],
            None => self.bare_projects(),
        };
        let mut sql = "SELECT p.name AS project, a.scope, a.worktree, a.status FROM agents a JOIN projects p ON p.id = a.project_id".to_string();
        if project.is_some() {
            sql.push_str(" WHERE p.name=?");
        }
        let rows: Vec<(String, String, String, String)> = self.read(|c| {
            let mut stmt = c.prepare(&sql)?;
            let it = stmt.query_map(awm_store::rusqlite::params_from_iter(project), |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?;
            Ok(it.collect::<Result<_, _>>()?)
        })?;
        let mut names: HashMap<String, BTreeSet<String>> = HashMap::new();
        let mut paths: HashMap<String, BTreeSet<PathBuf>> = HashMap::new();
        let mut orphaned = vec![];
        for (p, s, wt, status) in rows {
            let worktree = self.resolve_worktree(&p, &s, Some(&wt));
            names.entry(p.clone()).or_default().insert(s.clone());
            paths.entry(p.clone()).or_default().insert(resolve(&worktree));
            if (status == "allocated" || status == "active") && !worktree.is_dir() {
                orphaned.push(json!({"project": p, "scope": s, "status": status, "worktree": path_str(&worktree)}));
            }
        }
        for (name, main) in self.main_checkouts(project)? {
            paths.entry(name).or_default().insert(resolve(Path::new(&main.worktree)));
        }
        let mut unregistered = vec![];
        let mut errors = vec![];
        for name in projects {
            let project_dir = self.paths.projects.join(&name);
            let worktrees = match self.worktree_branches(&project_dir.join(".bare")) {
                Ok(w) => w,
                Err(e) if e.class == "RuntimeError" || e.is_os_error() => {
                    errors.push(json!({"project": name, "error": e.message}));
                    continue;
                }
                Err(e) => return Err(e),
            };
            let known_names = names.get(&name);
            let known_paths = paths.get(&name);
            for (path, branch) in worktrees {
                let Ok(rel) = path.strip_prefix(&project_dir) else { continue };
                if rel.components().any(|c| c.as_os_str() == ".claude") || !path.is_dir() {
                    continue;
                }
                let scope = rel.to_string_lossy().into_owned();
                if known_names.is_some_and(|n| n.contains(&scope)) || known_paths.is_some_and(|k| k.contains(&resolve(&path))) {
                    continue;
                }
                unregistered.push(json!({"project": name, "scope": scope, "worktree": path_str(&path), "branch": branch}));
            }
        }
        Ok(json!({"unregistered": unregistered, "orphaned_rows": orphaned, "errors": errors}))
    }

    /// Provision a new worktree's data view, then record its agents row.
    pub fn register_scope(&self, project: &str, scope: &str, repo_dir: &Path, branch: &str, is_vagrant: bool) -> Result<(), Fail> {
        self.fault("provision")?;
        let report = provision::provision_scope_data(&self.layout(), project, scope, repo_dir, &self.hook_bin);
        if report.get("mode").and_then(Value::as_str) == Some("unknown") {
            tracing::warn!("scope {project}/{scope}: {}", report.get("detail").map(crate::args::py_str).unwrap_or_default());
        }
        let bare = path_str(&self.paths.bare(project));
        let worktree = path_str(repo_dir);
        self.write(|c| {
            db::ensure_project(c, project, &bare, None)?;
            db::ensure_agent(c, project, scope, &NewAgent { branch: Some(branch), worktree: &worktree, is_vagrant, ..NewAgent::default() })?;
            Ok(())
        })?;
        self.fault("registered")
    }

    /// Write `text` as the scope's journal brief. Returns a warning when it could not.
    pub fn set_brief(&self, project: &str, scope: &str, text: Option<&str>) -> Option<String> {
        let text = awm_pycompat::pystr::strip(text.unwrap_or(""));
        if text.is_empty() {
            return None;
        }
        let args = json!({"project": project, "scope": scope, "text": text});
        match self.out.call("journal", "brief_set", args, std::time::Duration::from_secs(10)) {
            Ok(_) => None,
            Err(e) => {
                tracing::warn!("scope {project}/{scope}: brief not written: {e}");
                Some(format!("the brief was not written ({e}); set it with journal brief_set"))
            }
        }
    }

    /// Each remote whose fetched refs carry `branch`, with its SHA. No network.
    fn remote_copies(&self, bare: &Path, branch: &str) -> Result<BTreeMap<String, String>, Fail> {
        let r = self.git_in(bare, &["remote"])?;
        let remotes: Vec<String> = if r.ok() { r.stdout.split_whitespace().map(str::to_string).collect() } else { vec![] };
        let mut copies = BTreeMap::new();
        for remote in remotes {
            if let Some(sha) = self.ref_sha(bare, &format!("refs/remotes/{remote}/{branch}"))? {
                copies.insert(remote, sha);
            }
        }
        Ok(copies)
    }

    /// How to check `branch` out at `repo_dir`, refusing before anything changes.
    fn plan_checkout(&self, bare: &Path, branch: &str, from_branch: Option<&str>, repo_dir: &Path) -> Result<Checkout, Fail> {
        let local = self.ref_sha(bare, &format!("refs/heads/{branch}"))?;
        let copies = self.remote_copies(bare, branch)?;
        let wt = path_str(repo_dir);

        if local.is_none() && copies.is_empty() {
            let base = match from_branch {
                Some(b) => b.to_string(),
                None => self.default_branch(bare)?,
            };
            let Some(start) = self.ref_sha(bare, &base)? else {
                return Err(Fail::value(format!("from_branch '{base}' does not name a commit in this project")));
            };
            return Ok(Checkout { add_args: vec![wt, "-b".into(), branch.into(), base.clone()], upstream: None, made_at: Some(start), note: format!("new branch {branch} from {base}") });
        }

        let mut picked: Option<String> = None;
        if let Some(fb) = from_branch {
            picked = copies.keys().find(|r| fb == format!("{r}/{branch}")).cloned();
            if picked.is_none() {
                let named = if copies.is_empty() { "none, no remote carries it".to_string() } else { copies.keys().map(|r| format!("{r}/{branch}")).collect::<Vec<_>>().join(", ") };
                return Err(Fail::value(format!(
                    "Branch '{branch}' already exists, so from_branch can only pick which remote copy it tracks ({named}), not '{fb}'"
                )));
            }
        }
        let ambiguous = || {
            let listed: Vec<String> = copies.iter().map(|(r, sha)| format!("{r}/{branch} at {}", &sha[..sha.len().min(12)])).collect();
            Fail::value(format!("Branch '{branch}' is on several remotes ({}). Pass from_branch='<remote>/{branch}' to pick the one it tracks.", listed.join(", ")))
        };

        if let Some(local) = local {
            if let Some(holder) = self.branch_checkouts(bare)?.get(branch) {
                if holder != repo_dir {
                    return Err(Fail::exists(format!("Branch '{branch}' is already checked out at {}", holder.display())));
                }
            }
            let r = self.git_in(bare, &["for-each-ref", "--format=%(upstream:short)", &format!("refs/heads/{branch}")])?;
            let current = if r.ok() { r.stdout.trim().to_string() } else { String::new() };
            let upstream = if let Some(p) = &picked {
                Some(format!("{p}/{branch}"))
            } else if !current.is_empty() {
                None
            } else if copies.len() == 1 {
                Some(format!("{}/{branch}", copies.keys().next().expect("one copy")))
            } else if !copies.is_empty() {
                return Err(ambiguous());
            } else {
                None
            };
            let tracking = match (&upstream, current.is_empty()) {
                (Some(u), _) => format!("tracking {u}"),
                (None, false) => format!("tracking {current}"),
                (None, true) => "with no upstream".into(),
            };
            return Ok(Checkout { add_args: vec![wt, branch.into()], upstream, made_at: None, note: format!("existing branch {branch} at {}, {tracking}", &local[..local.len().min(12)]) });
        }

        let picked = match picked {
            Some(p) => p,
            None if copies.len() > 1 => return Err(ambiguous()),
            None => copies.keys().next().expect("a copy").clone(),
        };
        let sha = copies[&picked].clone();
        Ok(Checkout {
            add_args: vec!["--track".into(), "-b".into(), branch.into(), wt, format!("{picked}/{branch}")],
            upstream: None,
            made_at: Some(sha),
            note: format!("branch {branch} from {picked}/{branch}, tracking it"),
        })
    }

    /// Undo a failed create. The branch goes only if this call made it and it has not moved.
    fn roll_back_create(&self, bare: &Path, repo_dir: &Path, branch: &str, project: &str, scope: &str, made_at: Option<&str>) {
        if let Err(e) = self.cleanup_worktree(bare, repo_dir, true) {
            tracing::error!("scope {project}/{scope}: rollback left the worktree: {e}");
        }
        let mut fate = "kept";
        if let Some(sha) = made_at {
            match self.delete_branch(bare, branch, Some(sha)) {
                Ok(Some(_)) => fate = "deleted",
                Ok(None) => {}
                Err(e) => tracing::error!("scope {project}/{scope}: rollback kept branch {branch}: {e}"),
            }
        }
        let gone = self.write(|c| {
            if let Some(id) = db::agent_id(c, project, scope, true)? {
                c.execute("DELETE FROM agents WHERE id=?", [id])?;
            }
            Ok(())
        });
        if let Err(e) = gone {
            tracing::error!("scope {project}/{scope}: rollback kept the row: {e}");
        }
        tracing::warn!("scope {project}/{scope}: create failed; worktree rolled back, branch {branch} {fate}");
    }

    /// Create a scope: a worktree, its data view and its row. With `new_project` the project
    /// is created first, and `scope` may be omitted to create the project alone.
    pub fn create_scope(&self, req: &CreateReq) -> Result<Action, Fail> {
        validate_project(&req.project)?;
        let tags = crate::tags::normalise(&json!(req.tags.clone().unwrap_or_default()))?;
        match &req.scope {
            None if !req.new_project => {
                return Err(Fail::value("scope is required; omit it only with new_project=true, to create the project alone"));
            }
            None => {}
            Some(scope) => {
                validate_scope(scope)?;
                if scope.split('/').next() == Some(provision::SHARED_STORE_DIRNAME) {
                    return Err(Fail::value(format!(
                        "'{}' is reserved at the top of a project for its shared data store; pick another scope name",
                        provision::SHARED_STORE_DIRNAME
                    )));
                }
            }
        }
        self.locked(&req.project, || self.create_locked(req, &tags))
    }

    fn create_locked(&self, req: &CreateReq, tags: &[String]) -> Result<Action, Fail> {
        let bare = self.paths.bare(&req.project);
        if !bare.exists() {
            if req.project == VAGRANT_PROJECT {
                return Err(Fail::not_found(format!("Vagrant-scopes repo not initialized at {}. Run `awm vagrant-init` first.", bare.display())));
            }
            if !req.new_project {
                return Err(Fail::not_found(format!("Project '{}' not found (expected {}). Pass new_project=true to create it.", req.project, bare.display())));
            }
            let created = self.create_project(&req.project, req.clone_url.as_deref(), req.fork_url.as_deref())?;
            if req.scope.is_none() {
                let main = created.worktree_dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                if !tags.is_empty() {
                    self.set_tags(&req.project, None, &json!(tags))?;
                }
                let mut out = Action::new(&req.project, &main, "active", format!("Created project at projects/{} (main checkout {main})", req.project));
                out.session = Some(1);
                return Ok(out);
            }
        } else if req.scope.is_none() {
            return Err(Fail::exists(format!("Project '{}' already exists at {}", req.project, bare.display())));
        }
        let project = req.project.as_str();
        let scope = req.scope.as_deref().expect("checked above");
        let repo_dir = self.paths.projects.join(project).join(scope);
        let branch = req.branch_name.clone().filter(|b| !b.is_empty()).unwrap_or_else(|| format!("feat/{scope}"));

        let bare_s = path_str(&bare);
        let session = self.write(|c| {
            db::ensure_project(c, project, &bare_s, None)?;
            let prior: i64 = c.query_row(
                "SELECT COUNT(*) AS n FROM agents a JOIN projects p ON p.id = a.project_id WHERE p.name=? AND a.scope=?",
                [project, scope],
                |r| r.get(0),
            )?;
            if db::agent_id(c, project, scope, true)?.is_some() {
                return Err(Fail::exists(format!("Scope '{scope}' already has an active session in project '{project}'")));
            }
            Ok(prior + 1)
        })?;

        if let Some(hook) = crate::hook::find(self, project, scope)? {
            return self.run_create_hook(req, &hook, session, tags);
        }

        self.assert_branch_available(&bare, &branch)?;
        self.git_in(&bare, &["worktree", "prune"])?;
        let checkout = self.plan_checkout(&bare, &branch, req.from_branch.as_deref(), &repo_dir)?;
        if repo_dir.exists() {
            self.cleanup_worktree(&bare, &repo_dir, false)?;
        }

        let made = (|| -> Result<(), Fail> {
            let args: Vec<&str> = checkout.add_args.iter().map(String::as_str).collect();
            let mut argv = vec!["worktree", "add"];
            argv.extend(args);
            let r = self.git_in(&bare, &argv)?;
            if !r.ok() {
                return Err(Fail::runtime(format!("Failed to create worktree: {}", r.stderr)));
            }
            if let Some(up) = &checkout.upstream {
                let r = self.git_in(&repo_dir, &["branch", &format!("--set-upstream-to={up}"), &branch])?;
                if !r.ok() {
                    return Err(Fail::runtime(format!("Failed to set upstream {up}: {}", r.stderr)));
                }
            }
            self.register_scope(project, scope, &repo_dir, &branch, project == VAGRANT_PROJECT)
        })();
        if let Err(e) = made {
            self.shielded().roll_back_create(&bare, &repo_dir, &branch, project, scope, checkout.made_at.as_deref());
            return Err(e);
        }
        if !tags.is_empty() {
            self.set_tags(project, Some(scope), &json!(tags))?;
        }
        let warning = self.set_brief(project, scope, req.context.as_deref());
        if let Err(e) = self.after_create(true) {
            tracing::warn!("scope {project}/{scope}: hygiene count failed: {e}");
        }
        let mut out = Action::new(
            project,
            scope,
            "active",
            format!(
                "Created scope at projects/{project}/{scope} on {}, session {session}{}",
                checkout.note,
                warning.map(|w| format!("; {w}")).unwrap_or_default()
            ),
        );
        out.session = Some(session);
        Ok(out)
    }

    /// Reconcile an on-disk worktree with a missing agents row.
    pub fn repair_scope(&self, project: &str, scope: &str) -> Result<Action, Fail> {
        validate_project(project)?;
        validate_scope(scope)?;
        self.locked(project, || {
            let bare = self.paths.bare(project);
            if !bare.exists() {
                return Err(Fail::not_found(format!("Project '{project}' has no bare repo at {} — nothing to repair against", bare.display())));
            }
            let repo_dir = self.paths.projects.join(project).join(scope);
            if !repo_dir.is_dir() {
                return Err(Fail::not_found(format!("Worktree not found at {}", repo_dir.display())));
            }
            if !repo_dir.join(".git").exists() {
                return Err(Fail::not_found(format!("{} is not a git worktree; nothing to repair", repo_dir.display())));
            }
            let r = self.git_in(&repo_dir, &["branch", "--show-current"])?;
            let branch = r.stdout.trim().to_string();
            if !r.ok() || branch.is_empty() {
                let why = if r.stderr.is_empty() { "empty branch name".to_string() } else { r.stderr.clone() };
                return Err(Fail::runtime(format!("Could not read branch from worktree {}: {why}", repo_dir.display())));
            }
            let (bare_s, wt) = (path_str(&bare), path_str(&repo_dir));
            let done = self.write(|c| {
                db::ensure_project(c, project, &bare_s, None)?;
                if let Some(existing) = db::agent_id(c, project, scope, true)? {
                    return Ok(Err(existing));
                }
                let aid = db::ensure_agent(c, project, scope, &NewAgent { branch: Some(&branch), worktree: &wt, is_vagrant: project == VAGRANT_PROJECT, ..NewAgent::default() })?;
                Ok(Ok(aid))
            })?;
            Ok(match done {
                Err(existing) => Action::new(project, scope, "skipped", format!("Scope {project}/{scope} already has an active agent row ({existing}); nothing to repair")),
                Ok(aid) => Action::new(
                    project,
                    scope,
                    "repaired",
                    format!("Backfilled agents row for {project}/{scope} from on-disk worktree ({}) on branch {branch}, agent_id={aid}", repo_dir.display()),
                ),
            })
        })
    }

    /// Complete (retire) a scope; optionally merge it into the default branch and remove its
    /// worktree.
    pub fn complete_scope(&self, project: &str, scope: &str, merge: bool, cleanup: bool, force: bool, delete_branch: bool) -> Result<Action, Fail> {
        validate_project(project)?;
        validate_scope(scope)?;
        self.locked(project, || {
            let bare = self.paths.bare(project);
            let repo_dir = self.paths.projects.join(project).join(scope);
            if !bare.exists() {
                return Err(Fail::not_found(format!("Bare repository not found at {}", bare.display())));
            }
            let branch = self.scope_branch(project, scope, Some(&repo_dir))?;
            if delete_branch && !cleanup {
                return Err(Fail::value("delete_branch needs cleanup=true: git cannot delete a branch its worktree still has checked out"));
            }
            let mut guard = None;
            if cleanup {
                let removal = self.removal_guard(&bare, &repo_dir, project, scope, &branch, force, !merge)?;
                let pending = self.worktree_guard(&bare, &repo_dir, force)?;
                guard = Some((pending, removal));
            }
            let mut merge_msg = String::new();
            if merge {
                let default = self.default_branch(&bare)?;
                let main = self.paths.projects.join(project).join(&default);
                if !main.exists() {
                    return Err(Fail::not_found(format!("Main worktree not found at {}", main.display())));
                }
                self.git_in(&main, &["checkout", &default])?;
                let r = self.git_in(&main, &["merge", &branch, "-m", &format!("Merge {branch} into {default}")])?;
                if !r.ok() {
                    return Err(Fail::runtime(format!("Merge failed: {}", r.stderr)));
                }
                self.git_in(&main, &["push"])?;
                merge_msg = format!(", merged into {default}");
            }
            self.write(|c| {
                if let Some(id) = db::agent_id(c, project, scope, true)? {
                    db::retire_agent(c, &id)?;
                }
                Ok(())
            })?;
            let mut cleanup_msg = String::new();
            let mut discarded = None;
            if let Some((pending, removal)) = guard {
                self.remove_worktree(&bare, &repo_dir)?;
                cleanup_msg = format!(", worktree removed, {}", self.settle_branch(&bare, &branch, delete_branch)?);
                if force {
                    discarded = discard_list(&pending, &removal);
                }
            }
            let mut out = Action::new(project, scope, "completed", format!("Scope completed{merge_msg}{cleanup_msg}"));
            out.discarded = discarded;
            Ok(out)
        })
    }

    /// Bring a scope's branch up to a base branch by merge or rebase.
    pub fn sync_scope(&self, project: &str, scope: &str, strategy: &str, from_branch: Option<&str>) -> Result<Action, Fail> {
        validate_project(project)?;
        validate_scope(scope)?;
        self.locked(project, || {
            let bare = self.paths.bare(project);
            let repo_dir = self.paths.projects.join(project).join(scope);
            let branch = self.scope_branch(project, scope, Some(&repo_dir))?;
            if !bare.exists() {
                return Err(Fail::not_found(format!("Bare repository not found at {}", bare.display())));
            }
            if !repo_dir.exists() {
                return Err(Fail::not_found(format!("Worktree not found at {}", repo_dir.display())));
            }
            let base = match from_branch.filter(|b| !b.is_empty()) {
                Some(b) => b.to_string(),
                None => self.default_branch(&bare)?,
            };
            if self.read(|c| db::agent_id(c, project, scope, true))?.is_none() {
                return Err(Fail::not_found(format!("No active scope '{scope}' found in project '{project}'")));
            }
            let r = self.git_in(&repo_dir, &["status", "--porcelain"])?;
            if !r.ok() {
                return Err(Fail::runtime(format!("git status failed: {}", r.stderr)));
            }
            if !r.stdout.trim().is_empty() {
                return Err(Fail::runtime(format!("Worktree has uncommitted changes — refusing to sync:\n{}", r.stdout)));
            }
            let r = self.git_in(&repo_dir, &["rev-parse", "--abbrev-ref", "HEAD"])?;
            if !r.ok() {
                return Err(Fail::runtime(format!("git rev-parse failed: {}", r.stderr)));
            }
            let current = r.stdout.trim();
            if current != branch {
                return Err(Fail::runtime(format!("Worktree HEAD is on '{current}', expected '{branch}'. Check out the feature branch before syncing.")));
            }
            let r = if strategy == "merge" {
                self.git_in(&repo_dir, &["merge", &base, "-m", &format!("Sync {base} into {branch}")])?
            } else {
                self.git_in(&repo_dir, &["rebase", &base])?
            };
            if !r.ok() {
                let said = if r.stderr.is_empty() { &r.stdout } else { &r.stderr };
                return Err(Fail::runtime(format!(
                    "{strategy} failed: {said}\nWorktree left mid-{strategy}; resolve and re-run, or `git {strategy} --abort` to undo."
                )));
            }
            Ok(Action::new(project, scope, "active", format!("Synced {branch} with {base} via {strategy}")))
        })
    }

    /// Merge `source` into the branch checked out at `worktree`. A conflict is aborted, so
    /// the worktree is left clean and the batch goes on.
    fn merge_one(&self, worktree: &Path, scope: &str, branch: &str, source: &str, message: &str) -> Result<Value, Fail> {
        let r = self.git_in(worktree, &["merge", source, "-m", message])?;
        let out = format!("{}{}", r.stdout, r.stderr);
        let row = |result: &str, detail: &str| json!({"scope": scope, "branch": branch, "result": result, "detail": detail});
        if r.ok() {
            if out.contains("Already up to date") {
                return Ok(row("up_to_date", "already up to date"));
            }
            let t = out.trim();
            let detail = if t.is_empty() { "merged" } else { t.lines().last().unwrap_or("merged") };
            return Ok(row("merged", detail));
        }
        if self.git_in(worktree, &["rev-parse", "--verify", "-q", "MERGE_HEAD"])?.ok() {
            self.git_in(worktree, &["merge", "--abort"])?;
            return Ok(row("conflict", "merge conflict — aborted, worktree left clean"));
        }
        let said = if r.stderr.is_empty() { out.as_str() } else { r.stderr.as_str() };
        Ok(row("error", said.trim()))
    }

    /// A scope's branch and worktree, from its row, else `feat/<scope>` and its default path.
    fn record(&self, project: &str, scope: &str) -> Result<(String, PathBuf), Fail> {
        Ok(match self.agent_record(project, scope, false)? {
            Some(rec) => (rec.branch, PathBuf::from(rec.worktree)),
            None => (format!("feat/{scope}"), self.paths.projects.join(project).join(scope)),
        })
    }

    /// Fan-in: merge each peripheral's branch into the hub's, in the hub's worktree. Local
    /// only. A peripheral's data pins ride its branch, so this merges data too.
    pub fn gather(&self, project: &str, hub: &str, peripherals: &[String], strategy: &str) -> Result<Value, Fail> {
        validate_project(project)?;
        validate_scope(hub)?;
        if strategy != "merge" {
            return Err(Fail::value("gather only supports strategy='merge' (rebase is not meaningful for a shared hub)."));
        }
        self.locked(project, || {
            let bare = self.paths.bare(project);
            if !bare.exists() {
                return Err(Fail::not_found(format!("Bare repository not found at {}", bare.display())));
            }
            let (hub_branch, hub_wt) = self.record(project, hub)?;
            if !hub_wt.exists() {
                return Err(Fail::not_found(format!("Hub worktree not found at {}", hub_wt.display())));
            }
            let r = self.git_in(&hub_wt, &["status", "--porcelain"])?;
            if !r.ok() {
                return Err(Fail::runtime(format!("git status failed in hub worktree: {}", r.stderr)));
            }
            if !r.stdout.trim().is_empty() {
                return Err(Fail::runtime(format!("Hub worktree {} has uncommitted changes — refusing to gather:\n{}", hub_wt.display(), r.stdout)));
            }
            let current = self.git_in(&hub_wt, &["rev-parse", "--abbrev-ref", "HEAD"])?.stdout.trim().to_string();
            if current != hub_branch {
                return Err(Fail::runtime(format!(
                    "Hub worktree HEAD is on '{current}', expected hub branch '{hub_branch}'. Check out the hub branch before gathering."
                )));
            }
            let mut results = vec![];
            for p in peripherals {
                validate_scope(p)?;
                let (pb, _) = self.record(project, p)?;
                if !self.git_in(&bare, &["rev-parse", "--verify", "-q", &format!("refs/heads/{pb}")])?.ok() {
                    results.push(json!({"scope": p, "branch": pb, "result": "skipped", "detail": format!("branch {pb} not found")}));
                    continue;
                }
                results.push(self.merge_one(&hub_wt, p, &pb, &pb, &format!("Gather {pb} into {hub_branch}"))?);
            }
            Ok(fan(project, hub, &hub_branch, "gather", results))
        })
    }

    /// Fan-out: merge the hub's branch into each peripheral's, in its own worktree. A dirty
    /// or off-branch peripheral is skipped.
    pub fn scatter(&self, project: &str, hub: &str, peripherals: &[String], strategy: &str) -> Result<Value, Fail> {
        validate_project(project)?;
        validate_scope(hub)?;
        if strategy != "merge" {
            return Err(Fail::value("scatter only supports strategy='merge' (rebase is not meaningful for fan-out)."));
        }
        self.locked(project, || {
            let bare = self.paths.bare(project);
            if !bare.exists() {
                return Err(Fail::not_found(format!("Bare repository not found at {}", bare.display())));
            }
            let (hub_branch, _) = self.record(project, hub)?;
            if !self.git_in(&bare, &["rev-parse", "--verify", "-q", &format!("refs/heads/{hub_branch}")])?.ok() {
                return Err(Fail::not_found(format!("Hub branch '{hub_branch}' not found")));
            }
            let mut results = vec![];
            for p in peripherals {
                validate_scope(p)?;
                let (pb, pwt) = self.record(project, p)?;
                let skip = |detail: String| json!({"scope": p, "branch": pb, "result": "skipped", "detail": detail});
                if !pwt.exists() {
                    results.push(skip(format!("worktree not found at {}", pwt.display())));
                    continue;
                }
                let r = self.git_in(&pwt, &["status", "--porcelain"])?;
                if !r.ok() || !r.stdout.trim().is_empty() {
                    results.push(skip("worktree dirty — skipped".into()));
                    continue;
                }
                let current = self.git_in(&pwt, &["rev-parse", "--abbrev-ref", "HEAD"])?.stdout.trim().to_string();
                if current != pb {
                    results.push(skip(format!("HEAD on '{current}', expected '{pb}'")));
                    continue;
                }
                results.push(self.merge_one(&pwt, p, &pb, &hub_branch, &format!("Scatter {hub_branch} into {pb}"))?);
            }
            Ok(fan(project, hub, &hub_branch, "scatter", results))
        })
    }

    /// Delete a scope: its worktree goes and its row retires. The branch stays unless asked.
    pub fn delete_scope(&self, project: &str, scope: &str, force: bool, delete_branch: bool) -> Result<Action, Fail> {
        validate_project(project)?;
        validate_scope(scope)?;
        self.locked(project, || {
            let bare = self.paths.bare(project);
            let repo_dir = self.paths.projects.join(project).join(scope);
            let branch = self.scope_branch(project, scope, Some(&repo_dir))?;
            if !bare.exists() {
                return Err(Fail::not_found(format!("Bare repository not found at {}", bare.display())));
            }
            let row: Option<String> = self.read(|c| {
                Ok(c.query_row(
                    "SELECT a.id, a.status FROM agents a JOIN projects p ON p.id = a.project_id \
                     WHERE p.name=? AND a.scope=? AND a.status IN ('allocated','active','retired') ORDER BY a.created_at DESC LIMIT 1",
                    [project, scope],
                    |r| r.get(0),
                )
                .optional()?)
            })?;
            let Some(id) = row else {
                return Err(Fail::not_found(format!("No scope '{scope}' found in project '{project}'")));
            };
            let removal = self.removal_guard(&bare, &repo_dir, project, scope, &branch, force, true)?;
            let pending = self.cleanup_worktree(&bare, &repo_dir, force)?;
            let branch_msg = self.settle_branch(&bare, &branch, delete_branch)?;
            self.write(|c| {
                c.execute("UPDATE agents SET status='retired', retired_at=? WHERE id=?", awm_store::rusqlite::params![db::now_ms(), id])?;
                Ok(())
            })?;
            self.drop_tags(project, scope)?;
            let mut out = Action::new(project, scope, "deleted", format!("Scope deleted, worktree removed, {branch_msg}"));
            if force {
                out.discarded = discard_list(&pending, &removal);
            }
            Ok(out)
        })
    }
}

/// How create gets the scope's branch into its worktree.
struct Checkout {
    /// After `git worktree add`.
    add_args: Vec<String>,
    /// `<remote>/<branch>` to set after the add.
    upstream: Option<String>,
    /// The SHA a branch this call creates starts at; `None` when it exists already.
    made_at: Option<String>,
    note: String,
}

fn fan(project: &str, hub: &str, hub_branch: &str, direction: &str, results: Vec<Value>) -> Value {
    let mut summary = Map::new();
    for r in &results {
        let key = r["result"].as_str().unwrap_or("").to_string();
        let n = summary.get(&key).and_then(Value::as_i64).unwrap_or(0);
        summary.insert(key, json!(n + 1));
    }
    json!({"project": project, "hub": hub, "hub_branch": hub_branch, "direction": direction, "results": results,
           "data_results": null, "data_summary": null, "summary": summary})
}

/// Python's `Path.resolve()`: canonical where it exists, else made absolute and normalised.
pub fn resolve(p: &Path) -> PathBuf {
    if let Ok(real) = p.canonicalize() {
        return real;
    }
    // Resolve the longest existing ancestor and append the rest.
    let mut tail = vec![];
    let mut cur = p.to_path_buf();
    loop {
        if let Ok(real) = cur.canonicalize() {
            let mut out = real;
            for part in tail.iter().rev() {
                out.push(part);
            }
            return out;
        }
        match (cur.file_name().map(|n| n.to_os_string()), cur.parent()) {
            (Some(name), Some(parent)) => {
                tail.push(name);
                cur = parent.to_path_buf();
            }
            _ => return p.to_path_buf(),
        }
    }
}

/// Remove the empty container directories a nested scope's removal left, up to but never
/// including `stop_at`. `rmdir` refuses anything that still holds a file.
fn prune_empty_parents(stop_at: &Path, path: &Path) {
    let stop = resolve(stop_at);
    let Ok(mut current) = path.canonicalize() else { return };
    while current != stop && current.starts_with(&stop) {
        if std::fs::remove_dir(&current).is_err() {
            return;
        }
        match current.parent() {
            Some(p) => current = p.to_path_buf(),
            None => return,
        }
    }
}

/// Pinned paths whose data on disk differs from its pin (scopes.md R44 item 3). Their bytes
/// are in no cache yet, so a delete would lose them.
pub fn dvc_drift(worktree: &Path) -> Vec<String> {
    if !provision::is_dvc_repo(worktree) || provision::chunk_pins(worktree).is_empty() {
        return vec![];
    }
    let changed = match awm_dvcdata::status::changed_outs(worktree, None) {
        Ok(c) => c,
        Err(e) => return vec![format!("dvc status failed: {}", awm_pycompat::pystr::head(&e.to_string(), 200))],
    };
    let mut out: Vec<String> = changed
        .values()
        .flat_map(|outs| outs.iter())
        .filter(|(path, state)| LOSABLE.contains(state) && (**state != State::NotInCache || worktree.join(path).exists()))
        .map(|(path, state)| format!("{path} ({})", state.as_str()))
        .collect();
    out.sort();
    out
}

/// The repr Python gives a list of strings.
pub fn str_list_repr(items: &[String]) -> String {
    format!("[{}]", items.iter().map(|s| str_repr(s)).collect::<Vec<_>>().join(", "))
}
