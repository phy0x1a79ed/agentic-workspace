//! The scope lifecycle on real git: create, existing branches, rollback, nesting, delete
//! and complete guards, repair, heal and its audit, gather and scatter, and projects.

use crate::common;

use std::path::{Path, PathBuf};
use std::sync::Arc;

use awm_plugin_scopes::lifecycle::CreateReq;
use awm_plugin_scopes::Fail;
use common::{commit, git, git_try, sha, Rig};
use serde_json::{json, Value};

const P: &str = "proj";

fn create_with(r: &Rig, scope: &str, tune: impl FnOnce(&mut CreateReq)) -> Result<awm_plugin_scopes::lifecycle::Action, Fail> {
    let mut req = CreateReq { project: P.into(), scope: Some(scope.into()), ..Default::default() };
    tune(&mut req);
    r.op.create_scope(&req)
}

fn assert_rolled_back(r: &Rig, scope: &str) {
    assert!(!r.projects.join(P).join(scope).exists(), "{scope} left a worktree");
    assert_eq!(common::agent_id(&r.op, P, scope), None, "{scope} left a row");
}

/// A project cloned from an origin and fetched, so pushed branches have remote copies.
fn with_origin(r: &Rig) -> (PathBuf, PathBuf) {
    let seed = common::seed(&r.tmp.path().join("seed"));
    let origin = r.tmp.path().join("origin.git");
    git(r.tmp.path(), &["clone", "--bare", "-q", &seed.to_string_lossy(), &origin.to_string_lossy()]);
    let bare = r.projects.join(P).join(".bare");
    git(r.tmp.path(), &["clone", "--bare", "-q", &origin.to_string_lossy(), &bare.to_string_lossy()]);
    git(&bare, &["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"]);
    git(&bare, &["fetch", "-q", "origin"]);
    (bare, seed)
}

// ---- create ------------------------------------------------------------------------------

#[test]
fn create_makes_a_feat_branch_worktree_and_row() {
    let r = common::rig();
    common::bare_project(&r, P);
    let out = create_with(&r, "work", |_| {}).unwrap();
    let wt = r.projects.join(P).join("work");
    assert_eq!(git(&wt, &["rev-parse", "--abbrev-ref", "HEAD"]), "feat/work");
    assert_eq!(out.to_json(), json!({"project": P, "scope": "work", "status": "active",
        "message": "Created scope at projects/proj/work on new branch feat/work from main, session 1", "session": 1, "discarded": null}));
    assert!(common::agent_id(&r.op, P, "work").is_some());
    assert!(!wt.join(".awm").exists());
    let res = create_with(&r, "rel", |q| q.branch_name = Some("release".into())).unwrap();
    assert_eq!(git(&r.projects.join(P).join("rel"), &["rev-parse", "--abbrev-ref", "HEAD"]), "release");
    assert_eq!(res.session, Some(1));
}

fn repos(r: &Rig) -> (PathBuf, PathBuf) {
    let (bare, seed) = with_origin(r);
    let capella = r.tmp.path().join("capella.git");
    git(r.tmp.path(), &["clone", "--bare", "-q", &seed.to_string_lossy(), &capella.to_string_lossy()]);
    git(&bare, &["remote", "add", "capella", &capella.to_string_lossy()]);
    git(&bare, &["fetch", "-q", "--all"]);
    (bare, seed)
}

/// Put `branch` with one commit on each named remote, then fetch.
fn publish(r: &Rig, seed: &Path, branch: &str, remotes: &[&str]) -> String {
    git(seed, &["checkout", "-q", "-B", branch, "main"]);
    let sha = commit(seed, &branch.replace('/', "_"));
    git(seed, &["checkout", "-q", "main"]);
    for remote in remotes {
        let url = r.tmp.path().join(format!("{remote}.git"));
        git(seed, &["push", "-q", &url.to_string_lossy(), &format!("{branch}:{branch}")]);
    }
    git(&r.projects.join(P).join(".bare"), &["fetch", "-q", "--all"]);
    sha
}

