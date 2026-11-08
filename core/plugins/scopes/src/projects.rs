//! Projects: creation (a bare repo, its main checkout and data dirs) and the listing `search`
//! returns beside the scopes.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::lifecycle::VAGRANT_PROJECT;
use crate::names::validate_project;
use crate::{path_str, Fail, Op};

/// git's stderr signatures for a missing or rejected credential, in priority order.
const AUTH_SIGNATURES: [&str; 5] = [
    "Permission denied (publickey)",
    "could not read Username",
    "Authentication failed",
    "fatal: Authentication",
    "remote: Repository not found",
];

/// `ProjectCreateResponse`.
#[derive(Debug, Clone)]
pub struct Created {
    pub name: String,
    pub bare_dir: PathBuf,
    pub worktree_dir: PathBuf,
    pub data_dir: PathBuf,
    pub mode: &'static str,
}

/// One project as `search` lists it, with its scope counts per status.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ProjectInfo {
    pub name: String,
    pub active: i64,
    pub completed: i64,
    pub deleted: i64,
    pub private: bool,
    pub tags: Vec<String>,
}

impl ProjectInfo {
    pub fn to_json(&self) -> Value {
        json!({"name": self.name, "scope_counts": {"active": self.active, "completed": self.completed, "deleted": self.deleted},
               "private": self.private, "tags": self.tags})
    }
}

/// A scratch directory removed when dropped.
struct Scratch(PathBuf);

