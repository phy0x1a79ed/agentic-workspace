//! Private marks: what a call from another node may not reach (`config/privacy.py`).
//!
//! The scopes service writes `<AWM_DIR>/marks/private.json` as `{"version": 1, "marks":
//! [{project, scope?, path}]}`, and `<AWM_DIR>/private.json` on a node not yet moved. A call from a peer never names a marked project, scope or path, and an
//! emit stream to a peer never carries a payload a mark covers. A refusal reads as an unknown
//! name, so it does not confirm the target exists. A marks file that does not parse fails
//! closed: every test answers "hidden". So does a dangling link, which proves no absence.

use std::path::{Component, Path, PathBuf};
use std::sync::Mutex;
use std::time::SystemTime;

use awm_proto::{CallContext, Lane};
use serde_json::Value;

pub const FILENAME: &str = "private.json";
pub const DIRNAME: &str = "marks";
/// How deep nested arguments are searched for a path.
const MAX_DEPTH: usize = 4;
/// Argument keys whose absolute-path value a mark can cover.
const ARG_PATH_KEYS: [&str; 4] = ["scope", "path", "cwd", "worktree"];
/// Payload keys whose value is a path a mark can cover.
const ROW_PATH_KEYS: [&str; 6] = ["path", "cwd", "worktree", "worktree_path", "repo_path", "dir"];

#[derive(Debug, Clone)]
struct Mark {
    project: String,
    scope: Option<String>,
    roots: Vec<PathBuf>,
}

/// `None` is a marks file that exists and does not parse.
type Loaded = Option<Vec<Mark>>;
/// A marks file's path, mtime and length, which say when to reread it.
type Stamp = (PathBuf, SystemTime, u64);

pub struct Marks {
    state: PathBuf,
    workspace: PathBuf,
    cache: Mutex<Option<(Stamp, Loaded)>>,
}

/// Whether a call comes from another node: the edge lane, or a `peer` / `peer:<node>` stamp.
pub fn from_peer(ctx: &CallContext) -> bool {
    ctx.caller.lane == Lane::Edge
        || ctx.as_.as_deref().map(|a| a.trim().to_lowercase()).is_some_and(|a| a == "peer" || a.starts_with("peer:"))
}

impl Marks {
    pub fn new(state: &Path, workspace: &Path) -> Self {
        Self { state: state.to_path_buf(), workspace: workspace.to_path_buf(), cache: Mutex::new(None) }
    }

    /// The marks file: in `marks/` once that directory exists.
    fn file(&self) -> PathBuf {
        let dir = self.state.join(DIRNAME);
        if dir.is_dir() {
            dir.join(FILENAME)
        } else {
            self.state.join(FILENAME)
        }
    }

