//! A throwaway workspace and a scripted Globus, shared by the integration tests. Every id
//! here is synthetic, and nothing reaches the live workspace or the network.

#![allow(dead_code)]

use std::collections::VecDeque;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};

use awm_plugin_dvc::config::ChinookConfig;
use awm_plugin_dvc::globus::{Api, GlobusError};
use awm_plugin_dvc::runs::RunsDao;
use awm_plugin_dvc::Paths;
use serde_json::{json, Value};

pub struct Ws {
    pub _tmp: tempfile::TempDir,
    /// The canonical temp dir that holds the workspace.
    pub root: PathBuf,
    pub paths: Paths,
}

impl Ws {
    pub fn ws(&self) -> &Path {
        &self.paths.workspace
    }

    pub fn projects(&self) -> PathBuf {
        self.paths.projects()
    }

    pub fn dao(&self) -> Arc<RunsDao> {
        Arc::new(RunsDao::open(&self.paths.db()).unwrap())
    }

    /// A `dvc.toml` with synthetic endpoints.
    pub fn write_config(&self) {
        fs::write(
            self.paths.awm_dir.join("dvc.toml"),
            "[chinook]\nlocal_endpoint = \"local-uuid\"\nremote_endpoint = \"remote-uuid\"\nprefix = \"/backups/node\"\n",
        )
        .unwrap();
    }
}

/// `<tmp>/workspace` with `.awm`, `projects` and `data`.
pub fn workspace() -> Ws {
    let tmp = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(tmp.path()).unwrap();
    let ws = root.join("workspace");
    for d in [".awm", "projects", "data"] {
        fs::create_dir_all(ws.join(d)).unwrap();
    }
    let paths = Paths { workspace: ws.clone(), awm_dir: ws.join(".awm") };
    Ws { _tmp: tmp, root, paths }
}

/// A bare `Paths` rooted at `dir` itself, for tests that treat a temp dir as the workspace.
pub fn paths_at(dir: &Path) -> Paths {
    Paths { workspace: dir.to_path_buf(), awm_dir: dir.join(".awm") }
}

pub fn tempdir() -> (tempfile::TempDir, PathBuf) {
    let tmp = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(tmp.path()).unwrap();
    (tmp, root)
}

pub fn cfg(prefix: &str) -> ChinookConfig {
    ChinookConfig { local_endpoint: "local-uuid".into(), remote_endpoint: "remote-uuid".into(), prefix: prefix.into() }
}

pub fn touch(path: &Path, text: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}

/// Run git with a clean environment, HOME at `home`, so no developer config leaks in.
pub fn git_in(home: &Path, args: &[&str]) -> String {
    let o = Command::new("git")
        .args(args)
        .env_clear()
        .env("PATH", std::env::var("PATH").unwrap_or_else(|_| "/usr/bin:/bin".into()))
        .env("HOME", home)
        .env("GIT_AUTHOR_NAME", "t")
        .env("GIT_AUTHOR_EMAIL", "t@t")
        .env("GIT_COMMITTER_NAME", "t")
        .env("GIT_COMMITTER_EMAIL", "t@t")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .output()
        .expect("spawn git");
    assert!(o.status.success(), "git {args:?} failed: {}", String::from_utf8_lossy(&o.stderr));
    String::from_utf8_lossy(&o.stdout).into_owned()
}

/// `git -C repo <args>`, HOME at the repo.
pub fn git(repo: &Path, args: &[&str]) -> String {
    let mut v = vec!["-C", repo.to_str().unwrap()];
    v.extend_from_slice(args);
    git_in(repo, &v)
}

/// A bare repo at `projects/<name>/.bare` seeded with one commit on `main`.
pub fn bare_project(ws: &Ws, name: &str) -> PathBuf {
    let bare = ws.projects().join(name).join(".bare");
    fs::create_dir_all(bare.parent().unwrap()).unwrap();
    git_in(&ws.root, &["init", "-q", "--bare", "-b", "main", bare.to_str().unwrap()]);
    let seed = ws.root.join(format!("seed-{name}"));
    git_in(&ws.root, &["clone", "-q", bare.to_str().unwrap(), seed.to_str().unwrap()]);
    fs::write(seed.join("README.md"), "seed\n").unwrap();
    git(&seed, &["add", "-A"]);
    git(&seed, &["commit", "-q", "-m", "seed"]);
    git(&seed, &["push", "-q", "origin", "main"]);
    bare
}

// -- a scripted Globus ---------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct ApiCall {
    pub method: &'static str,
    pub path: String,
    pub body: Option<Value>,
}

type Handler = Box<dyn Fn(&str, &str, Option<&Value>) -> Result<Value, GlobusError> + Send + Sync>;

/// A `globus::Api` that answers from a closure and records every call.
pub struct FakeApi {
    pub calls: Mutex<Vec<ApiCall>>,
    handler: Handler,
}

