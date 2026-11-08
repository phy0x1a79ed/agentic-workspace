//! The hygiene pass (scopes.md R45) and the R142 `.awm/` sweep, on real git.

mod common;

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use awm_dvcdata::provision;
use common::{commit, git, Rig};
use serde_json::{json, Value};

const P: &str = "proj";

fn later() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs_f64() + 30.0 * 86400.0
}

fn scope(r: &Rig, name: &str) -> PathBuf {
    common::create(&r.op, P, name).unwrap();
    r.projects.join(P).join(name)
}

fn rig() -> Rig {
    let r = common::rig();
    common::bare_project(&r, P);
    r
}

fn scopes(v: &Value, key: &str) -> Vec<String> {
    v[key].as_array().unwrap().iter().map(|x| x["scope"].as_str().unwrap().to_string()).collect()
}

#[test]
fn a_merged_idle_clean_scope_is_removed_and_its_branch_kept() {
    let r = rig();
    let wt = scope(&r, "old");
    let report = r.op.hygiene_check(Some(later()), 14.0, false).unwrap();
    assert_eq!(scopes(&report, "removed"), ["old"]);
    assert!(!wt.exists());
    assert!(common::sha(&r.projects.join(P).join(".bare"), "refs/heads/feat/old").is_some());
    let posts = r.out.calls_to("ops_post");
    assert_eq!(posts.len(), 1);
    assert_eq!(posts[0]["kind"], json!("scope-hygiene"));
    assert_eq!(posts[0]["title"], json!("Scope hygiene: 1 removed, 0 held"));
    assert!(posts[0]["body"].as_str().unwrap().contains("- removed proj/old (restore: scope create on branch feat/old)"));
}

#[test]
fn an_unmerged_scope_is_held_with_the_reason() {
    let r = rig();
    let wt = scope(&r, "work");
    commit(&wt, "change");
    let report = r.op.hygiene_check(Some(later()), 14.0, false).unwrap();
    assert_eq!(report["removed"], json!([]));
    assert!(report["held"][0]["why"].as_str().unwrap().contains("not merged"));
    assert!(wt.exists());
}

#[test]
fn a_recently_used_scope_is_held() {
    let r = rig();
    let wt = scope(&r, "fresh");
    let report = r.op.hygiene_check(None, 14.0, false).unwrap();
    assert_eq!(report["removed"], json!([]));
    assert!(report["held"][0]["why"].as_str().unwrap().contains("day(s) ago"));
    assert!(wt.exists());
}

#[test]
fn a_claimed_scope_is_held() {
    let r = rig();
    scope(&r, "busy");
    let session = format!("cc{}", "b".repeat(32));
    r.op.claim(P, "busy", &session, Some(std::process::id() as i64), None, false, false).unwrap();
    let report = r.op.hygiene_check(Some(later()), 14.0, false).unwrap();
    assert_eq!(report["held"][0]["why"], json!(format!("in use by session {session}")));
}

#[test]
fn a_dirty_scope_is_held() {
    let r = rig();
    let wt = scope(&r, "dirty");
    std::fs::write(wt.join("notes.txt"), "x").unwrap();
    let report = r.op.hygiene_check(Some(later()), 14.0, false).unwrap();
    assert!(report["held"][0]["why"].as_str().unwrap().contains("uncommitted"));
    assert!(wt.exists());
}

#[test]
fn a_dry_run_removes_nothing_and_posts_nothing() {
    let r = rig();
    let wt = scope(&r, "old");
    let report = r.op.hygiene_check(Some(later()), 14.0, true).unwrap();
    assert_eq!(report["removed"][0]["dry_run"], json!(true));
    assert!(wt.exists());
    assert!(r.out.calls_to("ops_post").is_empty());
}

#[test]
fn a_project_with_a_create_hook_is_left_alone() {
    let r = rig();
    let wt = scope(&r, "old");
    let main = r.projects.join(P).join("main");
    git(&r.projects.join(P).join(".bare"), &["worktree", "add", "-q", &main.to_string_lossy(), "main"]);
    std::fs::write(main.join(awm_plugin_scopes::hook::FILE), "[scope]\ncreate_hook = \"seat.sh\"\n").unwrap();
    let report = r.op.hygiene_check(Some(later()), 14.0, false).unwrap();
    assert_eq!((report["removed"].clone(), report["held"].clone()), (json!([]), json!([])));
    assert!(wt.exists());
}

