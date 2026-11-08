//! The scope hygiene check (scopes.md R6, R45). Every 10th new scope triggers one pass; the
//! count is its only trigger. A pass removes a scope's worktree only when the delete guard
//! needs no `force`, the branch is merged, and no session used the scope for the idle
//! period. It keeps every branch and posts one report. Projects with a create hook are
//! skipped: their scopes are not worktrees this pass understands.

use std::path::Path;
use std::time::Duration;

use serde_json::{json, Value};

use crate::{Fail, Op};

pub const EVERY: i64 = 10;
pub const IDLE_DAYS: f64 = 14.0;
/// Branches that are the project's own lines, never a scope to clean up.
pub const LINES: [&str; 4] = ["dev", "release", "main", "master"];
pub const COUNTER: &str = "scope_creates";

fn now_s() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

impl Op {
    /// The line `branch` is merged into, among release, dev and the default branch.
    fn merged_into(&self, bare: &Path, branch: &str) -> Result<Option<String>, Fail> {
        let Some(tip) = self.ref_sha(bare, &format!("refs/heads/{branch}"))? else { return Ok(None) };
        let lines = ["release".to_string(), "dev".to_string(), self.default_branch(bare)?];
        let refs = self.git_in(bare, &["for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"])?;
        let own = format!("refs/heads/{branch}");
        for r in refs.stdout.split_whitespace() {
            let short = match r.strip_prefix("refs/remotes/") {
                Some(rest) => rest.split_once('/').map(|(_, b)| b).unwrap_or(rest),
                None => r.strip_prefix("refs/heads/").unwrap_or(r),
            };
            if lines.iter().any(|l| l == short) && r != own && self.git_in(bare, &["merge-base", "--is-ancestor", &tip, r])?.ok() {
                return Ok(Some(r.to_string()));
            }
        }
        Ok(None)
    }