impl FakeApi {
    pub fn new(h: impl Fn(&str, &str, Option<&Value>) -> Result<Value, GlobusError> + Send + Sync + 'static) -> Arc<Self> {
        Arc::new(Self { calls: Mutex::new(vec![]), handler: Box::new(h) })
    }

    pub fn calls(&self) -> Vec<ApiCall> {
        self.calls.lock().unwrap().clone()
    }

    pub fn posts(&self) -> Vec<Value> {
        self.calls().into_iter().filter(|c| c.method == "POST").filter_map(|c| c.body).collect()
    }

    fn answer(&self, method: &'static str, path: &str, body: Option<&Value>) -> Result<Value, GlobusError> {
        self.calls.lock().unwrap().push(ApiCall { method, path: path.into(), body: body.cloned() });
        (self.handler)(method, path, body)
    }
}

impl Api for FakeApi {
    fn get(&self, path: &str) -> Result<Value, GlobusError> {
        self.answer("GET", path, None)
    }
    fn post(&self, path: &str, body: &Value) -> Result<Value, GlobusError> {
        self.answer("POST", path, Some(body))
    }
}

/// A Globus that mints a fresh submission id and task id per submit.
pub fn submitting_api() -> Arc<FakeApi> {
    let n = Arc::new(Mutex::new(0u32));
    FakeApi::new(move |method, path, _| {
        let mut n = n.lock().unwrap();
        match (method, path) {
            ("GET", "/submission_id") => {
                *n += 1;
                Ok(json!({"value": format!("sub-{n}")}))
            }
            ("POST", "/transfer") => Ok(json!({"task_id": format!("task-{n}")})),
            other => Err(GlobusError(format!("unscripted call {other:?}"))),
        }
    })
}

/// A Globus whose task polls come from `docs` in order, then from `then` (or an error), and
/// whose skipped-error list is `skipped`. It refuses every submit.
pub fn task_api(docs: Vec<Value>, then: Option<Value>, skipped: Vec<Value>) -> Arc<FakeApi> {
    let queue = Mutex::new(VecDeque::from(docs));
    FakeApi::new(move |method, path, _| {
        if method != "GET" || path == "/submission_id" {
            panic!("the watcher must never submit: {method} {path}");
        }
        if path.contains("/skipped_errors") {
            return Ok(json!({"DATA": skipped}));
        }
        if let Some(d) = queue.lock().unwrap().pop_front() {
            return Ok(d);
        }
        then.clone().ok_or_else(|| GlobusError("the watcher polled past the end of the script".into()))
    })
}

/// A Globus task document.
pub fn doc(status: &str, extra: Value) -> Value {
    let mut d = json!({
        "status": status, "files": 10, "files_transferred": 0, "files_skipped": 0,
        "bytes_transferred": 0, "faults": 0, "nice_status": "",
    });
    if let Value::Object(m) = extra {
        for (k, v) in m {
            d[k] = v;
        }
    }
    d
}

// -- the watcher ---------------------------------------------------------------------------

pub type Events = Arc<Mutex<Vec<Value>>>;

/// An `Emit` that keeps every payload.
pub fn recorder() -> (Events, awm_plugin_dvc::watcher::Emit) {
    let events: Events = Arc::default();
    let sink = events.clone();
    (events, Arc::new(move |p| sink.lock().unwrap().push(p)))
}

pub fn statuses(events: &Events) -> Vec<String> {
    events.lock().unwrap().iter().map(|p| p["status"].as_str().unwrap_or("").to_string()).collect()
}

/// A watcher that polls every millisecond and never sweeps on its own.
pub fn watcher(dao: Arc<RunsDao>, api: Arc<FakeApi>, emit: awm_plugin_dvc::watcher::Emit) -> Arc<awm_plugin_dvc::watcher::Watcher> {
    let timing = awm_plugin_dvc::watcher::Timing { poll: std::time::Duration::from_millis(1), sweep: std::time::Duration::from_secs(3600) };
    awm_plugin_dvc::watcher::Watcher::new(dao, api, emit, timing)
}

/// Wait until no watch is alive.
pub async fn settle(w: &awm_plugin_dvc::watcher::Watcher) {
    let start = std::time::Instant::now();
    while w.health()["watching"].as_u64() != Some(0) {
        assert!(start.elapsed() < std::time::Duration::from_secs(20), "the watcher never reached a verdict");
        tokio::time::sleep(std::time::Duration::from_millis(2)).await;
    }
}

/// A run mid-transfer: claimed and submitted, never polled.
pub fn live_run(dao: &RunsDao, job: &str, task: &str) -> awm_plugin_dvc::runs::Row {
    let run = dao.begin_run(job, "schedule", "").unwrap().unwrap();
    let id = run["id"].as_str().unwrap().to_string();
    dao.mark_submitted(&id, task, "").unwrap();
    dao.get_run(&id).unwrap().unwrap()
}
