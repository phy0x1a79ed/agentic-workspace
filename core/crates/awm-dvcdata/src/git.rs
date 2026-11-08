//! The git calls the data layer makes. A daemon has no global git identity, so every call
//! carries a fallback one.

use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

pub fn command(repo: &Path) -> Command {
    let mut c = Command::new("git");
    c.arg("-C").arg(repo).stdin(Stdio::null());
    for (k, v) in [("GIT_AUTHOR_NAME", "awm"), ("GIT_AUTHOR_EMAIL", "awm@localhost"), ("GIT_COMMITTER_NAME", "awm"), ("GIT_COMMITTER_EMAIL", "awm@localhost")] {
        if std::env::var_os(k).is_none() {
            c.env(k, v);
        }
    }
    c
}

pub fn run(repo: &Path, args: &[&str]) -> std::io::Result<Output> {
    command(repo).args(args).output()
}

/// Trimmed stdout of a git call that succeeded.
pub fn out(repo: &Path, args: &[&str]) -> Option<String> {
    let o = run(repo, args).ok()?;
    o.status.success().then(|| String::from_utf8_lossy(&o.stdout).trim().to_string())
}

/// The common git dir, which every worktree of a project shares.
pub fn common_dir(repo: &Path) -> Option<PathBuf> {
    let p = PathBuf::from(out(repo, &["rev-parse", "--git-common-dir"])?);
    let p = if p.is_absolute() { p } else { repo.join(p) };
    Some(std::fs::canonicalize(&p).unwrap_or(p))
}

pub fn git_dir(repo: &Path) -> Option<PathBuf> {
    out(repo, &["rev-parse", "--absolute-git-dir"]).filter(|s| !s.is_empty()).map(PathBuf::from)
}

pub fn head_rev(repo: &Path) -> String {
    out(repo, &["rev-parse", "HEAD"]).unwrap_or_default()
}

pub fn current_branch(repo: &Path) -> String {
    out(repo, &["branch", "--show-current"]).unwrap_or_default()
}

/// `git ls-files -z <flags> -- <specs>`, or `None` when git fails.
pub fn ls_files(repo: &Path, flags: &[&str], specs: &[&str]) -> Option<Vec<String>> {
    let mut args = vec!["ls-files", "-z"];
    args.extend_from_slice(flags);
    args.push("--");
    args.extend_from_slice(specs);
    let o = run(repo, &args).ok()?;
    if !o.status.success() {
        return None;
    }
    let mut v: Vec<String> = String::from_utf8_lossy(&o.stdout).split('\0').filter(|s| !s.is_empty()).map(str::to_string).collect();
    v.dedup();
    Some(v)
}

/// Every `.dvc` pin tracked in this worktree, repo-relative and sorted. An untracked pin
/// is someone's work in progress, not part of what the commit pins.
pub fn chunk_pins(repo: &Path) -> Vec<String> {
    let mut v = ls_files(repo, &[], &["*.dvc"]).unwrap_or_default();
    v.sort();
    v
}

/// How many parents HEAD has.
pub fn head_parents(repo: &Path) -> usize {
    out(repo, &["rev-list", "--parents", "-n", "1", "HEAD"]).map(|s| s.split_whitespace().count().saturating_sub(1)).unwrap_or(0)
}