    fn load(&self) -> Loaded {
        let file = self.file();
        let meta = match std::fs::metadata(&file) {
            Ok(m) => m,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                if std::fs::symlink_metadata(&file).is_ok() {
                    tracing::error!("{} is a dangling link; peers see nothing a mark could cover", file.display());
                    return None;
                }
                return Some(vec![]);
            }
            Err(_) => return None,
        };
        let key = (file.clone(), meta.modified().unwrap_or(SystemTime::UNIX_EPOCH), meta.len());
        let mut cache = self.cache.lock().unwrap_or_else(|p| p.into_inner());
        if let Some((k, loaded)) = cache.as_ref() {
            if *k == key {
                return loaded.clone();
            }
        }
        let loaded = std::fs::read_to_string(&file).ok().and_then(|t| self.parse(&t));
        if loaded.is_none() {
            tracing::error!("{} does not parse; peers see nothing a mark could cover", file.display());
        }
        *cache = Some((key, loaded.clone()));
        loaded
    }

    fn parse(&self, text: &str) -> Option<Vec<Mark>> {
        let doc: Value = serde_json::from_str(text).ok()?;
        if doc.get("version").and_then(Value::as_i64) != Some(1) {
            return None;
        }
        let mut out = Vec::new();
        for m in doc.get("marks")?.as_array()? {
            let project = m.get("project")?.as_str().filter(|p| !p.is_empty())?.to_string();
            let scope = match m.get("scope") {
                None | Some(Value::Null) => None,
                Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
                Some(_) => return None,
            };
            let path = m.get("path")?.as_str().filter(|p| p.starts_with('/'))?;
            out.push(Mark { project, scope, roots: self.forms(path) });
        }
        Some(out)
    }

    /// The path as named (normalised, relative ones under the workspace) and as it resolves.
    fn forms(&self, raw: &str) -> Vec<PathBuf> {
        let expanded = match raw.strip_prefix('~') {
            Some(rest) => PathBuf::from(std::env::var("HOME").unwrap_or_default()).join(rest.trim_start_matches('/')),
            None => PathBuf::from(raw),
        };
        let absolute = if expanded.is_absolute() { expanded } else { self.workspace.join(expanded) };
        let mut forms = vec![normalise(&absolute)];
        if let Ok(real) = absolute.canonicalize() {
            if !forms.contains(&real) {
                forms.push(real);
            }
        }
        forms
    }

    /// Whether a mark covers this project, or this scope of it. Takes the journal's spellings
    /// `task:<p>`, and an empty project with scope `user:proj:<p>` or `project:<p>`.
    fn hidden(&self, marks: &[Mark], project: &str, scope: Option<&str>) -> bool {
        let (project, scope) = canonical(project, scope);
        let Some(project) = project else { return false };
        marks.iter().any(|m| {
            m.project == project
                && match (&m.scope, scope) {
                    (None, _) => true,
                    (Some(ms), Some(s)) => s == ms || s.starts_with(&format!("{ms}/")),
                    (Some(_), None) => false,
                }
        })
    }

    fn hidden_path(&self, marks: &[Mark], path: &str) -> bool {
        if path.is_empty() {
            return false;
        }
        let forms = self.forms(path);
        marks.iter().flat_map(|m| &m.roots).any(|root| forms.iter().any(|f| f.starts_with(root)))
    }

    /// Whether a peer call with `args` names something a mark covers.
    pub fn refuses(&self, ctx: &CallContext, args: &Value) -> bool {
        if !from_peer(ctx) || !args.is_object() {
            return false;
        }
        let Some(marks) = self.load() else { return true };
        if marks.is_empty() {
            return false;
        }
        let str_of = |k: &str| args.get(k).and_then(Value::as_str);
        let project = str_of("project").unwrap_or("");
        let scope = str_of("scope").filter(|s| !s.is_empty() && !looks_absolute(s));
        if (!project.is_empty() || scope.is_some()) && self.hidden(&marks, project, scope) {
            return true;
        }
        if ARG_PATH_KEYS.iter().filter_map(|k| str_of(k)).any(|v| looks_absolute(v) && self.hidden_path(&marks, v)) {
            return true;
        }
        if let Some(projects) = args.get("projects").and_then(Value::as_array) {
            if projects.iter().filter_map(Value::as_str).any(|p| !p.is_empty() && self.hidden(&marks, p, None)) {
                return true;
            }
        }
        let mut paths = Vec::new();
        paths_in(args, 0, &mut paths);
        paths.iter().any(|p| self.hidden_path(&marks, p))
    }

    /// Whether an emitted payload may not reach a peer: it, or an object it carries one level
    /// down (`{project, scope, post: {...}}`), is covered.
    pub fn emit_hidden(&self, payload: &Value) -> bool {
        let Some(marks) = self.load() else { return true };
        if marks.is_empty() {
            return false;
        }
        let mut rows = vec![payload];
        if let Some(o) = payload.as_object() {
            rows.extend(o.values().filter(|v| v.is_object()));
        }
        rows.into_iter().any(|row| self.row_hidden(&marks, row))
    }

    fn row_hidden(&self, marks: &[Mark], row: &Value) -> bool {
        let Some(o) = row.as_object() else { return false };
        let project = o.get("project").and_then(Value::as_str).unwrap_or("");
        let scope = o.get("scope").and_then(Value::as_str).filter(|s| !s.is_empty());
        if (!project.is_empty() || scope.is_some()) && self.hidden(marks, project, scope) {
            return true;
        }
        ROW_PATH_KEYS.iter().filter_map(|k| o.get(*k).and_then(Value::as_str)).any(|p| self.hidden_path(marks, p))
    }
}

fn canonical<'a>(project: &'a str, scope: Option<&'a str>) -> (Option<&'a str>, Option<&'a str>) {
    if project.is_empty() {
        if let Some(s) = scope {
            for channel in ["user:proj:", "project:"] {
                if let Some(p) = s.strip_prefix(channel) {
                    return (Some(p).filter(|p| !p.is_empty()), None);
                }
            }
        }
    }
    let project = project.strip_prefix("task:").unwrap_or(project);
    (Some(project).filter(|p| !p.is_empty()), scope)
}

