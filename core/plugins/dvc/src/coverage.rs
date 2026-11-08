//! What would be lost if this machine died. GitHub holds the code and chinook the DVC
//! cache; everything else is disposable by decision, and this report is how that decision
//! is checked. Per scope worktree it names uncommitted and untracked files, a branch with no
//! upstream or ahead of one, and pins whose objects the shared cache lacks. Read-only, no
//! network: `@{upstream}` is a recorded ref, not a fetch.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use awm_dvcdata::provision;
use awm_dvcdata::scope;
use serde_json::{json, Value};

use crate::Paths;

/// Long enough for a cold `git status` on a worktree with a large ignored scratch tree;
/// short enough that one wedged repo cannot hang the report.
const GIT_TIMEOUT: Duration = Duration::from_secs(120);
const SAMPLE: usize = 10;

/// Run git in `dir`; None on failure, so one unreadable repo degrades to one incomplete row.
fn git(dir: &Path, args: &[&str]) -> Option<String> {
    let mut child = Command::new("git").arg("-C").arg(dir).args(args).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().ok()?;
    let mut out = child.stdout.take()?;
    let reader = std::thread::spawn(move || {
        let mut buf = Vec::new();
        std::io::Read::read_to_end(&mut out, &mut buf).ok();
        buf
    });
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(st)) => {
                let buf = reader.join().unwrap_or_default();
                return st.success().then(|| String::from_utf8_lossy(&buf).into_owned());
            }
            Ok(None) if start.elapsed() > GIT_TIMEOUT => {
                let _ = child.kill();
                let _ = child.wait();
                tracing::warn!("git {} timed out in {}", args.join(" "), dir.display());
                return None;
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(_) => return None,
        }
    }
}

fn worktrees_of(bare: &Path) -> Option<Vec<PathBuf>> {
    let raw = git(bare, &["worktree", "list", "--porcelain"])?;
    Some(crate::scopedata::parse_worktrees(&raw).into_iter().map(|(p, _)| p).collect())
}

/// Scope worktrees under `projects/`, at whatever depth, from each project's bare repo;
/// a project with no readable bare repo falls back to its direct children with a `.git`.
pub fn find_worktrees(root: &Path) -> Vec<PathBuf> {
    let projects = root.join("projects");
    let Ok(rd) = std::fs::read_dir(&projects) else { return vec![] };
    let mut dirs: Vec<PathBuf> = rd.flatten().map(|e| e.path()).filter(|p| p.is_dir()).collect();
    dirs.sort();
    let mut out = vec![];
    for project in dirs {
        let bare = project.join(".bare");
        let listed = if bare.is_dir() { worktrees_of(&bare) } else { None };
        match listed {
            Some(l) => out.extend(l),
            None => {
                let mut kids: Vec<PathBuf> = std::fs::read_dir(&project).map(|rd| rd.flatten().map(|e| e.path()).collect()).unwrap_or_default();
                kids.sort();
                out.extend(kids.into_iter().filter(|s| s.is_dir() && s.join(".git").exists()));
            }
        }
    }
    out.sort();
    out.dedup();
    out
}

fn uncommitted(wt: &Path) -> Value {
    let Some(raw) = git(wt, &["status", "--porcelain=v1", "--untracked-files=normal"]) else {
        return json!({"error": "git status failed"});
    };
    let (mut modified, mut untracked) = (vec![], vec![]);
    for line in raw.lines() {
        if line.get(2..).is_none_or(|r| r.trim().is_empty()) {
            continue;
        }
        let name = line.get(3..).unwrap_or("").trim().to_string();
        if line.starts_with("??") {
            untracked.push(name);
        } else {
            modified.push(name);
        }
    }
    json!({
        "modified": modified.len(),
        "untracked": untracked.len(),
        "modified_sample": &modified[..modified.len().min(SAMPLE)],
        "untracked_sample": &untracked[..untracked.len().min(SAMPLE)],
    })
}

fn unpushed(wt: &Path) -> Value {
    let branch = git(wt, &["rev-parse", "--abbrev-ref", "HEAD"]).unwrap_or_default().trim().to_string();
    if branch.is_empty() || branch == "HEAD" {
        return json!({"branch": if branch.is_empty() { Value::Null } else { json!(branch) }, "detached": true});
    }
    let Some(counts) = git(wt, &["rev-list", "--left-right", "--count", &format!("{branch}...{branch}@{{upstream}}")]) else {
        return json!({"branch": branch, "no_upstream": true});
    };
    let counts = counts.trim();
    let (ahead, behind) = counts.split_once('\t').unwrap_or((counts, ""));
    json!({
        "branch": branch,
        "no_upstream": false,
        "ahead": ahead.trim().parse::<i64>().unwrap_or(0),
        "behind": behind.trim().parse::<i64>().unwrap_or(0),
    })
}

fn pins(wt: &Path, cache: &Path) -> Value {
    if !wt.join(".dvc").is_dir() {
        return json!({"dvc": false});
    }
    if provision::no_remote(wt) {
        return json!({"dvc": true, "no_remote": true});
    }
    let res = scope::resolve(wt, cache);
    json!({
        "dvc": true,
        "pins": res.pins,
        "objects_known": res.objects.len(),
        "missing": res.missing.len(),
        "unresolved_manifests": res.unresolved.len(),
    })
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().unwrap_or(0.0) != 0.0,
        Value::String(s) => !s.is_empty(),
        _ => true,
    }
}

fn at_risk(row: &Value) -> bool {
    let k = |sec: &str, key: &str| row[sec].get(key).is_some_and(truthy);
    k("uncommitted", "modified")
        || k("uncommitted", "untracked")
        || k("uncommitted", "error")
        || k("unpushed", "detached")
        || k("unpushed", "no_upstream")
        || k("unpushed", "ahead")
        || k("pins", "missing")
        || k("pins", "unresolved_manifests")
}

/// Inventory every scope worktree against the two remotes. A peer caller sees no worktree a
/// private mark covers.
pub fn report(paths: &Paths, at_risk_only: bool, from_peer: bool) -> Value {
    let root = &paths.workspace;
    let cache = paths.shared_cache();
    let mut rows = vec![];
    for wt in find_worktrees(root) {
        if from_peer && awm_plugin_sdk::privacy::hidden_path(&wt.to_string_lossy()) {
            continue;
        }
        let scope = wt.strip_prefix(root).map(|r| r.to_path_buf()).unwrap_or_else(|_| wt.clone());
        let mut row = json!({
            "scope": scope.to_string_lossy(),
            "uncommitted": uncommitted(&wt),
            "unpushed": unpushed(&wt),
            "pins": pins(&wt, &cache),
        });
        row["at_risk"] = json!(at_risk(&row));
        rows.push(row);
    }
    let risky: Vec<Value> = rows.iter().filter(|r| r["at_risk"] == json!(true)).cloned().collect();
    let truncated = at_risk_only && rows.len() != risky.len();
    json!({
        "workspace": root.to_string_lossy(),
        "cache": cache.to_string_lossy(),
        "cache_exists": cache.is_dir(),
        "scopes": rows.len(),
        "scopes_at_risk": risky.len(),
        "covered_by": {
            "code": "github (per project bare repo)",
            "data": "chinook (append-only sync of the shared cache)",
        },
        "not_covered": "anything in a worktree that is neither committed-and-pushed nor DVC-pinned — scratch dirs and run outputs. This is deliberate; those are disposable by construction.",
        "worktrees": if at_risk_only { risky } else { rows },
        "truncated": truncated,
    })
}
