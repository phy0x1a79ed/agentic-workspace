//! Private marks: the projects and scopes this node hides from every peer caller
//! (`awm.config.privacy`).
//!
//! The marks live in `<AWM_DIR>/private.json` as `{"version": 1, "marks": [{project, scope?,
//! path}]}`. The scopes plugin writes the file and every process rereads it when it changes.
//! A caller stamped `peer` or `peer:<node>` sees nothing a mark covers. A file that exists but
//! does not parse fails closed: every test answers "hidden". So does a marks path that is a
//! dangling link, or whose state dir is missing: neither proves that no mark exists.

use std::collections::HashSet;
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use std::sync::Mutex;
use std::time::SystemTime;

use serde_json::{json, Value};

pub const FILENAME: &str = "private.json";
const VERSION: u64 = 1;
/// The marks file is readable by the node's own group, as other users' units need it.
const FILE_MODE: u32 = 0o640;
const DIR_MODE: u32 = 0o750;
/// Row keys whose value is a filesystem path a mark can cover.
pub const PATH_KEYS: [&str; 6] = ["path", "cwd", "worktree", "worktree_path", "repo_path", "dir"];
/// Argument keys whose absolute-path value a mark can cover.
pub const ARG_PATH_KEYS: [&str; 4] = ["scope", "path", "cwd", "worktree"];

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Mark {
    pub project: String,
    pub scope: Option<String>,
    pub path: String,
}

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct MarksUnreadable(pub String);

struct Loaded {
    marks: Vec<Mark>,
    roots: Vec<Vec<PathBuf>>,
    slugs: Vec<Vec<String>>,
}

type Key = (PathBuf, SystemTime, u64);
static CACHE: Mutex<Option<(Key, Option<std::sync::Arc<Loaded>>)>> = Mutex::new(None);

pub fn marks_path() -> PathBuf {
    crate::config::awm_dir().join(FILENAME)
}

/// Whether a caller stamp names another node: bare `peer` or `peer:<node>`.
pub fn from_peer(as_: Option<&str>) -> bool {
    let stamp = as_.unwrap_or("").trim().to_lowercase();
    stamp == "peer" || stamp.starts_with("peer:")
}

fn check(entry: &Value) -> Result<Mark, MarksUnreadable> {
    let bad = |why: &str| MarksUnreadable(format!("mark {why}: {entry}"));
    let project = entry.get("project").and_then(Value::as_str).filter(|p| !p.is_empty()).ok_or_else(|| bad("has no project"))?;
    let scope = match entry.get("scope") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        _ => return Err(bad("has a malformed scope")),
    };
    let path = entry.get("path").and_then(Value::as_str).filter(|p| p.starts_with('/')).ok_or_else(|| bad("has no absolute path"))?;
    Ok(Mark { project: project.into(), scope, path: path.into() })
}

fn parse(raw: &str) -> Result<Vec<Mark>, MarksUnreadable> {
    let doc: Value = serde_json::from_str(raw).map_err(|e| MarksUnreadable(e.to_string()))?;
    if doc.get("version").and_then(Value::as_u64) != Some(VERSION) {
        return Err(MarksUnreadable("not a version-1 marks document".into()));
    }
    let entries = doc.get("marks").and_then(Value::as_array).ok_or_else(|| MarksUnreadable("marks is not a list".into()))?;
    entries.iter().map(check).collect()
}

/// `None` is a marks file that cannot be read as "these marks": one that does not parse, a
/// dangling link, or a path whose state dir is missing. Only an absent file in a present state
/// dir is "no marks".
fn load() -> Option<std::sync::Arc<Loaded>> {
    let path = marks_path();
    let meta = match std::fs::metadata(&path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            let dangling = std::fs::symlink_metadata(&path).is_ok();
            let dir_present = path.parent().is_some_and(Path::is_dir);
            if dangling || !dir_present {
                return None;
            }
            return Some(std::sync::Arc::new(Loaded { marks: vec![], roots: vec![], slugs: vec![] }));
        }
        Err(_) => return None,
    };
    let key = (path.clone(), meta.modified().unwrap_or(SystemTime::UNIX_EPOCH), meta.len());
    let mut cache = CACHE.lock().unwrap_or_else(|p| p.into_inner());
    if let Some((k, loaded)) = cache.as_ref() {
        if *k == key {
            return loaded.clone();
        }
    }
    let loaded = std::fs::read_to_string(&path).ok().and_then(|t| parse(&t).ok()).map(|marks| {
        let roots: Vec<Vec<PathBuf>> = marks.iter().map(|m| forms(Path::new(&m.path))).collect();
        let slugs = roots.iter().map(|rs| rs.iter().map(|r| slug(&r.to_string_lossy())).collect()).collect();
        std::sync::Arc::new(Loaded { marks, roots, slugs })
    });
    *cache = Some((key, loaded.clone()));
    loaded
}