impl Scratch {
    fn new() -> Result<Self, Fail> {
        let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
        let p = std::env::temp_dir().join(format!("awm-scopes-{}-{nanos}", std::process::id()));
        std::fs::create_dir_all(&p).map_err(|e| crate::os_fail(&e, &p))?;
        Ok(Self(p))
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

impl Op {
    /// `git clone --bare`, authenticating a GitHub HTTPS clone with the gh token through a
    /// credential helper set for this one command.
    fn clone_bare(&self, url: &str, bare: &Path) -> Result<(), Fail> {
        let bare_s = path_str(bare);
        let mut argv = vec!["git"];
        if self.gh.is_some() && url.starts_with("https://github.com/") {
            argv.extend(["-c", "credential.https://github.com.helper=", "-c", "credential.https://github.com.helper=!gh auth git-credential"]);
        }
        argv.extend(["clone", "--bare", url, &bare_s]);
        let r = self.run(&argv, None)?;
        if r.ok() {
            return Ok(());
        }
        let stderr = r.stderr.trim();
        let url_r = awm_pycompat::repr::str_repr(url);
        if AUTH_SIGNATURES.iter().any(|s| stderr.contains(s)) {
            return Err(Fail::runtime(format!(
                "git clone of {url_r} failed on credentials. For a private GitHub repo, pass an https://github.com/… URL and make sure `gh auth status` shows a login with 'repo' scope (the daemon authenticates the clone with that gh token, read from ~/.config/gh/hosts.yml). For a non-GitHub or git@…:/SSH remote, the daemon instead needs an ssh-agent exported via SSH_AUTH_SOCK.\ngit said: {stderr}"
            )));
        }
        Err(Fail::runtime(format!("git clone of {url_r} failed (exit {}): {stderr}", r.code)))
    }

    /// Create a project: its bare repo (fresh, cloned or forked), data dirs and main checkout,
    /// registered as a scope.
    pub fn create_project(&self, name: &str, clone_url: Option<&str>, fork_url: Option<&str>) -> Result<Created, Fail> {
        validate_project(name)?;
        if name == VAGRANT_PROJECT {
            return Err(Fail::value(format!(
                "project name {} is reserved for vagrant scopes; use `awm vagrant-init` to bootstrap the unified vagrant repo",
                awm_pycompat::repr::str_repr(name)
            )));
        }
        let bare = self.paths.bare(name);
        if bare.exists() {
            return Err(Fail::exists(format!("Project '{name}' already exists at {}", bare.display())));
        }
        let clone_url = clone_url.filter(|u| !u.is_empty());
        let fork_url = fork_url.filter(|u| !u.is_empty());
        let mode = if clone_url.is_some() {
            "clone"
        } else if fork_url.is_some() {
            "fork"
        } else {
            "fresh"
        };
        let bare_s = path_str(&bare);
        match mode {
            "fresh" => {
                // `-b main` is load-bearing: the seed commit goes to `main`, and HEAD must name it.
                self.checked(&["git", "init", "--bare", "-b", "main", &bare_s])?;
                if let Some(gh) = self.gh.clone() {
                    let gh = path_str(&gh);
                    let user = self.github_user.clone();
                    self.checked(&[&gh, "repo", "create", &format!("{user}/{name}"), "--private"])?;
                    self.checked(&["git", "-C", &bare_s, "remote", "add", "origin", &format!("https://github.com/{user}/{name}.git")])?;
                }
                let tmp = Scratch::new()?;
                let init = path_str(&tmp.0.join("init"));
                self.git(&["clone", &bare_s, &init])?;
                self.git(&["-C", &init, "checkout", "-b", "main"])?;
                self.git(&["-C", &init, "commit", "--allow-empty", "-m", &format!("Initial commit for {name}")])?;
                self.checked(&["git", "-C", &init, "push", "origin", "main"])?;
            }
            "clone" => {
                self.clone_bare(clone_url.expect("clone mode"), &bare)?;
                self.git_in(&bare, &["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"])?;
            }
            _ => {
                let fork = fork_url.expect("fork mode");
                let Some(gh) = self.gh.clone() else {
                    return Err(Fail::runtime("gh CLI is required for --fork"));
                };
                self.run(&[&path_str(&gh), "repo", "fork", fork, "--clone=false"], None)?;
                let stem = Path::new(fork).file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
                self.clone_bare(&format!("https://github.com/{}/{stem}.git", self.github_user), &bare)?;
                self.git_in(&bare, &["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"])?;
                self.git_in(&bare, &["remote", "add", "upstream", fork])?;
            }
        }
        for d in ["raw", "staged"] {
            let p = self.paths.data.join(name).join(d);
            std::fs::create_dir_all(&p).map_err(|e| crate::os_fail(&e, &p))?;
        }
        let default = self.default_branch(&bare)?;
        let worktree = self.paths.projects.join(name).join(&default);
        if !worktree.exists() {
            let rel = format!("../{default}");
            let exists = self.git_in(&bare, &["rev-parse", "--verify", &format!("refs/heads/{default}")])?.ok();
            let r = if exists { self.git_in(&bare, &["worktree", "add", &rel, &default])? } else { self.git_in(&bare, &["worktree", "add", &rel, "-b", &default])? };
            if !r.ok() {
                return Err(Fail::runtime(format!("Failed to create worktree for {default}: {}", r.stderr)));
            }
        }
        self.register_scope(name, &default, &worktree, &default, false)?;
        Ok(Created { name: name.into(), bare_dir: bare, worktree_dir: worktree, data_dir: self.paths.data.join(name), mode })
    }

    /// Every project from the rows and the bare repos on disk, with its scope counts. A main
    /// checkout counts as one active scope.
    pub fn project_listing(&self) -> Result<Vec<ProjectInfo>, Fail> {
        let rows: Vec<(String, String, i64)> = self.read(|c| {
            let mut stmt = c.prepare("SELECT p.name AS project, a.status, COUNT(*) AS n FROM agents a JOIN projects p ON p.id = a.project_id GROUP BY p.name, a.status ORDER BY p.name")?;
            let it = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
            Ok(it.collect::<Result<_, _>>()?)
        })?;
        let mut by: BTreeMap<String, ProjectInfo> = BTreeMap::new();
        for (p, status, n) in rows {
            let e = by.entry(p.clone()).or_insert_with(|| ProjectInfo { name: p, ..ProjectInfo::default() });
            match status.as_str() {
                "allocated" | "active" => e.active += n,
                "retired" => e.completed += n,
                _ => {}
            }
        }
        for name in self.bare_projects() {
            by.entry(name.clone()).or_insert_with(|| ProjectInfo { name, ..ProjectInfo::default() });
        }
        for name in self.main_checkouts(None)?.into_keys() {
            by.entry(name.clone()).or_insert_with(|| ProjectInfo { name, ..ProjectInfo::default() }).active += 1;
        }
        Ok(by.into_values().map(|mut p| {
            p.private = awm_plugin_sdk::privacy::hidden(&p.name, None);
            p
        }).collect())
    }

    /// The projects for `search`: name matches first, then the projects whose scopes' briefs
    /// the query ranked, in the order of their best brief. `ranked` is that order.
    pub fn search_projects(&self, query: Option<&str>, limit: Option<usize>, ranked: Option<&[String]>) -> Result<Vec<ProjectInfo>, Fail> {
        let items = self.project_listing()?;
        let Some(q) = query.filter(|q| !q.is_empty()) else {
            return Ok(match limit {
                Some(l) => items.into_iter().take(l).collect(),
                None => items,
            });
        };
        let limit = limit.unwrap_or(usize::MAX);
        let q = awm_pycompat::pystr::lower(q);
        let mut merged: Vec<ProjectInfo> = items.iter().filter(|p| awm_pycompat::pystr::lower(&p.name).contains(&q)).take(limit).cloned().collect();
        let eligible: BTreeMap<&str, &ProjectInfo> = items.iter().map(|p| (p.name.as_str(), p)).collect();
        for name in ranked.unwrap_or(&[]) {
            if merged.len() >= limit {
                break;
            }
            if merged.iter().any(|m| &m.name == name) {
                continue;
            }
            if let Some(p) = eligible.get(name.as_str()) {
                merged.push((*p).clone());
            }
        }
        Ok(merged)
    }
}