/// A local branch with a commit no other ref holds.
fn unpushed(r: &Rig, branch: &str) -> String {
    let bare = r.projects.join(P).join(".bare");
    let scratch = r.tmp.path().join("scratch");
    git(&bare, &["worktree", "add", "-q", "-b", branch, &scratch.to_string_lossy(), "main"]);
    let sha = commit(&scratch, "unpushed");
    git(&bare, &["worktree", "remove", &scratch.to_string_lossy()]);
    sha
}

fn upstream(wt: &Path, branch: &str) -> String {
    git(wt, &["rev-parse", "--abbrev-ref", &format!("{branch}@{{upstream}}")])
}

#[test]
fn create_adopts_an_existing_local_or_remote_branch() {
    let r = common::rig();
    let (bare, seed) = repos(&r);
    let x = unpushed(&r, "feat/x");
    let out = create_with(&r, "x", |_| {}).unwrap();
    assert_eq!(git(&r.projects.join(P).join("x"), &["rev-parse", "HEAD"]), x);
    assert!(out.message.contains("existing branch feat/x"));

    let y = publish(&r, &seed, "feat/y", &["origin"]);
    create_with(&r, "y", |_| {}).unwrap();
    let wt = r.projects.join(P).join("y");
    assert_eq!((git(&wt, &["rev-parse", "HEAD"]), upstream(&wt, "feat/y")), (y, "origin/feat/y".into()));

    publish(&r, &seed, "feat/z", &["origin"]);
    git(&bare, &["branch", "--no-track", "feat/z", "origin/feat/z"]);
    create_with(&r, "z", |_| {}).unwrap();
    assert_eq!(upstream(&r.projects.join(P).join("z"), "feat/z"), "origin/feat/z");
}

fn failing(point: &'static str, class: &'static str) -> impl FnOnce(&mut awm_plugin_scopes::App) {
    move |app| app.fault = Some(Arc::new(move |p| if p == point { Err(Fail::new(class, "index down")) } else { Ok(()) }))
}

#[test]
fn a_failed_create_keeps_a_pre_existing_branch() {
    let r = common::rig_with(failing("registered", "RuntimeError"));
    repos(&r);
    let bare = r.projects.join(P).join(".bare");
    let x = unpushed(&r, "feat/x");
    let e = create_with(&r, "x", |_| {}).unwrap_err();
    assert_eq!(e.message, "index down");
    assert_rolled_back(&r, "x");
    assert_eq!(sha(&bare, "refs/heads/feat/x"), Some(x));
    assert!(create_with(&r, "fresh", |_| {}).is_err());
    assert_rolled_back(&r, "fresh");
    assert_eq!(sha(&bare, "refs/heads/feat/fresh"), None, "a branch this call made goes");
}

#[test]
fn a_made_branch_that_moved_survives_the_rollback() {
    let moved = Arc::new(std::sync::Mutex::new(None::<String>));
    let seen = moved.clone();
    let r = common::rig_with(move |app| {
        let ws = app.paths.projects.clone();
        app.fault = Some(Arc::new(move |p| {
            if p == "registered" {
                *seen.lock().unwrap() = Some(commit(&ws.join(P).join("fresh"), "late"));
                return Err(Fail::runtime("index down"));
            }
            Ok(())
        }));
    });
    repos(&r);
    assert!(create_with(&r, "fresh", |_| {}).is_err());
    assert_eq!(sha(&r.projects.join(P).join(".bare"), "refs/heads/feat/fresh"), moved.lock().unwrap().clone());
}

// ---- nesting -----------------------------------------------------------------------------

/// `metasmith` with a flat scope and two nested ones, all made by hand.
fn nested(r: &Rig) -> PathBuf {
    let bare = common::bare_project(r, "metasmith");
    let proj = bare.parent().unwrap().to_path_buf();
    for (b, d) in [("feat/monorepo", "monorepo"), ("fabfos/dev", "fabfos/dev"), ("engine/dev", "engine/dev")] {
        git(&bare, &["worktree", "add", "-q", "-b", b, &proj.join(d).to_string_lossy(), "main"]);
    }
    proj
}

