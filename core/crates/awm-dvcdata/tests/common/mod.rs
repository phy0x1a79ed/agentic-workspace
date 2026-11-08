//! A throwaway workspace: a bare repo with one DVC worktree, in the real awm layout,
//! where `.git` is a file and every hook lives in the common dir.

#![allow(dead_code)]

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use awm_dvcdata::provision::Layout;
use serde_json::Value;

pub struct Ws {
    pub _tmp: tempfile::TempDir,
    pub root: PathBuf,
    pub ws: PathBuf,
    pub layout: Layout,
    pub bare: PathBuf,
    pub main: PathBuf,
    pub bin: PathBuf,
}

pub fn sh(cwd: &Path, args: &[&str]) -> String {
    let o = Command::new(args[0]).args(&args[1..]).current_dir(cwd).output().expect("spawn");
    format!("{}{}", String::from_utf8_lossy(&o.stdout), String::from_utf8_lossy(&o.stderr))
}

pub fn git(repo: &Path, args: &[&str]) -> String {
    let mut v = vec!["git", "-C", repo.to_str().unwrap()];
    v.extend_from_slice(args);
    sh(repo, &v)
}

pub fn git_ok(repo: &Path, args: &[&str]) -> bool {
    Command::new("git").arg("-C").arg(repo).args(args).output().map(|o| o.status.success()).unwrap_or(false)
}

fn identity(wt: &Path) {
    git(wt, &["config", "user.email", "t@t"]);
    git(wt, &["config", "user.name", "t"]);
}

/// What `dvc init` leaves: a tracked, empty config and the gitignore for its local state.
pub fn dvc_init(wt: &Path) {
    fs::create_dir_all(wt.join(".dvc")).unwrap();
    fs::write(wt.join(".dvc/config"), "").unwrap();
    fs::write(wt.join(".dvc/.gitignore"), "/config.local\n/tmp\n/cache\n").unwrap();
    git(wt, &["add", "-A"]);
    git(wt, &["commit", "-q", "-m", "dvc init"]);
}

pub fn workspace() -> Ws {
    let tmp = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(tmp.path()).unwrap();
    let ws = root.join("workspace");
    for d in ["data", "projects", ".awm"] {
        fs::create_dir_all(ws.join(d)).unwrap();
    }
    let bare = ws.join("projects/p/.bare");
    fs::create_dir_all(bare.parent().unwrap()).unwrap();
    sh(&root, &["git", "init", "-q", "--bare", "-b", "main", bare.to_str().unwrap()]);
    let seed = root.join("seed");
    sh(&root, &["git", "clone", "-q", bare.to_str().unwrap(), seed.to_str().unwrap()]);
    identity(&seed);
    fs::write(seed.join("README.md"), "seed\n").unwrap();
    git(&seed, &["add", "-A"]);
    git(&seed, &["commit", "-q", "-m", "seed"]);
    git(&seed, &["push", "-q", "origin", "main"]);
    let main = ws.join("projects/p/main");
    git(&bare, &["worktree", "add", "-q", main.to_str().unwrap(), "main"]);
    identity(&main);
    let mut layout = Layout::new(&ws);
    layout.tmpfs = root.join("no-shm");
    Ws { _tmp: tmp, root, ws, layout, bare, main, bin: PathBuf::from(env!("CARGO_BIN_EXE_awm-dvcdata")) }
}

pub fn dvc_project() -> Ws {
    let w = workspace();
    dvc_init(&w.main);
    w
}

impl Ws {
    pub fn provision(&self, wt: &Path) -> Value {
        let scope = wt.file_name().unwrap().to_string_lossy().into_owned();
        awm_dvcdata::provision::provision_scope_data(&self.layout, "p", &scope, wt, &self.bin)
    }

    pub fn scope(&self, name: &str) -> PathBuf {
        let wt = self.ws.join("projects/p").join(name);
        git(&self.bare, &["worktree", "add", "-q", wt.to_str().unwrap(), "-b", &format!("feat/{name}"), "main"]);
        identity(&wt);
        wt
    }

    pub fn cache(&self) -> PathBuf {
        self.layout.cache_dir(None)
    }

    pub fn store(&self) -> PathBuf {
        self.ws.join("projects/p/data")
    }
}

/// Write `files` under `data/<name>` and save the chunk. Returns the chunk's id.
pub fn add_chunk(wt: &Path, name: &str, files: &[(&str, &str)]) -> String {
    let dir = wt.join("data").join(name);
    fs::create_dir_all(&dir).unwrap();
    for (f, body) in files {
        let p = dir.join(f);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, body).unwrap();
    }
    awm_dvcdata::testkit::add(wt, &format!("data/{name}")).unwrap()
}

pub fn commit_all(wt: &Path, msg: &str) {
    git(wt, &["add", "-A"]);
    git(wt, &["commit", "-q", "-m", msg]);
}

pub fn ino(p: &Path) -> u64 {
    use std::os::unix::fs::MetadataExt;
    fs::metadata(p).unwrap().ino()
}

pub fn writable(p: &Path) -> bool {
    let c = std::ffi::CString::new(p.to_str().unwrap()).unwrap();
    unsafe { libc::access(c.as_ptr(), libc::W_OK) == 0 }
}
