//! Where Claude Code keeps session logs, and what one session's files are.
//!
//! Each session is a transcript beside a sidecar directory of the same name:
//!
//! ```text
//! ~/.claude/projects/<project-slug>/
//!     <sessionId>.jsonl       the conversation
//!     <sessionId>/            sidecar: tool-results/, subagents/*.jsonl
//!     memory/                 the auto-memory, NOT session data
//! ```
//!
//! **CAUTION** The sidecar sits at the depth of a project's `memory/`, so a walk by depth
//! rather than by session reaches the memory. [`RESERVED`] is the guard, and every listing
//! goes through [`sessions`]. The archive mirrors the tree with `.gz` on each file.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

/// Directories inside a project that are not sessions and must never be swept.
pub const RESERVED: &[&str] = &["memory"];

/// The live tree and its archive.
#[derive(Debug, Clone)]
pub struct Tree {
    pub projects: PathBuf,
    pub archive: PathBuf,
}

impl Tree {
    /// `~/.claude/projects` and `~/.claude/projects-archive`.
    pub fn home() -> Self {
        #[allow(deprecated)]
        let home = std::env::home_dir().unwrap_or_else(|| PathBuf::from("/"));
        let claude = home.join(".claude");
        Tree { projects: claude.join("projects"), archive: claude.join("projects-archive") }
    }
}

/// Which side of the tree a listing reads: the file suffix differs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Live,
    Archive,
}

impl Side {
    pub fn suffix(self) -> &'static str {
        match self {
            Side::Live => ".jsonl",
            Side::Archive => ".jsonl.gz",
        }
    }
}

/// One session's files under one project. An orphan has a sidecar and no transcript.
#[derive(Debug, Clone)]
pub struct Session {
    pub project: String,
    pub session_id: String,
    pub transcript: Option<PathBuf>,
    pub sidecar: Option<PathBuf>,
}

fn mtime(meta: &std::fs::Metadata) -> f64 {
    meta.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map_or(0.0, |d| d.as_secs_f64())
}

/// The regular files under `dir`, as `os.walk` names them: symlinked directories are not entered.
pub fn walk_files(dir: &Path) -> Vec<PathBuf> {
    let mut out = vec![];
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        let Ok(rd) = std::fs::read_dir(&d) else { continue };
        let mut entries: Vec<_> = rd.flatten().collect();
        entries.sort_by_key(|e| e.file_name());
        for e in entries {
            let p = e.path();
            match e.file_type() {
                Ok(t) if t.is_dir() => stack.push(p),
                Ok(t) if t.is_symlink() => {
                    if !p.is_dir() {
                        out.push(p);
                    }
                }
                Ok(_) => out.push(p),
                Err(_) => {}
            }
        }
    }
    out
}

impl Session {
    /// The age the sweep decides on: the newest file anywhere in the session. A subagent that
    /// wrote a minute ago keeps an old-looking transcript live.
    pub fn mtime(&self) -> f64 {
        let mut best: f64 = 0.0;
        if let Some(t) = &self.transcript {
            if let Ok(m) = std::fs::metadata(t) {
                best = mtime(&m);
            }
        }
        if let Some(d) = &self.sidecar {
            for f in walk_files(d) {
                if let Ok(m) = std::fs::metadata(&f) {
                    best = best.max(mtime(&m));
                }
            }
        }
        best
    }

    pub fn bytes(&self) -> u64 {
        let mut total = 0;
        if let Some(t) = &self.transcript {
            total += std::fs::metadata(t).map_or(0, |m| m.len());
        }
        if let Some(d) = &self.sidecar {
            total += walk_files(d).iter().filter_map(|f| std::fs::metadata(f).ok()).map(|m| m.len()).sum::<u64>();
        }
        total
    }
}

fn sorted_entries(dir: &Path) -> Option<Vec<PathBuf>> {
    let mut v: Vec<PathBuf> = std::fs::read_dir(dir).ok()?.flatten().map(|e| e.path()).collect();
    v.sort();
    Some(v)
}

fn name(p: &Path) -> String {
    p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

/// Every session under `root`, less the projects `hide` names. A session is named by whichever
/// of its two parts exists, so an orphaned sidecar comes back with no transcript.
pub fn sessions(root: &Path, side: Side, hide: Option<&dyn Fn(&str) -> bool>) -> Vec<Session> {
    let suffix = side.suffix();
    let mut out = vec![];
    let Some(projects) = sorted_entries(root) else { return out };
    for proj in projects.into_iter().filter(|p| p.is_dir() && !hide.is_some_and(|h| h(&name(p)))) {
        let Some(entries) = sorted_entries(&proj) else { continue };
        let mut found: BTreeMap<String, (Option<PathBuf>, Option<PathBuf>)> = BTreeMap::new();
        for e in entries {
            let n = name(&e);
            if e.is_dir() {
                if !RESERVED.contains(&n.as_str()) {
                    found.entry(n).or_default().1 = Some(e);
                }
            } else if let Some(sid) = n.strip_suffix(suffix) {
                found.entry(sid.to_string()).or_default().0 = Some(e);
            }
        }
        let project = name(&proj);
        out.extend(found.into_iter().map(|(sid, (transcript, sidecar))| Session { project: project.clone(), session_id: sid, transcript, sidecar }));
    }
    out
}