#[test]
fn removing_the_last_nested_scope_takes_its_container_only() {
    let r = common::rig();
    let proj = nested(&r);
    let bare = proj.join(".bare");
    git(&bare, &["worktree", "add", "-q", "-b", "fabfos/bench", &proj.join("fabfos/bench").to_string_lossy(), "main"]);
    r.op.cleanup_worktree(&bare, &proj.join("fabfos/dev"), false).unwrap();
    assert!(proj.join("fabfos/bench").exists());
    r.op.cleanup_worktree(&bare, &proj.join("fabfos/bench"), false).unwrap();
    assert!(!proj.join("fabfos").exists() && proj.exists());
    let stale = proj.join("stale");
    std::fs::create_dir_all(&stale).unwrap();
    std::fs::write(stale.join("leftover.txt"), "x").unwrap();
    r.op.cleanup_worktree(&bare, &stale, false).unwrap();
    assert!(!stale.exists());
}

// ---- delete and complete -----------------------------------------------------------------

fn origin_scope(r: &Rig) -> PathBuf {
    with_origin(r);
    create_with(r, "s", |_| {}).unwrap();
    r.projects.join(P).join("s")
}

#[test]
fn a_clean_merged_scope_deletes_and_ignored_files_never_block() {
    let r = common::rig();
    let s = origin_scope(&r);
    std::fs::write(s.join(".gitignore"), "secrets.env\n").unwrap();
    git(&s, &["add", ".gitignore"]);
    git(&s, &["commit", "-q", "-m", "ignore"]);
    git(&r.projects.join(P).join(".bare"), &["push", "-q", "origin", "feat/s"]);
    std::fs::write(s.join("secrets.env"), "x").unwrap();
    let out = r.op.delete_scope(P, "s", false, false).unwrap();
    assert_eq!(out.to_json(), json!({"project": P, "scope": "s", "status": "deleted", "message": "Scope deleted, worktree removed, branch feat/s kept", "session": null, "discarded": null}));
    assert!(!s.exists());
}

#[test]
fn a_live_claim_blocks_even_with_force_until_released() {
    let r = common::rig();
    let s = origin_scope(&r);
    r.op.claim(P, "s", "cc".repeat(17).as_str(), Some(std::process::id() as i64), false, false).unwrap();
    let e = r.op.delete_scope(P, "s", true, false).unwrap_err();
    assert!(e.class == "RuntimeError" && e.message.contains("force does not override"));
    assert!(s.exists());
    r.op.release(P, "s", "cc".repeat(17).as_str()).unwrap();
    assert_eq!(r.op.delete_scope(P, "s", false, false).unwrap().status, "deleted");
}

#[test]
fn uncommitted_files_and_pins_block_and_a_force_lists_them_all() {
    let r = common::rig();
    let s = origin_scope(&r);
    std::fs::create_dir_all(s.join("data")).unwrap();
    std::fs::write(s.join("data/raw.dvc"), "outs:\n- md5: 0\n  path: raw\n").unwrap();
    let e = r.op.delete_scope(P, "s", false, false).unwrap_err();
    assert!(e.message.contains("uncommitted"));
    for i in 0..8 {
        std::fs::write(s.join(format!("f{i}.txt")), "x").unwrap();
    }
    let out = r.op.delete_scope(P, "s", true, false).unwrap();
    assert_eq!(out.discarded.unwrap()["uncommitted"].as_array().unwrap().len(), 9);
}