    /// When a session last used the scope, in epoch seconds. A scope never claimed falls
    /// back to the newest of its last commit and its worktree's `.git` file.
    fn idle_since(&self, project: &str, scope: &str, worktree: &Path, bare: &Path, branch: &str) -> Result<f64, Fail> {
        if let Some(used) = self.last_used(project, scope)? {
            return Ok(used as f64 / 1000.0);
        }
        let mut stamps = vec![];
        let r = self.git_in(bare, &["log", "-1", "--format=%ct", &format!("refs/heads/{branch}")])?;
        let t = r.stdout.trim();
        if r.ok() && !t.is_empty() && t.chars().all(|c| c.is_ascii_digit()) {
            stamps.push(t.parse::<f64>().unwrap_or(0.0));
        }
        if let Ok(m) = std::fs::metadata(worktree.join(".git")) {
            if let Ok(mt) = m.modified() {
                stamps.push(mt.duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0));
            }
        }
        Ok(stamps.into_iter().fold(0.0, f64::max))
    }

    /// One pass over every active scope on this node.
    pub fn hygiene_check(&self, now: Option<f64>, idle_days: f64, dry_run: bool) -> Result<Value, Fail> {
        let now = now.unwrap_or_else(now_s);
        let rows: Vec<(String, String, String)> = self.read(|c| {
            let mut stmt = c.prepare(
                "SELECT p.name AS project, a.scope, a.worktree, a.branch FROM agents a JOIN projects p ON p.id = a.project_id \
                 WHERE a.status IN ('allocated','active') AND a.is_vagrant = 0 ORDER BY p.name, a.scope",
            )?;
            let it = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(3)?)))?;
            Ok(it.collect::<Result<_, _>>()?)
        })?;
        let mut removed = vec![];
        let mut held = vec![];
        for (project, scope, branch) in rows {
            let hold = |held: &mut Vec<Value>, why: String| held.push(json!({"project": project, "scope": scope, "why": why}));
            let bare = self.paths.bare(&project);
            let worktree = self.paths.projects.join(&project).join(&scope);
            if !bare.is_dir() || !worktree.is_dir() {
                continue;
            }
            if crate::hook::config(self, &project)?.is_some() {
                continue;
            }
            if LINES.contains(&branch.as_str()) || branch == self.default_branch(&bare)? {
                continue;
            }
            if let Some(holder) = self.live_holder(&project, &scope)? {
                hold(&mut held, format!("in use by session {}", crate::args::py_str(&holder["session_id"])));
                continue;
            }
            let idle = (now - self.idle_since(&project, &scope, &worktree, &bare, &branch)?) / 86400.0;
            if idle < idle_days {
                hold(&mut held, format!("used {idle:.0} day(s) ago"));
                continue;
            }
            if self.merged_into(&bare, &branch)?.is_none() {
                hold(&mut held, format!("branch {branch} is not merged into release, dev or the default branch"));
                continue;
            }
            let status = self.git_in(&worktree, &["status", "--porcelain"])?;
            if !status.stdout.trim().is_empty() {
                hold(&mut held, "uncommitted or untracked files".into());
                continue;
            }
            let drift = crate::lifecycle::dvc_drift(&worktree);
            if !drift.is_empty() {
                hold(&mut held, format!("data differs from its pin ({})", drift[..drift.len().min(3)].join(", ")));
                continue;
            }
            if dry_run {
                removed.push(json!({"project": project, "scope": scope, "branch": branch, "dry_run": true}));
                continue;
            }
            match self.delete_scope(&project, &scope, false, false) {
                Ok(_) => removed.push(json!({"project": project, "scope": scope, "branch": branch})),
                Err(e) if e.class == "RuntimeError" || e.is_os_error() => hold(&mut held, format!("delete refused: {}", e.message)),
                Err(e) => return Err(e),
            }
        }
        let report = json!({"removed": removed, "held": held, "dry_run": dry_run});
        if !dry_run && !(removed.is_empty() && held.is_empty()) {
            self.post_hygiene(&report);
        }
        Ok(report)
    }

    /// Write the report to the board's Ops book. Best effort: the pass already ran.
    fn post_hygiene(&self, report: &Value) {
        let empty = vec![];
        let removed = report["removed"].as_array().unwrap_or(&empty);
        let held = report["held"].as_array().unwrap_or(&empty);
        let s = |v: &Value, k: &str| crate::args::py_str(&v[k]);
        let mut lines = vec![format!("Removed {} worktree(s); branches kept.", removed.len())];
        lines.extend(removed.iter().map(|r| format!("- removed {}/{} (restore: scope create on branch {})", s(r, "project"), s(r, "scope"), s(r, "branch"))));
        lines.extend(held.iter().map(|h| format!("- held {}/{}: {}", s(h, "project"), s(h, "scope"), s(h, "why"))));
        let args = json!({"kind": "scope-hygiene", "node": self.node, "title": format!("Scope hygiene: {} removed, {} held", removed.len(), held.len()), "body": lines.join("\n")});
        if let Err(e) = self.out.call("operations", "ops_post", args, Duration::from_secs(60)) {
            tracing::warn!("scopes: hygiene report not posted: {e}");
        }
    }

    /// A pass that never fails the caller: its outcome goes to the log.
    pub fn hygiene_run(&self) {
        match self.hygiene_check(None, IDLE_DAYS, false) {
            Ok(r) => tracing::info!(
                "scopes: hygiene removed {}, held {}",
                r["removed"].as_array().map_or(0, Vec::len),
                r["held"].as_array().map_or(0, Vec::len)
            ),
            Err(e) => tracing::error!("scopes: hygiene pass failed: {e}"),
        }
    }

    /// Count a new scope. Every `EVERY`th one starts a pass, off the request path when
    /// `background`.
    pub fn after_create(&self, background: bool) -> Result<i64, Fail> {
        self.after_create_with(background, |op: Op| op.hygiene_run())
    }

    /// [`Op::after_create`] with the pass it starts.
    pub fn after_create_with(&self, background: bool, pass: impl FnOnce(Op) + Send + 'static) -> Result<i64, Fail> {
        let n = self.bump(COUNTER)?;
        if n % EVERY == 0 {
            let op = self.shielded();
            if background {
                std::thread::Builder::new().name("scope-hygiene".into()).spawn(move || pass(op)).map_err(|e| Fail::runtime(e.to_string()))?;
            } else {
                pass(op);
            }
        }
        Ok(n)
    }

    pub fn hygiene_status(&self) -> Result<Value, Fail> {
        let n: i64 = self.read(|c| {
            use awm_store::rusqlite::OptionalExtension;
            Ok(c.query_row("SELECT value FROM scope_counters WHERE name=?", [COUNTER], |r| r.get(0)).optional()?.unwrap_or(0))
        })?;
        Ok(json!({"creates": n, "next_check_after": EVERY - n.rem_euclid(EVERY)}))
    }
}
