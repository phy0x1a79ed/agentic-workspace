//! The guarded gc: every route by which a pin reaches the cache is kept, and every
//! refusal deletes nothing.



use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use awm_dvcdata::gc::{self, GcProject, GcRequest};
use awm_dvcdata::odb::{self, Manifest};
use crate::common::*;

pub struct Layout {
    pub w: Ws,
    pub wt2: PathBuf,
    pub project: GcProject,
    pub h: BTreeMap<&'static str, String>,
}

pub const ROUTES: [&str; 5] = ["history", "merge", "lock", "staged", "untracked"];

fn remove_pin(wt: &Path, name: &str) {
    fs::remove_file(wt.join(format!("data/{name}.dvc"))).unwrap();
    awm_dvcdata::provision::chmod_dirs_writable(&wt.join("data").join(name));
    fs::remove_dir_all(wt.join("data").join(name)).unwrap();
}

/// Two worktrees whose pins reach the cache by every route gc must honour.
pub fn gc_layout() -> Layout {
    let w = dvc_project();
    let wt = w.main.clone();
    w.provision(&wt);
    let mut h = BTreeMap::new();

    h.insert("history", add_chunk(&wt, "old", &[("f.txt", "old\n")]));
    commit_all(&wt, "add old");
    remove_pin(&wt, "old");
    commit_all(&wt, "drop old");

    git(&wt, &["branch", "side"]);
    git(&wt, &["commit", "-q", "--allow-empty", "-m", "main moves"]);
    git(&wt, &["checkout", "-q", "side"]);
    git(&wt, &["commit", "-q", "--allow-empty", "-m", "side moves"]);
    git(&wt, &["checkout", "-q", "main"]);
    git(&wt, &["merge", "-q", "--no-ff", "--no-commit", "side"]);
    h.insert("merge", add_chunk(&wt, "resolved", &[("f.txt", "resolved\n")]));
    commit_all(&wt, "merge side");
    remove_pin(&wt, "resolved");
    commit_all(&wt, "drop resolved");

    let lock = add_chunk(&wt, "gen", &[("x", "gen\n")]);
    fs::remove_file(wt.join("data/gen.dvc")).unwrap();
    fs::write(wt.join("dvc.yaml"), "stages:\n  gen:\n    cmd: mkdir -p data/gen\n    outs:\n    - data/gen\n").unwrap();
    fs::write(wt.join("dvc.lock"), format!("schema: '2.0'\nstages:\n  gen:\n    cmd: mkdir -p data/gen\n    outs:\n    - path: data/gen\n      hash: md5\n      md5: {lock}\n      size: 4\n      nfiles: 1\n")).unwrap();
    h.insert("lock", lock);
    commit_all(&wt, "stage gen");
    fs::remove_file(wt.join("dvc.yaml")).unwrap();
    fs::remove_file(wt.join("dvc.lock")).unwrap();
    commit_all(&wt, "drop gen");

    h.insert("staged", add_chunk(&wt, "staged", &[("f.txt", "staged\n")]));
    git(&wt, &["add", "data/staged.dvc"]);

    let wt2 = w.bare.parent().unwrap().join("other");
    git(&w.bare, &["worktree", "add", "-q", "-b", "other", wt2.to_str().unwrap(), "main"]);
    awm_dvcdata::wiring::ensure_cache_config(&wt2, &w.layout).unwrap();
    h.insert("untracked", add_chunk(&wt2, "untracked", &[("f.txt", "untracked\n")]));

    h.insert("garbage", add_chunk(&wt, "junk", &[("f.txt", "junk\n")]));
    fs::remove_file(wt.join("data/junk.dvc")).unwrap();

    let project = GcProject { name: "p".into(), bare: w.bare.clone(), worktrees: vec![wt.clone(), wt2.clone()] };
    Layout { w, wt2, project, h }
}

pub fn objects(cache: &Path) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    let root = cache.join("files/md5");
    for shard in fs::read_dir(root).unwrap().flatten() {
        if let Ok(rd) = fs::read_dir(shard.path()) {
            for o in rd.flatten() {
                out.insert(format!("{}{}", shard.file_name().to_string_lossy(), o.file_name().to_string_lossy()));
            }
        }
    }
    out
}

pub fn closure(cache: &Path, oid: &str) -> BTreeSet<String> {
    let mut s: BTreeSet<String> = Manifest::load(cache, oid).unwrap().entries.into_iter().map(|e| e.md5).collect();
    s.insert(oid.into());
    s
}

fn run(l: &Layout, f: impl FnOnce(&mut GcRequest)) -> serde_json::Value {
    let mut req = GcRequest::new(vec![l.project.clone()], ["p".to_string()].into());
    f(&mut req);
    gc::collect_garbage(&l.w.layout, &req)
}

