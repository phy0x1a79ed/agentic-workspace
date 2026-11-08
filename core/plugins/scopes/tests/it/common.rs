//! A workspace in a temp dir: `projects/`, `data/`, `.awm/` as `AWM_DIR`, a fresh
//! `scopes.db`, and a recorder in place of the gateway. Git runs with a fixed identity and
//! pinned commit dates, and no user or system config.
#![allow(dead_code)]

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
use std::time::Duration;

use awm_plugin_scopes::{db, App, Fail, Op, Outbound, Paths};
use awm_plugin_sdk::{CancelToken, VerbError};
use serde_json::{json, Value};

/// Tests share `AWM_DIR` and the git env, so each holds this for its whole run.
static ENV: Mutex<()> = Mutex::new(());

fn runtime() -> &'static tokio::runtime::Runtime {
    static RT: OnceLock<tokio::runtime::Runtime> = OnceLock::new();
    RT.get_or_init(|| tokio::runtime::Builder::new_multi_thread().worker_threads(2).enable_all().build().unwrap())
}

/// The gateway as tests see it: every call recorded, answers set per verb.
#[derive(Default)]
pub struct Recorder {
    pub calls: Mutex<Vec<(String, String, Value)>>,
    pub waiting: Mutex<HashSet<String>>,
    pub refuse: Mutex<HashSet<String>>,
    pub answers: Mutex<HashMap<String, Value>>,
}

impl Recorder {
    pub fn calls_to(&self, verb: &str) -> Vec<Value> {
        self.calls.lock().unwrap().iter().filter(|(_, v, _)| v == verb).map(|(_, _, a)| a.clone()).collect()
    }
}

impl Outbound for Recorder {
    fn call(&self, tool: &str, verb: &str, args: Value, _timeout: Duration) -> Result<Value, String> {
        self.calls.lock().unwrap().push((tool.into(), verb.into(), args));
        if self.refuse.lock().unwrap().contains(verb) {
            return Err(format!("{tool}.{verb}: down"));
        }
        if verb == "waits" {
            let w: Vec<Value> = self.waiting.lock().unwrap().iter().map(|s| json!({"session_id": s})).collect();
            return Ok(json!({"waits": w}));
        }
        Ok(self.answers.lock().unwrap().get(verb).cloned().unwrap_or_else(|| json!({"ok": true})))
    }
}

pub struct Rig {
    pub tmp: tempfile::TempDir,
    pub ws: PathBuf,
    pub projects: PathBuf,
    pub awm_dir: PathBuf,
    pub out: Arc<Recorder>,
    pub op: Op,
    pub ts: awm_plugin_sdk::TestService,
    _env: MutexGuard<'static, ()>,
}

impl Rig {
    /// Call a verb through the service, as the gateway would, stamped `as_`.
    pub fn call(&self, verb: &str, args: Value, as_: Option<&str>) -> Result<Value, VerbError> {
        let caller = awm_plugin_sdk::Caller { node: "testnode".into(), ..Default::default() };
        runtime().block_on(self.ts.call_as(verb, args, caller, as_))
    }
}

pub fn env(awm_dir: &Path) -> awm_plugin_sdk::Env {
    awm_plugin_sdk::Env {
        hub_url: "http://127.0.0.1:1".into(),
        service: "scopes".into(),
        plugin: "scopes".into(),
        node: "testnode".into(),
        awm_dir: awm_dir.into(),
        token_file: None,
    }
}

pub fn rig() -> Rig {
    rig_with(|_| {})
}

pub fn rig_with(tune: impl FnOnce(&mut App)) -> Rig {
    let guard = ENV.lock().unwrap_or_else(|p| p.into_inner());
    let tmp = tempfile::tempdir().unwrap();
    let ws = tmp.path().join("workspace");
    let awm_dir = ws.join(".awm");
    for d in [ws.join("projects"), ws.join("data"), awm_dir.join("services")] {
        std::fs::create_dir_all(&d).unwrap();
    }
    for (k, v) in [
        ("GIT_AUTHOR_NAME", "t"),
        ("GIT_AUTHOR_EMAIL", "t@t"),
        ("GIT_COMMITTER_NAME", "t"),
        ("GIT_COMMITTER_EMAIL", "t@t"),
        ("GIT_AUTHOR_DATE", "2026-01-02T03:04:05Z"),
        ("GIT_COMMITTER_DATE", "2026-01-02T03:04:05Z"),
        ("GIT_CONFIG_NOSYSTEM", "1"),
        ("GIT_CONFIG_GLOBAL", "/dev/null"),
    ] {
        std::env::set_var(k, v);
    }
    std::env::set_var("AWM_DIR", &awm_dir);
    std::env::set_var("AWM_WORKSPACE", &ws);
    std::env::remove_var("AWM_DATA_DVC");
    let store = db::open(&awm_dir.join("services/scopes/scopes.db")).unwrap();
    let out = Arc::new(Recorder::default());
    let mut app = App::new(Paths::new(&ws), store, out.clone(), tmp.path().join("bin/dvc"), runtime().handle().clone());
    app.gh = None;
    app.node = "testnode".into();
    tune(&mut app);
    let app = Arc::new(app);
    let op = Op::new(app.clone(), CancelToken::new());
    let svc = awm_plugin_scopes::service(awm_plugin_sdk::Service::new(env(&awm_dir)), app);
    let ts = runtime().block_on(svc.into_test()).unwrap();
    Rig { projects: ws.join("projects"), tmp, ws, awm_dir, out, op, ts, _env: guard }
}