#[test]
fn unique_commits_block_unless_forced_and_a_pushed_branch_has_none() {
    let r = common::rig();
    let s = origin_scope(&r);
    let bare = r.projects.join(P).join(".bare");
    let head = commit(&s, "work");
    let e = r.op.delete_scope(P, "s", false, true).unwrap_err();
    assert!(e.message.contains("no other branch or remote holds"));
    git(&bare, &["push", "-q", "origin", "feat/s"]);
    git(&bare, &["fetch", "-q", "origin"]);
    assert!(r.op.unique_commits(&bare, "feat/s").unwrap().is_empty());
    commit(&s, "more");
    let out = r.op.delete_scope(P, "s", true, true).unwrap();
    let commits = out.discarded.unwrap()["commits"].clone();
    assert_eq!(commits.as_array().unwrap().len(), 1);
    assert_ne!(commits[0].as_str().unwrap().split(' ').next().unwrap(), &head[..7]);
    assert!(out.message.contains("(restore: git branch feat/s "), "{}", out.message);
}

#[test]
fn data_that_differs_from_its_pin_blocks_unless_forced() {
    let r = common::rig();
    let s = origin_scope(&r);
    assert!(awm_plugin_scopes::lifecycle::dvc_drift(&s).is_empty());
    std::fs::create_dir_all(s.join(".dvc")).unwrap();
    std::fs::write(s.join(".dvc/config"), "[core]\n").unwrap();
    std::fs::write(s.join(".dvc/.gitignore"), "/cache\n/tmp\n").unwrap();
    std::fs::create_dir_all(s.join("data")).unwrap();
    std::fs::write(s.join("data/raw"), "pinned").unwrap();
    awm_dvcdata::testkit::add(&s, "data/raw").unwrap();
    git(&s, &["add", "-A"]);
    git(&s, &["commit", "-q", "-m", "data"]);
    git(&r.projects.join(P).join(".bare"), &["push", "-q", "origin", "feat/s"]);
    assert!(awm_plugin_scopes::lifecycle::dvc_drift(&s).is_empty());
    awm_dvcdata::testkit::unprotect(&s.join("data/raw")).unwrap();
    std::fs::write(s.join("data/raw"), "changed").unwrap();
    assert_eq!(awm_plugin_scopes::lifecycle::dvc_drift(&s), ["data/raw (modified)"]);
    let e = r.op.delete_scope(P, "s", false, false).unwrap_err();
    assert!(e.message.contains("data differs from its pin"), "{e:?}");
    assert!(s.exists());
    let out = r.op.delete_scope(P, "s", true, false).unwrap();
    assert_eq!(out.discarded.unwrap()["dvc_drift"], json!(["data/raw (modified)"]));
}

/// The scope with one commit to merge, and the project's main checkout.
fn mergeable(r: &Rig) -> PathBuf {
    let s = origin_scope(r);
    let bare = r.projects.join(P).join(".bare");
    git(&bare, &["worktree", "add", "-q", &r.projects.join(P).join("main").to_string_lossy(), "main"]);
    commit(&s, "work");
    s
}

fn complete_state(r: &Rig) -> (Option<String>, String, String, bool) {
    let bare = r.projects.join(P).join(".bare");
    (common::agent_id(&r.op, P, "s"), git(&bare, &["rev-parse", "main"]), git(&bare, &["ls-remote", "origin", "main"]), r.projects.join(P).join("s").exists())
}

#[test]
fn a_refused_complete_changes_nothing() {
    let r = common::rig();
    let s = mergeable(&r);
    let before = complete_state(&r);
    assert!(before.0.is_some());
    std::fs::write(s.join("draft.txt"), "x").unwrap();
    assert!(r.op.complete_scope(P, "s", true, true, false, false).unwrap_err().message.contains("uncommitted"));
    assert_eq!(complete_state(&r), before);
    std::fs::remove_file(s.join("draft.txt")).unwrap();
    assert!(r.op.complete_scope(P, "s", false, true, false, false).unwrap_err().message.contains("no other branch or remote holds"));
    assert_eq!(complete_state(&r), before);
    let e = r.op.complete_scope(P, "s", false, false, false, true).unwrap_err();
    assert!(e.class == "ValueError" && e.message.contains("cleanup"));
    assert_eq!(complete_state(&r), before);
}