#[test]
fn every_route_to_a_pin_is_kept() {
    let l = gc_layout();
    let cache = l.w.cache();
    let (keep, info) = gc::project_keep_set(&l.project, &cache).unwrap();
    for r in ROUTES {
        assert!(closure(&cache, &l.h[r]).is_subset(&keep), "{r} pin not kept");
    }
    assert!(!closure(&cache, &l.h["garbage"]).is_subset(&keep));
    assert_eq!(info["missing_dir_manifests"], serde_json::json!({}));
}

#[test]
fn an_unparseable_pin_aborts() {
    let l = gc_layout();
    fs::write(l.wt2.join("data/broken.dvc"), "outs: [\n").unwrap();
    let err = gc::project_keep_set(&l.project, &l.w.cache()).unwrap_err().to_string();
    assert!(err.contains("broken.dvc"), "{err}");
}

#[test]
fn collects_garbage_and_nothing_else() {
    let l = gc_layout();
    let cache = l.w.cache();
    let young = run(&l, |r| r.dry_run = false);
    assert_eq!(young["result"], "ok", "{young}");
    assert_eq!(young["sweep"]["collected"], 0, "the grace window protects a young object");
    assert!(closure(&cache, &l.h["garbage"]).is_subset(&objects(&cache)));
    let partial = cache.join("files/md5/.abc123.tmp");
    fs::write(&partial, "partial").unwrap();
    let dry = run(&l, |r| r.grace_days = 0.0);
    assert_eq!(dry["result"], "ok");
    assert_eq!(dry["dry_run"], true);
    assert!(dry["sweep"]["collected"].as_u64().unwrap() >= 1 && dry["deleted_anything"] == false);
    assert!(objects(&cache).contains(&l.h["garbage"]), "a dry run deleted something");
    let rep = run(&l, |r| {
        r.dry_run = false;
        r.grace_days = 0.0;
    });
    assert_eq!(rep["result"], "ok");
    assert_eq!(rep["deleted_anything"], true);
    assert!(!partial.exists(), "an abandoned transfer is collected");
    let left = objects(&cache);
    assert!(!left.contains(&l.h["garbage"]));
    for r in ROUTES {
        assert!(closure(&cache, &l.h[r]).is_subset(&left), "{r} pin collected");
    }
}

#[test]
fn refuses_an_unlisted_wired_project() {
    let l = gc_layout();
    let rep = run(&l, |r| {
        r.wired = ["p".to_string(), "q".to_string()].into();
        r.grace_days = 0.0;
    });
    assert_eq!(rep["result"], "refused");
    assert!(rep["detail"].as_str().unwrap().contains("\"q\""));
    let rep = run(&l, |r| {
        r.wired = ["p".to_string(), "q".to_string()].into();
        r.exclude = vec!["q".into()];
    });
    assert_eq!(rep["result"], "ok");
}

#[test]
fn refuses_a_missing_dir_manifest() {
    let l = gc_layout();
    let cache = l.w.cache();
    fs::remove_file(odb::object_path(&cache, &l.h["history"])).unwrap();
    let before = objects(&cache);
    let rep = run(&l, |r| {
        r.dry_run = false;
        r.grace_days = 0.0;
    });
    let detail = rep["detail"].as_str().unwrap();
    assert_eq!(rep["result"], "refused");
    assert!(detail.contains(&l.h["history"]) && detail.contains("data/old.dvc"), "{detail}");
    assert_eq!(objects(&cache), before);
    let rep = run(&l, |r| r.accept_missing = vec![l.h["history"].clone()]);
    assert_eq!(rep["result"], "ok", "{rep}");
}

#[test]
fn refuses_while_another_gc_runs() {
    let l = gc_layout();
    let lock = gc::acquire_gc_lock(&l.w.cache()).unwrap();
    let rep = run(&l, |r| {
        r.dry_run = false;
        r.grace_days = 0.0;
    });
    drop(lock);
    assert_eq!(rep["result"], "refused");
    assert!(rep["detail"].as_str().unwrap().contains(&format!("pid {}", std::process::id())), "{rep}");
}

#[test]
fn refuses_an_unknown_cache_layout() {
    let l = gc_layout();
    fs::create_dir(l.w.cache().join("runs")).unwrap();
    let rep = run(&l, |r| {
        r.dry_run = false;
        r.grace_days = 0.0;
    });
    assert_eq!(rep["result"], "refused");
    assert!(rep["detail"].as_str().unwrap().contains("runs"));
}

#[test]
fn refuses_a_keep_mode_other_than_all_commits_and_an_empty_project_list() {
    let l = gc_layout();
    assert_eq!(run(&l, |r| r.keep = "workspace".into())["result"], "refused");
    assert_eq!(run(&l, |r| r.projects.clear())["result"], "refused");
}