#[test]
fn every_tenth_create_runs_a_pass() {
    let r = rig();
    let runs = Arc::new(Mutex::new(0));
    let counts: Vec<i64> = (0..20)
        .map(|_| {
            let runs = runs.clone();
            r.op.after_create_with(false, move |_| *runs.lock().unwrap() += 1).unwrap()
        })
        .collect();
    assert_eq!((counts[19], *runs.lock().unwrap()), (20, 2));
    assert_eq!(r.op.hygiene_status().unwrap(), json!({"creates": 20, "next_check_after": 10}));
}

// ---- the R142 sweep ----------------------------------------------------------------------

const OLD_HOOK: &str = "#!/usr/bin/env bash\n# AWM-managed. Materialise the data this commit pins, after a merge moved it.\nSENTINEL=\".awm/data-checkout-failed\"\nMOUNTS=\".awm/data-mounts\"\n";

fn layout_project(r: &Rig, name: &str, scopes: &[&str], dvc: bool) -> Vec<(String, PathBuf)> {
    let seed = r.tmp.path().join(format!("seed-{name}"));
    std::fs::create_dir_all(&seed).unwrap();
    git(&seed, &["init", "-q", "-b", "main"]);
    std::fs::write(seed.join("README.md"), "x\n").unwrap();
    if dvc {
        std::fs::create_dir_all(seed.join(".dvc")).unwrap();
        std::fs::write(seed.join(".dvc/config"), "[core]\n").unwrap();
    }
    git(&seed, &["add", "-A"]);
    git(&seed, &["commit", "-q", "-m", "seed"]);
    let bare = r.projects.join(name).join(".bare");
    git(r.tmp.path(), &["clone", "-q", "--bare", &seed.to_string_lossy(), &bare.to_string_lossy()]);
    let mut out = vec![];
    for s in std::iter::once("main").chain(scopes.iter().copied()) {
        let wt = r.projects.join(name).join(s);
        if s == "main" {
            git(&bare, &["worktree", "add", "-q", &wt.to_string_lossy(), "main"]);
        } else {
            git(&bare, &["worktree", "add", "-q", "-b", &format!("feat/{s}"), &wt.to_string_lossy(), "main"]);
        }
        out.push((s.to_string(), wt));
    }
    out
}

struct Layout {
    r: Rig,
    plain: Vec<(String, PathBuf)>,
    tracked: Vec<(String, PathBuf)>,
    odd: PathBuf,
}

fn wt<'a>(set: &'a [(String, PathBuf)], scope: &str) -> &'a Path {
    &set.iter().find(|(s, _)| s == scope).unwrap().1
}

/// Three projects shaped like the live ones, each worktree with an old `.awm/`.
fn layout() -> Layout {
    let r = common::rig();
    let plain = layout_project(&r, "plain", &["dev", "lab/sub"], false);
    for (_, w) in &plain {
        let awm = w.join(".awm");
        std::fs::create_dir_all(&awm).unwrap();
        std::os::unix::fs::symlink(r.ws.join("data/plain"), awm.join("data")).unwrap();
        std::fs::write(awm.join("history.md"), "# generated\n").unwrap();
        std::fs::write(awm.join("mcp-opencode.json"), "{}\n").unwrap();
        std::os::unix::fs::symlink(r.ws.join("skills"), awm.join("skills")).unwrap();
    }
    std::fs::write(wt(&plain, "dev").join(".awm/context.md"), "# plain/dev\n\nFor the dev line.\n").unwrap();

    let tracked = layout_project(&r, "tracked", &["s1"], true);
    for (_, w) in &tracked {
        std::fs::create_dir_all(w.join(".awm")).unwrap();
        std::os::unix::fs::symlink("../data", w.join(".awm/data")).unwrap();
    }
    let s1 = wt(&tracked, "s1").join(".awm");
    std::fs::write(s1.join(provision::MOUNTS_FILE), "data/hot\n").unwrap();
    std::fs::write(s1.join(provision::CHECKOUT_FAILED_FILE), "dvc checkout FAILED after merge\n").unwrap();
    std::fs::write(r.projects.join("tracked/.bare/hooks/post-merge"), OLD_HOOK).unwrap();

    let odd = layout_project(&r, "odd", &[], false).remove(0).1;
    std::fs::create_dir_all(odd.join(".awm/data")).unwrap();
    std::fs::write(odd.join(".awm/data/notes.txt"), "someone's files\n").unwrap();
    Layout { r, plain, tracked, odd }
}

fn awm_entries(projects: &Path) -> Vec<String> {
    let mut out = vec![];
    let mut stack = vec![projects.to_path_buf()];
    while let Some(d) = stack.pop() {
        for e in std::fs::read_dir(&d).unwrap().flatten() {
            let p = e.path();
            if p.to_string_lossy().contains("/.awm") {
                out.push(p.to_string_lossy().into_owned());
            }
            if e.file_type().unwrap().is_dir() {
                stack.push(p);
            }
        }
    }
    out.sort();
    out
}