#[test]
fn a_complete_that_merges_its_commits_cleans_up() {
    let r = common::rig();
    let s = mergeable(&r);
    let head = git(&s, &["rev-parse", "HEAD"]);
    let out = r.op.complete_scope(P, "s", true, true, false, false).unwrap();
    assert_eq!(out.status, "completed");
    assert_eq!(out.message, "Scope completed, merged into main, worktree removed, branch feat/s kept");
    assert!(!s.exists());
    assert!(git_try(&r.projects.join(P).join(".bare"), &["merge-base", "--is-ancestor", &head, "main"]).0);
    assert_eq!(common::agent_id(&r.op, P, "s"), None);
}

#[test]
fn delete_branch_names_the_sha_and_refuses_a_branch_held_elsewhere() {
    let r = common::rig();
    let s = origin_scope(&r);
    let bare = r.projects.join(P).join(".bare");
    let head = commit(&s, "work");
    let out = r.op.delete_scope(P, "s", true, true).unwrap();
    assert_eq!(sha(&bare, "refs/heads/feat/s"), None);
    assert!(out.message.contains(&format!("git branch feat/s {head}")));

    create_with(&r, "d", |_| {}).unwrap();
    r.op.delete_scope(P, "d", true, false).unwrap();
    let other = r.tmp.path().join("other");
    git(&bare, &["worktree", "add", "-q", &other.to_string_lossy(), "feat/d"]);
    let said = r.op.settle_branch(&bare, "feat/d", true).unwrap();
    assert!(said.starts_with("branch feat/d kept: branch 'feat/d' is checked out at"), "{said}");
    assert!(sha(&bare, "refs/heads/feat/d").is_some());
}

// ---- sync, gather, scatter ---------------------------------------------------------------

/// A hub on the flat branch `dev` and peripherals p1, p2 on `feat/*`, all with rows.
fn hub(r: &Rig) -> (PathBuf, PathBuf, PathBuf) {
    let bare = common::bare_project(r, "fan");
    let proj = bare.parent().unwrap().to_path_buf();
    git(&bare, &["worktree", "add", "-q", "-b", "dev", &proj.join("dev").to_string_lossy(), "main"]);
    r.call("ensure", json!({"project": "fan", "scope": "dev", "branch": "dev", "worktree": proj.join("dev").to_string_lossy(), "status": "active", "new_project": true}), None).unwrap();
    for p in ["p1", "p2"] {
        git(&bare, &["worktree", "add", "-q", "-b", &format!("feat/{p}"), &proj.join(p).to_string_lossy(), "main"]);
        r.call("ensure", json!({"project": "fan", "scope": p, "worktree": proj.join(p).to_string_lossy(), "status": "active"}), None).unwrap();
    }
    (proj.join("dev"), proj.join("p1"), proj.join("p2"))
}

fn by_scope(v: &Value) -> std::collections::BTreeMap<String, Value> {
    v["results"].as_array().unwrap().iter().map(|x| (x["scope"].as_str().unwrap().to_string(), x.clone())).collect()
}

#[test]
fn gather_fans_in_and_reports_up_to_date() {
    let r = common::rig();
    let (dev, p1, p2) = hub(&r);
    commit(&p1, "p1.txt");
    commit(&p2, "p2.txt");
    let out = r.call("git_gather", json!({"project": "fan", "hub": "dev", "peripherals": ["p1", "p2"]}), None).unwrap();
    let keys: Vec<&String> = out.as_object().unwrap().keys().collect();
    assert_eq!(keys, ["project", "hub", "hub_branch", "direction", "results", "data_results", "data_summary", "summary"]);
    assert_eq!((out["direction"].clone(), out["hub_branch"].clone(), out["summary"].clone()), (json!("gather"), json!("dev"), json!({"merged": 2})));
    let by = by_scope(&out);
    assert_eq!((by["p1"]["result"].clone(), by["p1"]["branch"].clone()), (json!("merged"), json!("feat/p1")));
    assert!(dev.join("p1.txt").exists() && dev.join("p2.txt").exists());
    let again = r.op.gather("fan", "dev", &["p1".into()], "merge").unwrap();
    assert_eq!(by_scope(&again)["p1"]["result"], json!("up_to_date"));
    assert_eq!(by_scope(&r.op.gather("fan", "dev", &["nope".into()], "merge").unwrap())["nope"]["result"], json!("skipped"));
}

