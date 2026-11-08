//! Fixtures: a temp store, a fake outbound, git helpers and a temp workspace.
#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};

use awm_plugin_routine::outbound::{BoxFut, Outbound, Start};
use awm_plugin_routine::runner::Runner;
use awm_plugin_routine::store::Store;

#[derive(Default)]
pub struct FakeOutbound {
    pub starts: Mutex<Vec<Start>>,
    pub dms: Mutex<Vec<String>>,
    pub fail_start: Mutex<bool>,
    pub fail_dm: Mutex<bool>,
}

impl Outbound for FakeOutbound {
    fn start_session(&self, start: Start) -> BoxFut<'_, String> {
        Box::pin(async move {
            if *self.fail_start.lock().unwrap() {
                anyhow::bail!("peer unreachable");
            }
            let mut starts = self.starts.lock().unwrap();
            starts.push(start);
            Ok(format!("job{}", starts.len()))
        })
    }

    fn notify_operator(&self, text: String) -> BoxFut<'_, ()> {
        Box::pin(async move {
            if *self.fail_dm.lock().unwrap() {
                anyhow::bail!("discord down");
            }
            self.dms.lock().unwrap().push(text);
            Ok(())
        })
    }
}

pub fn git(cwd: &Path, args: &[&str]) -> String {
    let out = Command::new("git").arg("-C").arg(cwd).args(["-c", "user.name=t", "-c", "user.email=t@t"]).args(args).output().unwrap();
    assert!(out.status.success(), "git {args:?} in {}: {}", cwd.display(), String::from_utf8_lossy(&out.stderr));
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

pub struct Fixture {
    pub tmp: tempfile::TempDir,
    pub store: Arc<Store>,
    pub out: Arc<FakeOutbound>,
    pub runner: Arc<Runner>,
}

pub fn fixture() -> Fixture {
    let tmp = tempfile::tempdir().unwrap();
    let store = Arc::new(Store::open(&tmp.path().join("routine.db")).unwrap());
    let out = Arc::new(FakeOutbound::default());
    let runner = Runner::new(store.clone(), out.clone(), None);
    Fixture { tmp, store, out, runner }
}

/// A git checkout with a routines/ folder, standing in for a project repo.
pub fn project_repo(root: &Path) -> PathBuf {
    let repo = root.join("proj");
    std::fs::create_dir_all(repo.join("routines")).unwrap();
    git(&repo, &["init", "-q", "-b", "main"]);
    std::fs::write(repo.join("README"), "x\n").unwrap();
    git(&repo, &["add", "-A"]);
    git(&repo, &["commit", "-q", "-m", "init"]);
    repo.canonicalize().unwrap()
}

pub fn write_routine(repo: &Path, name: &str, body: &str) -> PathBuf {
    let p = repo.join("routines").join(format!("{name}.toml"));
    std::fs::write(&p, body).unwrap();
    git(repo, &["add", "-A"]);
    git(repo, &["commit", "-q", "-m", &format!("routine {name}")]);
    p
}

/// A temp workspace with projects/proj/.bare, dev and release, scope worktrees and a live
/// release checkout, standing in for a node.
pub struct Workspace {
    pub root: PathBuf,
    pub proj: PathBuf,
    pub bare: PathBuf,
}

impl Workspace {
    pub fn new(root: &Path) -> Self {
        let root = root.to_path_buf();
        let proj = root.join("projects/proj");
        let bare = proj.join(".bare");
        let seed = root.join("seed");
        std::fs::create_dir_all(&seed).unwrap();
        git(&seed, &["init", "-q", "-b", "dev"]);
        std::fs::write(seed.join("app.txt"), "base\n").unwrap();
        git(&seed, &["add", "-A"]);
        git(&seed, &["commit", "-q", "-m", "base"]);
        git(&seed, &["branch", "release"]);
        std::fs::create_dir_all(&proj).unwrap();
        git(&root, &["clone", "-q", "--bare", &seed.to_string_lossy(), &bare.to_string_lossy()]);
        git(&bare, &["worktree", "add", "-q", &proj.join("release").to_string_lossy(), "release"]);
        Self { root, proj, bare }
    }

    pub fn scope(&self, name: &str, files: &[(&str, &str)], base: &str) -> PathBuf {
        let wt = self.proj.join(name);
        if !wt.exists() {
            git(&self.bare, &["worktree", "add", "-q", "-b", &format!("feat/{name}"), &wt.to_string_lossy(), base]);
        }
        let mut add = vec!["add", "-A", "--"];
        for (rel, text) in files {
            let p = wt.join(rel);
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(&p, text).unwrap();
            add.push(rel);
        }
        git(&wt, &add);
        git(&wt, &["commit", "-q", "-m", &format!("{name}: change")]);
        wt
    }

    pub fn rev(&self, r: &str) -> String {
        git(&self.bare, &["rev-parse", r])
    }

    pub fn show(&self, r: &str, path: &str) -> String {
        git(&self.bare, &["show", &format!("{r}:{path}")])
    }
}
