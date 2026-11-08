//! scopes: the scope lifecycle on git (worktrees, branches, the data view), claims and the
//! busy-scope queue, tags, the private marks, the project index, the create hook and the
//! hygiene pass.
//!
//! The Python service is the specification: its result shapes, error classes and texts, and
//! `scopes.db` at schema v5. scopes keeps no search index of its own: `search` ranks scopes
//! by the journal's brief posts, asked over the hub.
//!
//! The work is synchronous, as the Python's was: each verb runs on tokio's blocking pool and
//! shells out to `git`, killed with its process group when the caller cancels.

pub mod args;
pub mod claims;
pub mod db;
pub mod git;
pub mod hook;
pub mod hygiene;
pub mod lifecycle;
pub mod names;
pub mod outbound;
pub mod private;
pub mod projects;
pub mod search;
pub mod service;
pub mod sweep;
pub mod tags;

use std::collections::HashMap;
use std::ops::Deref;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use awm_plugin_sdk::{CancelToken, VerbError};
use awm_store::Store;

pub use outbound::Outbound;
pub use service::service;

/// A verb's failure as the Python raised it: the exception's class and its text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fail {
    pub class: String,
    pub message: String,
}

impl Fail {
    pub fn new(class: &str, message: impl Into<String>) -> Self {
        Self { class: class.to_string(), message: message.into() }
    }
    pub fn value(message: impl Into<String>) -> Self {
        Self::new("ValueError", message)
    }
    pub fn runtime(message: impl Into<String>) -> Self {
        Self::new("RuntimeError", message)
    }
    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new("FileNotFoundError", message)
    }
    pub fn exists(message: impl Into<String>) -> Self {
        Self::new("FileExistsError", message)
    }
    /// Python's `KeyError(msg)`, whose text is the message's repr.
    pub fn key(message: &str) -> Self {
        Self::new("KeyError", awm_pycompat::repr::str_repr(message))
    }
    /// An `OSError` subclass, which heal and the hygiene pass report rather than raise.
    pub fn is_os_error(&self) -> bool {
        matches!(self.class.as_str(), "OSError" | "FileNotFoundError" | "FileExistsError" | "PermissionError" | "IsADirectoryError" | "NotADirectoryError")
    }
}

impl std::fmt::Display for Fail {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl From<Fail> for VerbError {
    fn from(f: Fail) -> Self {
        VerbError::new(f.class, f.message)
    }
}

impl From<awm_store::StoreError> for Fail {
    fn from(e: awm_store::StoreError) -> Self {
        Fail::new("OperationalError", e.to_string())
    }
}

impl From<awm_store::rusqlite::Error> for Fail {
    fn from(e: awm_store::rusqlite::Error) -> Self {
        Fail::new("OperationalError", e.to_string())
    }
}

/// An `OSError` from the filesystem, with Python's `[Errno n] text: 'path'` message.
pub fn os_fail(e: &std::io::Error, path: &Path) -> Fail {
    let class = match e.kind() {
        std::io::ErrorKind::NotFound => "FileNotFoundError",
        std::io::ErrorKind::AlreadyExists => "FileExistsError",
        std::io::ErrorKind::PermissionDenied => "PermissionError",
        _ => "OSError",
    };
    let errno = e.raw_os_error().unwrap_or(0);
    let text = e.to_string();
    let text = text.split(" (os error").next().unwrap_or(&text).to_string();
    Fail::new(class, format!("[Errno {errno}] {text}: {}", awm_pycompat::repr::str_repr(&path.to_string_lossy())))
}

/// A failure injected at a named point.
pub type Fault = Arc<dyn Fn(&str) -> std::result::Result<(), Fail> + Send + Sync>;

pub type Result<T, E = Fail> = std::result::Result<T, E>;

/// The workspace paths a scope lives in.
#[derive(Debug, Clone)]
pub struct Paths {
    pub workspace: PathBuf,
    /// `<workspace>/projects`
    pub projects: PathBuf,
    /// `<workspace>/data`
    pub data: PathBuf,
}

impl Paths {
    pub fn new(workspace: impl Into<PathBuf>) -> Self {
        let workspace = workspace.into();
        Self { projects: workspace.join("projects"), data: workspace.join("data"), workspace }
    }

    pub fn bare(&self, project: &str) -> PathBuf {
        self.projects.join(project).join(".bare")
    }
}

/// The plugin's state, shared by every call.
pub struct App {
    pub paths: Paths,
    pub store: Store,
    pub out: Arc<dyn Outbound>,
    /// The binary the DVC hooks and merge driver call: `<AWM_DIR>/bin/dvc`.
    pub hook_bin: PathBuf,
    /// `/proc`, where a claim's process start time is read.
    pub proc_root: PathBuf,
    /// The GitHub account a fresh project's remote is made under.
    pub github_user: String,
    pub node: String,
    /// `gh`, which makes a fresh project's GitHub remote and forks; `None` makes none.
    pub gh: Option<PathBuf>,
    /// A test's injected failure at a named point of `create`.
    pub fault: Option<Fault>,
    pub rt: tokio::runtime::Handle,
    locks: Mutex<HashMap<String, Arc<Mutex<()>>>>,
}

impl App {
    pub fn new(paths: Paths, store: Store, out: Arc<dyn Outbound>, hook_bin: PathBuf, rt: tokio::runtime::Handle) -> Self {
        Self {
            paths,
            store,
            out,
            hook_bin,
            proc_root: PathBuf::from("/proc"),
            github_user: std::env::var("AWM_GITHUB_USER").ok().filter(|v| !v.is_empty()).unwrap_or_else(|| "phy0x1a79ed".into()),
            node: awm_plugin_sdk::config::node_name(),
            gh: git::which("gh"),
            fault: None,
            rt,
            locks: Mutex::new(HashMap::new()),
        }
    }

    /// Fail at `point` when a test asked for it.
    pub fn fault(&self, point: &str) -> Result<(), Fail> {
        self.fault.as_ref().map_or(Ok(()), |f| f(point))
    }

    /// The mutex that serialises the git-mutating verbs of one project.
    pub fn project_lock(&self, project: &str) -> Arc<Mutex<()>> {
        let mut locks = self.locks.lock().unwrap_or_else(|p| p.into_inner());
        locks.entry(project.to_string()).or_default().clone()
    }
}

/// One verb's run: the app and the caller's cancel token, which kills each `git` it starts.
#[derive(Clone)]
pub struct Op {
    pub app: Arc<App>,
    pub cancel: CancelToken,
}

impl Deref for Op {
    type Target = App;
    fn deref(&self) -> &App {
        &self.app
    }
}

impl Op {
    pub fn new(app: Arc<App>, cancel: CancelToken) -> Self {
        Self { app, cancel }
    }

    /// The same run with a fresh token: cleanup that must finish though the call was cancelled.
    pub fn shielded(&self) -> Op {
        Op { app: self.app.clone(), cancel: CancelToken::new() }
    }

    /// Run `f` holding the project's mutex.
    pub fn locked<T>(&self, project: &str, f: impl FnOnce() -> T) -> T {
        let lock = self.project_lock(project);
        let _held = lock.lock().unwrap_or_else(|p| p.into_inner());
        f()
    }
}

/// Python's `Path.is_relative_to`.
pub fn is_relative_to(path: &Path, base: &Path) -> bool {
    path.strip_prefix(base).is_ok()
}

/// Python's `str(path)` for a path built by joining: no trailing slash.
pub fn path_str(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}