#[test]
fn a_gather_conflict_is_aborted_and_the_batch_goes_on() {
    let r = common::rig();
    let (dev, p1, p2) = hub(&r);
    std::fs::write(dev.join("README"), "hub edit").unwrap();
    git(&dev, &["commit", "-qam", "hub edits"]);
    std::fs::write(p1.join("README"), "p1 edit").unwrap();
    git(&p1, &["commit", "-qam", "p1 edits"]);
    commit(&p2, "p2.txt");
    let by = by_scope(&r.op.gather("fan", "dev", &["p1".into(), "p2".into()], "merge").unwrap());
    assert_eq!((by["p1"]["result"].clone(), by["p2"]["result"].clone()), (json!("conflict"), json!("merged")));
    assert_eq!(git(&dev, &["status", "--porcelain"]), "");
    assert!(!git_try(&dev, &["rev-parse", "--verify", "-q", "MERGE_HEAD"]).0);
}

#[test]
fn gather_refuses_a_dirty_hub_and_a_rebase() {
    let r = common::rig();
    let (dev, _, _) = hub(&r);
    let e = r.op.gather("fan", "dev", &["p1".into()], "rebase").unwrap_err();
    assert!(e.class == "ValueError" && e.message.contains("merge"));
    std::fs::write(dev.join("dirty.txt"), "x").unwrap();
    assert!(r.op.gather("fan", "dev", &["p1".into()], "merge").unwrap_err().message.contains("uncommitted changes"));
}

#[test]
fn sync_merges_the_base_and_refuses_a_dirty_worktree_or_bad_strategy() {
    let r = common::rig();
    let (dev, p1, _) = hub(&r);
    git(&r.projects.join("fan/.bare"), &["worktree", "add", "-q", &r.projects.join("fan/main").to_string_lossy(), "main"]);
    commit(&r.projects.join("fan/main"), "base.txt");
    let out = r.call("sync", json!({"project": "fan", "scope": "p1"}), None).unwrap();
    assert_eq!(out["message"], json!("Synced feat/p1 with main via merge"));
    assert!(p1.join("base.txt").exists());
    std::fs::write(dev.join("x"), "x").unwrap();
    assert!(r.op.sync_scope("fan", "dev", "merge", None).unwrap_err().message.starts_with("Worktree has uncommitted changes"));
    let e = r.call("sync", json!({"project": "fan", "scope": "p1", "strategy": "squash"}), None).unwrap_err();
    assert_eq!(e.error_class, "ValidationError");
}

fn init(wt: &Path, track: &[&str]) {
    std::fs::create_dir_all(wt).unwrap();
    git(wt, &["init", "-q"]);
    for f in track {
        std::fs::write(wt.join(f), "# tracked\n").unwrap();
        git(wt, &["add", f]);
    }
    if !track.is_empty() {
        git(wt, &["commit", "-q", "-m", "init"]);
    }
}