fn by_worktree<'a>(out: &'a Value, projects: &Path) -> std::collections::BTreeMap<String, &'a Value> {
    out["worktrees"].as_array().unwrap().iter().map(|w| (Path::new(w["worktree"].as_str().unwrap()).strip_prefix(projects).unwrap().to_string_lossy().into_owned(), w)).collect()
}

#[test]
fn a_dry_run_reports_and_changes_nothing() {
    let l = layout();
    let before = awm_entries(&l.r.projects);
    let out = l.r.op.awm_sweep(true).unwrap();
    assert_eq!(out["errors"], json!([]));
    assert_eq!(awm_entries(&l.r.projects), before);
    let by = by_worktree(&out, &l.r.projects);
    assert_eq!(by["plain/lab/sub"]["awm_dir"], json!("removed"));
    assert_eq!(by["plain/dev"]["left"], json!(["context.md"]));
    assert_eq!(out["hooks"], json!({"tracked": "would-rewrite"}));
}

#[test]
fn the_sweep_empties_every_awm_dir_it_can() {
    let l = layout();
    let out = l.r.op.awm_sweep(false).unwrap();
    assert_eq!(out["errors"], json!([]));
    for s in ["main", "lab/sub"] {
        assert!(!wt(&l.plain, s).join(".awm").exists());
    }
    for (_, w) in &l.tracked {
        assert!(!w.join(".awm").exists());
    }
    assert_eq!(out["left_behind"], json!(2));
    let left: Vec<String> = std::fs::read_dir(wt(&l.plain, "dev").join(".awm")).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).collect();
    assert_eq!(left, ["context.md"], "context.md stays for the briefs one-shot");
    let odd = out["worktrees"].as_array().unwrap().iter().find(|w| w["project"] == "odd").unwrap();
    assert_eq!(odd["left"], json!(["data"]));
    assert!(l.odd.join(".awm/data/notes.txt").exists());
}

#[test]
fn the_hook_state_moves_into_the_git_dir_and_the_hooks_are_rewritten() {
    let l = layout();
    let s1 = wt(&l.tracked, "s1");
    let out = l.r.op.awm_sweep(false).unwrap();
    let state = provision::state_dir(s1);
    assert_eq!(state, l.r.projects.join("tracked/.bare/worktrees/s1/awm"));
    assert_eq!(provision::read_mounts(s1), ["data/hot"]);
    assert!(std::fs::read_to_string(state.join(provision::CHECKOUT_FAILED_FILE)).unwrap().contains("FAILED after merge"));
    assert_eq!(out["hooks"]["tracked"]["post-merge_hook"], json!("installed"));
    let hook = std::fs::read_to_string(l.r.projects.join("tracked/.bare/hooks/post-merge")).unwrap();
    assert!(!hook.contains(".awm/") && hook.contains("git rev-parse --git-dir"), "{hook}");
}

#[test]
fn a_second_run_finds_nothing_to_do() {
    let l = layout();
    l.r.op.awm_sweep(false).unwrap();
    let again = l.r.op.awm_sweep(false).unwrap();
    let busy: Vec<&Value> = again["worktrees"].as_array().unwrap().iter().filter(|w| w.as_object().unwrap().keys().any(|k| !["project", "worktree", "left"].contains(&k.as_str()))).collect();
    assert!(busy.is_empty(), "{busy:?}");
    assert_eq!(again["hooks"]["tracked"], json!({}));
}

#[test]
fn a_build_stamp_moves_into_the_git_dir() {
    let l = layout();
    let main = wt(&l.plain, "main");
    std::fs::write(main.join(".awm/dsh-build-stamp"), "head=abc dirty=0 lock=x\n").unwrap();
    l.r.op.awm_sweep(false).unwrap();
    assert!(!main.join(".awm").exists());
    assert_eq!(std::fs::read_to_string(provision::state_dir(main).join("dsh-build-stamp")).unwrap(), "head=abc dirty=0 lock=x\n");
}

#[test]
fn a_peer_may_not_sweep() {
    let l = layout();
    let e = l.r.call("awm_sweep", json!({"dry_run": true}), Some("peer:altair")).unwrap_err();
    assert_eq!((e.error_class.as_str(), e.message.as_str()), ("PermissionError", "scope_awm_sweep runs on its own node"));
    assert_eq!(l.r.call("awm_sweep", json!({"dry_run": true}), None).unwrap()["dry_run"], json!(true));
}