/// `git -C <dir> <args>`, which must succeed; its stdout trimmed.
pub fn git(dir: &Path, args: &[&str]) -> String {
    let out = std::process::Command::new("git").arg("-C").arg(dir).args(args).env("LC_ALL", "C").output().unwrap();
    assert!(out.status.success(), "git {args:?} in {}: {}", dir.display(), String::from_utf8_lossy(&out.stderr));
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

/// `git -C <dir> <args>` that may fail: `(ok, stdout)`.
pub fn git_try(dir: &Path, args: &[&str]) -> (bool, String) {
    let out = std::process::Command::new("git").arg("-C").arg(dir).args(args).output().unwrap();
    (out.status.success(), String::from_utf8_lossy(&out.stdout).trim().to_string())
}

pub fn commit(wt: &Path, name: &str) -> String {
    std::fs::write(wt.join(name), name).unwrap();
    git(wt, &["add", name]);
    git(wt, &["commit", "-q", "-m", name]);
    git(wt, &["rev-parse", "HEAD"])
}

/// A one-commit repo on `main` at `dir`.
pub fn seed(dir: &Path) -> PathBuf {
    std::fs::create_dir_all(dir).unwrap();
    git(dir, &["init", "-q", "-b", "main"]);
    commit(dir, "README");
    dir.to_path_buf()
}

/// `projects/<project>/.bare`, cloned from a fresh seed.
pub fn bare_project(r: &Rig, project: &str) -> PathBuf {
    let seed_dir = seed(&r.tmp.path().join(format!("seed-{project}")));
    let bare = r.projects.join(project).join(".bare");
    std::fs::create_dir_all(bare.parent().unwrap()).unwrap();
    git(r.tmp.path(), &["clone", "--bare", "-q", &seed_dir.to_string_lossy(), &bare.to_string_lossy()]);
    bare
}

/// Three scopes over two projects, one active and two retired, with worktree dirs outside
/// `projects/` and stub `.bare` dirs: the Python's `seeded_scopes`.
pub fn seeded(r: &Rig) -> Vec<PathBuf> {
    let wts = r.ws.join("test_worktrees");
    let rows = [("proj-a", "scope-1", "active"), ("proj-a", "scope-2", "retired"), ("proj-b", "scope-3", "retired")];
    let mut out = vec![];
    r.op.write(|c| {
        for p in ["proj-a", "proj-b"] {
            db::ensure_project(c, p, &r.projects.join(p).join(".bare").to_string_lossy(), None)?;
        }
        for (p, s, status) in rows {
            let wt = wts.join(p).join(s).to_string_lossy().into_owned();
            let branch = format!("feat/{s}");
            db::ensure_agent(c, p, s, &db::NewAgent { branch: Some(&branch), worktree: &wt, status, ..db::NewAgent::default() })?;
        }
        Ok::<_, Fail>(())
    })
    .unwrap();
    for (p, s, _) in rows {
        let wt = wts.join(p).join(s);
        std::fs::create_dir_all(wt.join("results")).unwrap();
        std::fs::write(wt.join("AGENTS.md"), format!("# {p}/{s}\n")).unwrap();
        std::fs::create_dir_all(r.projects.join(p).join(s)).unwrap();
        out.push(wt);
    }
    for p in ["proj-a", "proj-b"] {
        std::fs::create_dir_all(r.projects.join(p).join(".bare")).unwrap();
    }
    out
}

/// `create {project, scope}` with nothing else.
pub fn create(op: &Op, project: &str, scope: &str) -> Result<awm_plugin_scopes::lifecycle::Action, Fail> {
    op.create_scope(&awm_plugin_scopes::lifecycle::CreateReq { project: project.into(), scope: Some(scope.into()), ..Default::default() })
}

pub fn agent_id(op: &Op, project: &str, scope: &str) -> Option<String> {
    op.read(|c| db::agent_id(c, project, scope, true)).unwrap()
}

pub fn sha(bare: &Path, refname: &str) -> Option<String> {
    let (ok, out) = git_try(bare, &["rev-parse", "--verify", "--quiet", refname]);
    (ok && !out.is_empty()).then_some(out)
}

/// The search reply's `(project, scope)` pairs.
pub fn pairs(v: &Value) -> Vec<(String, String)> {
    v["scopes"].as_array().unwrap().iter().map(|s| (s["project"].as_str().unwrap().into(), s["scope"].as_str().unwrap().into())).collect()
}

pub fn search(op: &Op, status: Option<&str>, project: Option<&str>, query: Option<&str>, as_: Option<&str>) -> Value {
    let ask = awm_plugin_scopes::search::Ask {
        query: query.map(Into::into),
        status: status.map(Into::into),
        project: project.map(Into::into),
        limit: 50,
        offset: 0,
        tag: None,
    };
    op.search_scopes(&ask, as_).unwrap()
}