#[test]
fn heal_worktree_drops_untracked_chrome_and_keeps_tracked_files() {
    let r = common::rig();
    let wt = r.tmp.path().join("wt");
    init(&wt, &[]);
    std::fs::write(wt.join("AGENTS.md"), "# local\n").unwrap();
    assert!(r.op.is_tracked(&wt, "AGENTS.md").map(|t| !t).unwrap());
    let out = r.op.heal_worktree(&wt, "proj-a", "s1", false).unwrap();
    assert_eq!(out["agents_md"], json!("deleted:untracked"));
    assert!(!wt.join("AGENTS.md").exists());

    let wt = r.tmp.path().join("wt2");
    init(&wt, &["AGENTS.md"]);
    std::os::unix::fs::symlink("AGENTS.md", wt.join("CLAUDE.md")).unwrap();
    let dry = r.op.heal_worktree(&wt, "proj-a", "s1", true).unwrap();
    assert_eq!(dry["claude_md"], json!("deleted:symlink"));
    assert!(std::fs::symlink_metadata(wt.join("CLAUDE.md")).is_ok());
    r.op.heal_worktree(&wt, "proj-a", "s1", false).unwrap();
    assert!(std::fs::symlink_metadata(wt.join("CLAUDE.md")).is_err());
    assert_eq!(r.op.heal_worktree(&wt, "proj-a", "s1", false).unwrap(), json!({"agents_md": null, "claude_md": null, "data": null}));
    assert!(!wt.join(".awm").exists());

    let wt = r.tmp.path().join("wt3");
    init(&wt, &["CLAUDE.md"]);
    assert_eq!(r.op.heal_worktree(&wt, "proj-a", "s1", false).unwrap()["claude_md"], Value::Null);
    assert!(wt.join("CLAUDE.md").exists());
}

// ---- projects ----------------------------------------------------------------------------

#[test]
fn new_project_clones_and_registers_its_main_checkout() {
    let r = common::rig();
    let seed = common::seed(&r.tmp.path().join("seed"));
    let url = seed.to_string_lossy().into_owned();
    let res = r.call("create", json!({"project": "fresh", "scope": "work", "new_project": true, "clone_url": url}), None).unwrap();
    assert_eq!(res["scope"], json!("work"));
    let proj = r.projects.join("fresh");
    assert!(proj.join(".bare/HEAD").exists() && proj.join("main/.git").exists() && proj.join("work/.git").exists());
    assert!(!proj.join("work/.awm").exists());
    assert_eq!(git(&proj.join("main"), &["status", "--porcelain"]), "");
    let found = common::search(&r.op, None, Some("fresh"), None, None);
    let mut scopes: Vec<String> = common::pairs(&found).into_iter().map(|p| p.1).collect();
    scopes.sort();
    assert_eq!(scopes, ["main", "work"], "a scaffolded main checkout is listed once");
    assert_eq!(r.call("create", json!({"project": "fresh", "scope": "two", "new_project": true}), None).unwrap()["scope"], json!("two"));
    assert!(r.op.project_listing().unwrap().iter().any(|p| p.name == "fresh"));
}

// ---- search ------------------------------------------------------------------------------

#[test]
fn search_filters_by_status_and_project() {
    let r = common::rig();
    assert_eq!(common::search(&r.op, Some("all"), None, None, None)["total"], json!(0));
    common::seeded(&r);
    let n = |status: Option<&str>, project: Option<&str>| common::search(&r.op, status, project, None, None)["total"].as_i64().unwrap();
    assert_eq!(n(Some("all"), None), 3);
    assert_eq!(n(Some("active"), None), 1);
    assert_eq!(n(Some("all"), Some("proj-a")), 2);
    let done = common::search(&r.op, Some("completed"), Some("proj-b"), None, None);
    assert_eq!(common::pairs(&done), [("proj-b".into(), "scope-3".into())]);
    assert_eq!(done["scopes"][0]["status"], json!("completed"));
    let active = r.call("search", json!({}), None).unwrap();
    assert_eq!((active["total"].clone(), active["scopes"][0]["status"].clone()), (json!(1), json!("active")));
    assert!(active["scopes"][0]["repo_path"].as_str().is_some_and(|p| !p.is_empty()));
    assert_eq!(r.call("search", json!({"status": "deleted"}), None).unwrap(), json!({"scopes": [], "total": 0}));
    let keys: Vec<String> = active["scopes"][0].as_object().unwrap().keys().cloned().collect();
    assert_eq!(keys, ["project", "scope", "status", "branch", "worktree", "repo_path", "session", "private", "tags"]);
}