/// The marks in force, or an error when the file is broken.
pub fn marks() -> Result<Vec<Mark>, MarksUnreadable> {
    load().map(|l| l.marks.clone()).ok_or_else(|| {
        MarksUnreadable(format!("{} is no valid marks file: a broken file, a dangling link or a missing state dir", marks_path().display()))
    })
}

/// Replace the marks file atomically with `entries`, and return them as written.
pub fn write_marks(entries: &[Value]) -> Result<Vec<Mark>, MarksUnreadable> {
    let checked: Vec<Mark> = entries.iter().map(check).collect::<Result<_, _>>()?;
    let path = marks_path();
    let dir = path.parent().unwrap_or(Path::new("."));
    let io = |e: std::io::Error| MarksUnreadable(format!("{}: {e}", path.display()));
    std::fs::DirBuilder::new().recursive(true).mode(DIR_MODE).create(dir).map_err(io)?;
    let body = serde_json::to_string_pretty(&json!({"version": VERSION, "marks": checked})).unwrap_or_default() + "\n";
    let tmp = dir.join(format!(".{FILENAME}.{}", std::process::id()));
    let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(FILE_MODE).open(&tmp).map_err(io)?;
    // The umask may widen or narrow the mode the open asked for.
    let written = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(FILE_MODE))
        .and_then(|_| f.write_all(body.as_bytes()))
        .and_then(|_| f.sync_all())
        .and_then(|_| std::fs::rename(&tmp, &path));
    if let Err(e) = written {
        let _ = std::fs::remove_file(&tmp);
        return Err(io(e));
    }
    Ok(checked)
}

/// The `(project, scope)` a journal spelling names: an empty project with scope `project:<p>`
/// names project `<p>`.
pub fn canonical<'a>(project: Option<&'a str>, scope: Option<&'a str>) -> (Option<&'a str>, Option<&'a str>) {
    match (project.filter(|p| !p.is_empty()), scope) {
        (None, Some(s)) if s.starts_with("project:") => (Some(&s[8..]).filter(|p| !p.is_empty()), None),
        (p, s) => (p, s),
    }
}

/// Whether a mark covers this project, or this scope of it.
pub fn hidden(project: &str, scope: Option<&str>) -> bool {
    let Some(loaded) = load() else { return true };
    let (Some(project), scope) = canonical(Some(project), scope) else { return false };
    loaded.marks.iter().any(|m| {
        m.project == project
            && match (&m.scope, scope) {
                (None, _) => true,
                (Some(ms), Some(s)) => s == ms || s.starts_with(&format!("{ms}/")),
                (Some(_), None) => false,
            }
    })
}

fn normpath(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
}

fn forms(path: &Path) -> Vec<PathBuf> {
    let raw = path.to_string_lossy();
    let expanded = match raw.strip_prefix('~') {
        Some(rest) => PathBuf::from(format!("{}{rest}", std::env::var("HOME").unwrap_or_default())),
        None => path.to_path_buf(),
    };
    let p = if expanded.is_absolute() { expanded } else { crate::config::workspace_root().join(expanded) };
    let mut out = vec![normpath(&p)];
    if let Ok(real) = p.canonicalize() {
        if !out.contains(&real) {
            out.push(real);
        }
    }
    out
}

/// Whether the path is a marked path or lies under one. It need not exist.
pub fn hidden_path(path: &str) -> bool {
    let Some(loaded) = load() else { return true };
    if loaded.marks.is_empty() || path.is_empty() {
        return false;
    }
    let fs = forms(Path::new(path));
    loaded.roots.iter().flatten().any(|root| fs.iter().any(|f| f.starts_with(root)))
}

/// Claude Code's transcript directory name for a path.
pub fn slug(path: &str) -> String {
    path.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect()
}

/// Whether the slug `candidate` is `prefix` or continues it past a `-`.
pub fn slug_covers(prefix: &str, candidate: &str) -> bool {
    candidate == prefix || candidate.strip_prefix(prefix).is_some_and(|r| r.starts_with('-'))
}

/// Whether a Claude Code transcript slug belongs to a marked path.
pub fn hidden_slug(name: &str) -> bool {
    let Some(loaded) = load() else { return true };
    loaded.slugs.iter().flatten().any(|prefix| slug_covers(prefix, name))
}