fn looks_absolute(v: &str) -> bool {
    v.starts_with('/') || v.starts_with('~')
}

/// Every string in `value` shaped like a path: absolute, `~` or under `projects/`.
fn paths_in(value: &Value, depth: usize, out: &mut Vec<String>) {
    match value {
        Value::String(s) if s.starts_with('/') || s.starts_with('~') || s.starts_with("projects/") => out.push(s.clone()),
        Value::Object(o) if depth < MAX_DEPTH => o.values().for_each(|v| paths_in(v, depth + 1, out)),
        Value::Array(a) if depth < MAX_DEPTH => a.iter().for_each(|v| paths_in(v, depth + 1, out)),
        _ => {}
    }
}

/// `a/./b/../c` → `a/c`, with no filesystem access.
fn normalise(p: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use awm_proto::Surface;
    use serde_json::json;

    fn peer() -> CallContext {
        let mut c = CallContext::local("mizar", Surface::Http);
        c.caller.lane = Lane::Edge;
        c
    }

    #[test]
    fn a_peer_never_reaches_a_marked_project_scope_or_path() {
        let t = tempfile::tempdir().unwrap();
        let ws = t.path().join("ws");
        std::fs::create_dir_all(ws.join("projects/secret/main")).unwrap();
        let marks = Marks::new(t.path(), &ws);
        assert!(!marks.refuses(&peer(), &json!({"project": "secret"})), "no marks file hides nothing");
        let path = ws.join("projects/secret");
        std::fs::write(
            t.path().join(FILENAME),
            json!({"version": 1, "marks": [{"project": "secret", "scope": null, "path": path},
                                           {"project": "lab", "scope": "a", "path": ws.join("projects/lab/a")}]}).to_string(),
        )
        .unwrap();
        let p = peer();
        assert!(marks.refuses(&p, &json!({"project": "secret", "scope": "main"})));
        assert!(marks.refuses(&p, &json!({"project": "task:secret"})));
        assert!(marks.refuses(&p, &json!({"scope": "project:secret"})));
        assert!(marks.refuses(&p, &json!({"project": "lab", "scope": "a/b"})));
        assert!(!marks.refuses(&p, &json!({"project": "lab", "scope": "b"})));
        assert!(marks.refuses(&p, &json!({"cwd": format!("{}/main/x", path.display())})));
        assert!(marks.refuses(&p, &json!({"deep": {"list": ["projects/secret/main"]}})));
        assert!(marks.refuses(&p, &json!({"projects": ["open", "secret"]})));
        assert!(!marks.refuses(&p, &json!({"project": "open", "body": "projects are fun"})));
        assert!(!marks.refuses(&CallContext::local("mizar", Surface::Http), &json!({"project": "secret"})), "a local caller sees all");
        let mut stamped = CallContext::local("mizar", Surface::Http);
        stamped.as_ = Some("peer:alcor".into());
        assert!(marks.refuses(&stamped, &json!({"project": "secret"})));

        assert!(marks.emit_hidden(&json!({"project": "x", "post": {"project": "secret"}})));
        assert!(!marks.emit_hidden(&json!({"project": "open", "post": {"body": "hi"}})));

        std::fs::write(t.path().join(FILENAME), "not json").unwrap();
        assert!(marks.refuses(&p, &json!({"project": "open"})), "a broken file fails closed");
        assert!(marks.emit_hidden(&json!({"any": 1})));

        std::fs::remove_file(t.path().join(FILENAME)).unwrap();
        std::fs::create_dir(t.path().join(DIRNAME)).unwrap();
        assert!(!marks.refuses(&p, &json!({"project": "secret"})), "an empty marks dir hides nothing");
        std::fs::write(
            t.path().join(DIRNAME).join(FILENAME),
            json!({"version": 1, "marks": [{"project": "secret", "scope": null, "path": path}]}).to_string(),
        )
        .unwrap();
        std::os::unix::fs::symlink("marks/private.json", t.path().join(FILENAME)).unwrap();
        assert!(marks.refuses(&p, &json!({"project": "secret"})), "the marks dir is read first");
        assert!(!marks.refuses(&p, &json!({"project": "open"})));
        std::fs::remove_file(t.path().join(DIRNAME).join(FILENAME)).unwrap();
        std::fs::remove_dir(t.path().join(DIRNAME)).unwrap();
        assert!(marks.refuses(&p, &json!({"project": "open"})), "a dangling link fails closed");
    }
}
