//! The create hook: a project makes some scopes by its own script rather than by a git
//! worktree, as social's seats do.
//!
//! A project opts in with `awm-project.toml` at the root of its main checkout, the worktree
//! of the bare repo's HEAD branch. The file sits outside `.awm/`, which the R142 sweep
//! empties:
//!
//! ```toml
//! [scope]
//! create_hook = "scripts/new-seat.sh"   # relative to the checkout
//! create_hook_prefix = "seats/"         # optional: only the scopes under it
//! ```
//!
//! `create {project, scope}` on a matching scope runs the hook with the prefix stripped
//! (`seats/ana` runs `new-seat.sh ana`) in the checkout, with stdin closed, killed if the
//! caller cancels. The hook registers the scope itself with `scope ensure`. scopes then
//! checks the three things success needs, sets the brief and tags, and removes nothing
//! when any is missing.

use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;

use serde_json::json;

use crate::lifecycle::{Action, CreateReq};
use crate::{db, path_str, Fail, Op};

pub const FILE: &str = "awm-project.toml";
/// The hook's output lines a failure quotes.
const TAIL: usize = 20;

/// A project's create hook, as its file declares it.
#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub checkout: PathBuf,
    pub hook: String,
    pub prefix: Option<String>,
}

/// The hook that makes one scope.
#[derive(Debug, Clone, PartialEq)]
pub struct Hook {
    pub checkout: PathBuf,
    /// As the file names it.
    pub rel: String,
    pub script: PathBuf,
    /// The scope name with the prefix stripped.
    pub arg: String,
}

/// The project's main checkout: the worktree holding the bare repo's HEAD branch.
fn main_checkout(op: &Op, project: &str) -> Result<Option<PathBuf>, Fail> {
    let bare = op.paths.bare(project);
    let Some(branch) = crate::git::head_branch(&bare) else { return Ok(None) };
    let held = op.branch_checkouts(&bare).ok().and_then(|m| m.get(&branch).cloned());
    let dir = held.unwrap_or_else(|| op.paths.projects.join(project).join(&branch));
    Ok(dir.is_dir().then_some(dir))
}

/// The project's hook declaration, or `None` for a project without one.
pub fn config(op: &Op, project: &str) -> Result<Option<Config>, Fail> {
    let Some(checkout) = main_checkout(op, project)? else { return Ok(None) };
    let file = checkout.join(FILE);
    let text = match std::fs::read_to_string(&file) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(crate::os_fail(&e, &file)),
    };
    let doc: toml::Table = text.parse().map_err(|e: toml::de::Error| Fail::value(format!("{}: {}", file.display(), e.message())))?;
    let Some(scope) = doc.get("scope") else { return Ok(None) };
    let field = |k: &str| -> Result<Option<String>, Fail> {
        match scope.get(k) {
            None => Ok(None),
            Some(toml::Value::String(s)) if !s.trim().is_empty() => Ok(Some(s.trim().to_string())),
            Some(_) => Err(Fail::value(format!("{}: [scope] {k} must be a non-empty string", file.display()))),
        }
    };
    let Some(hook) = field("create_hook")? else { return Ok(None) };
    Ok(Some(Config { checkout, hook, prefix: field("create_hook_prefix")? }))
}

/// The hook that makes `scope`, or `None` when the project has none or the scope lies
/// outside its prefix.
pub fn find(op: &Op, project: &str, scope: &str) -> Result<Option<Hook>, Fail> {
    let Some(cfg) = config(op, project)? else { return Ok(None) };
    let arg = match &cfg.prefix {
        None => scope.to_string(),
        Some(prefix) => match scope.strip_prefix(prefix.as_str()) {
            Some(rest) if !rest.trim_start_matches('/').is_empty() => rest.trim_start_matches('/').to_string(),
            _ => return Ok(None),
        },
    };
    let root = cfg.checkout.canonicalize().map_err(|e| crate::os_fail(&e, &cfg.checkout))?;
    let named = cfg.checkout.join(&cfg.hook);
    let script = named.canonicalize().map_err(|_| Fail::value(format!("{project}'s create hook {} does not exist in {}", cfg.hook, cfg.checkout.display())))?;
    if !script.starts_with(&root) {
        return Err(Fail::value(format!("{project}'s create hook {} resolves to {}, outside its checkout {}", cfg.hook, script.display(), root.display())));
    }
    let executable = std::fs::metadata(&script).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0);
    if !executable {
        return Err(Fail::value(format!("{project}'s create hook {} is not an executable file", cfg.hook)));
    }
    Ok(Some(Hook { checkout: cfg.checkout, rel: cfg.hook, script, arg }))
}

fn tail(out: &str) -> String {
    let lines: Vec<&str> = out.lines().collect();
    lines[lines.len().saturating_sub(TAIL)..].join("\n")
}

impl Op {
    /// Make `req`'s scope with the project's hook. The name and "already exists" checks have
    /// run. `session` is the scope's session number.
    pub fn run_create_hook(&self, req: &CreateReq, hook: &Hook, session: i64, tags: &[String]) -> Result<Action, Fail> {
        let (project, scope) = (req.project.as_str(), req.scope.as_deref().unwrap_or(""));
        let refused: Vec<&str> = [("from_branch", &req.from_branch), ("branch_name", &req.branch_name), ("clone_url", &req.clone_url), ("fork_url", &req.fork_url)]
            .iter()
            .filter(|(_, v)| v.as_deref().is_some_and(|s| !s.is_empty()))
            .map(|(k, _)| *k)
            .collect();
        if !refused.is_empty() {
            return Err(Fail::value(format!(
                "{project}/{scope} is made by the project's create hook {}, which takes no git parameters; drop {}",
                hook.rel,
                refused.join(", ")
            )));
        }
        let script = path_str(&hook.script);
        // One stream, so the tail interleaves the hook's stdout and stderr as it wrote them.
        let r = self.run(&["/bin/sh", "-c", "exec \"$0\" \"$@\" 2>&1", &script, &hook.arg], Some(&hook.checkout))?;
        self.fault("hooked")?;
        let said = || {
            let t = tail(&r.stdout);
            if t.is_empty() { "the hook printed nothing".to_string() } else { format!("its last output:\n{t}") }
        };
        let what = format!("create hook {} {}", hook.rel, hook.arg);
        if !r.ok() {
            return Err(Fail::runtime(format!("{what} failed for {project}/{scope} with exit {}; {}", r.code, said())));
        }
        let dir = self.paths.projects.join(project).join(scope);
        if !dir.is_dir() {
            return Err(Fail::runtime(format!("{what} exited 0 but made no directory at {}; {}", dir.display(), said())));
        }
        if self.read(|c| db::agent_id(c, project, scope, true))?.is_none() {
            return Err(Fail::runtime(format!(
                "{what} exited 0 but registered no active scope {project}/{scope} (the hook must run `scope ensure`); {}",
                said()
            )));
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
            format!("Created scope at projects/{project}/{scope} with the create hook {} {}, session {session}{}", hook.rel, hook.arg, warning.map(|w| format!("; {w}")).unwrap_or_default()),
        );
        out.session = Some(session);
        Ok(out)
    }
}