/// Whether a Claude Code transcript ever ran with its cwd under a marked path.
pub fn transcript_entered(path: &Path) -> bool {
    let Some(loaded) = load() else { return true };
    if loaded.marks.is_empty() {
        return false;
    }
    let Ok(raw) = std::fs::read_to_string(path) else { return false };
    let mut seen = HashSet::new();
    let mut rest = raw.as_str();
    while let Some(i) = rest.find("\"cwd\":") {
        rest = rest[i + 6..].trim_start();
        let mut de = serde_json::Deserializer::from_str(rest).into_iter::<String>();
        if let Some(Ok(cwd)) = de.next() {
            if !cwd.is_empty() && seen.insert(cwd.clone()) && hidden_path(&cwd) {
                return true;
            }
        }
    }
    false
}

fn row_hidden(row: &serde_json::Map<String, Value>) -> bool {
    let project = row.get("project").and_then(Value::as_str);
    let scope = row.get("scope").and_then(Value::as_str).filter(|s| !s.is_empty());
    if (project.is_some_and(|p| !p.is_empty()) || scope.is_some()) && hidden(project.unwrap_or(""), scope) {
        return true;
    }
    PATH_KEYS.iter().any(|k| row.get(*k).and_then(Value::as_str).is_some_and(|p| !p.is_empty() && hidden_path(p)))
}

/// The rows a caller stamped `as_` may see: all of them for a local caller.
pub fn visible_rows(rows: Vec<Value>, as_: Option<&str>) -> Vec<Value> {
    if !from_peer(as_) {
        return rows;
    }
    if load().is_none() {
        return vec![];
    }
    rows.into_iter().filter(|r| !r.as_object().is_some_and(row_hidden)).collect()
}

fn looks_absolute(v: Option<&Value>) -> Option<&str> {
    v.and_then(Value::as_str).filter(|s| s.starts_with('/') || s.starts_with('~'))
}

/// Whether call arguments name a covered project, scope or absolute path.
pub fn hides_args(args: &Value) -> bool {
    let Some(map) = args.as_object() else { return false };
    let project = map.get("project").and_then(Value::as_str).unwrap_or("");
    let named_scope = map.get("scope").and_then(Value::as_str).filter(|s| !s.is_empty() && !s.starts_with('/') && !s.starts_with('~'));
    if (!project.is_empty() || named_scope.is_some()) && hidden(project, named_scope) {
        return true;
    }
    ARG_PATH_KEYS.iter().any(|k| looks_absolute(map.get(*k)).is_some_and(hidden_path))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn slugs() {
        assert_eq!(slug("/home/t/a.b"), "-home-t-a-b");
        assert!(slug_covers("-home-t", "-home-t-x") && !slug_covers("-home-t", "-home-tx"));
        assert_eq!(canonical(Some(""), Some("project:p")), (Some("p"), None));
        assert!(from_peer(Some(" Peer:x")) && from_peer(Some("peer")) && !from_peer(Some("tony")) && !from_peer(None));
    }

    /// One test, since `marks_path` reads the process-wide `AWM_DIR`.
    #[test]
    fn the_marks_file_and_its_failures() {
        let t = tempfile::tempdir().unwrap();
        let state = t.path().join("state");
        std::env::set_var("AWM_DIR", &state);
        assert!(marks().is_err(), "a missing state dir is no proof of no marks");
        let mark = json!({"project": "p", "path": "/x"});
        assert_eq!(write_marks(&[mark]).unwrap().len(), 1);
        let mode = |p: &Path| std::fs::metadata(p).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode(&marks_path()), 0o640);
        assert_eq!(mode(&state), 0o750);
        assert_eq!(marks().unwrap()[0].project, "p");

        std::fs::remove_file(marks_path()).unwrap();
        assert!(marks().unwrap().is_empty(), "no file in a present state dir is no marks");
        std::os::unix::fs::symlink(t.path().join("gone.json"), marks_path()).unwrap();
        assert!(marks().is_err(), "a dangling link reads as an error");
        assert!(hidden("q", None), "a caller refuses peers while the marks are unreadable");
        std::env::remove_var("AWM_DIR");
    }

    #[test]
    fn marks_parse_and_fail_closed() {
        assert!(parse(r#"{"version":1,"marks":[{"project":"p","path":"/x"}]}"#).is_ok());
        assert!(parse(r#"{"version":2,"marks":[]}"#).is_err());
        assert!(parse(r#"{"version":1,"marks":[{"project":"p","path":"rel"}]}"#).is_err());
    }
}
