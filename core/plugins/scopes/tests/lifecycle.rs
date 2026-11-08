//! The scope lifecycle on real git: create, existing branches, rollback, nesting, delete
//! and complete guards, repair, heal and its audit, gather and scatter, and projects.

mod common;

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

#[test]
fn create_refuses_a_missing_project_and_an_active_scope() {
    let r = common::rig();
    let e = common::create(&r.op, "nope", "s1").unwrap_err();
    assert_eq!(e.class, "FileNotFoundError");
    assert!(e.message.contains("not found") && e.message.contains("new_project=true"));
    assert!(!r.projects.join("nope").exists());
    common::bare_project(&r, P);
    create_with(&r, "s", |_| {}).unwrap();
    let e = create_with(&r, "s", |_| {}).unwrap_err();
    assert_eq!((e.class.as_str(), e.message.as_str()), ("FileExistsError", "Scope 's' already has an active session in project 'proj'"));
}

#[test]
fn create_counts_sessions_and_names_the_reserved_store() {
    let r = common::rig();
    common::bare_project(&r, P);
    create_with(&r, "s", |_| {}).unwrap();
    r.op.complete_scope(P, "s", false, true, true, false).unwrap();
    let again = create_with(&r, "s", |_| {}).unwrap();
    assert_eq!(again.session, Some(2));
    assert!(again.message.contains("existing branch feat/s"));
    let e = create_with(&r, "data/x", |_| {}).unwrap_err();
    assert!(e.class == "ValueError" && e.message.contains("reserved"));
}

#[test]
fn the_context_goes_to_journal_brief_set_and_a_refusal_is_a_warning() {
    let r = common::rig();
    common::bare_project(&r, P);
    create_with(&r, "b", |q| q.context = Some("  For the brief.  ".into())).unwrap();
    assert_eq!(r.out.calls_to("brief_set"), [json!({"project": P, "scope": "b", "text": "For the brief."})]);
    r.out.refuse.lock().unwrap().insert("brief_set".into());
    let out = create_with(&r, "w", |q| q.context = Some("For w.".into())).unwrap();
    assert_eq!(out.status, "active");
    assert!(out.message.ends_with("; the brief was not written (journal.brief_set: down); set it with journal brief_set"), "{}", out.message);
    create_with(&r, "quiet", |q| q.context = Some("   ".into())).unwrap();
    assert_eq!(r.out.calls_to("brief_set").len(), 2, "a blank context writes no brief");
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

#[test]
fn several_remotes_are_refused_by_name_until_from_branch_picks_one() {
    let r = common::rig();
    let (bare, seed) = repos(&r);
    publish(&r, &seed, "feat/amb", &["origin", "capella"]);
    let e = create_with(&r, "amb", |_| {}).unwrap_err();
    assert!(e.class == "ValueError" && e.message.contains("origin/feat/amb") && e.message.contains("capella/feat/amb"), "{e:?}");
    assert_rolled_back(&r, "amb");
    assert_eq!(sha(&bare, "refs/heads/feat/amb"), None);
    create_with(&r, "amb", |q| q.from_branch = Some("capella/feat/amb".into())).unwrap();
    assert_eq!(upstream(&r.projects.join(P).join("amb"), "feat/amb"), "capella/feat/amb");

    publish(&r, &seed, "feat/svc", &["origin", "capella"]);
    git(&bare, &["branch", "--no-track", "feat/svc", "origin/feat/svc"]);
    assert!(create_with(&r, "svc", |_| {}).unwrap_err().message.contains("several remotes"));
    create_with(&r, "svc", |q| q.from_branch = Some("capella/feat/svc".into())).unwrap();
    assert_eq!(upstream(&r.projects.join(P).join("svc"), "feat/svc"), "capella/feat/svc");
}

#[test]
fn from_branch_naming_another_base_and_a_branch_held_elsewhere_are_refused() {
    let r = common::rig();
    let (bare, _) = repos(&r);
    let x = unpushed(&r, "feat/x");
    let e = create_with(&r, "x", |q| q.from_branch = Some("main".into())).unwrap_err();
    assert!(e.class == "ValueError" && e.message.contains("already exists"));
    assert_eq!(sha(&bare, "refs/heads/feat/x"), Some(x));
    let elsewhere = r.projects.join(P).join("elsewhere");
    git(&bare, &["worktree", "add", "-q", "-b", "feat/held", &elsewhere.to_string_lossy(), "main"]);
    let e = create_with(&r, "held", |_| {}).unwrap_err();
    assert!(e.class == "FileExistsError" && e.message.contains("elsewhere"));
    assert!(elsewhere.join(".git").exists());
    assert_rolled_back(&r, "held");
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
fn a_cancel_during_provisioning_rolls_back() {
    let r = common::rig_with(failing("provision", "Cancelled"));
    repos(&r);
    let bare = r.projects.join(P).join(".bare");
    let x = unpushed(&r, "feat/x");
    assert_eq!(create_with(&r, "x", |_| {}).unwrap_err().class, "Cancelled");
    assert_rolled_back(&r, "x");
    assert_eq!(sha(&bare, "refs/heads/feat/x"), Some(x));
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

#[test]
fn stale_directory_cleanup_keeps_the_branch() {
    let r = common::rig();
    repos(&r);
    let x = unpushed(&r, "feat/x");
    let stale = r.projects.join(P).join("x");
    std::fs::create_dir_all(&stale).unwrap();
    std::fs::write(stale.join("leftover.txt"), "x").unwrap();
    create_with(&r, "x", |_| {}).unwrap();
    assert!(!stale.join("leftover.txt").exists());
    assert_eq!(git(&stale, &["rev-parse", "HEAD"]), x);
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
fn the_branch_check_names_both_sides_of_a_ref_clash() {
    let r = common::rig();
    let proj = nested(&r);
    let bare = proj.join(".bare");
    r.op.assert_branch_available(&bare, "libraries/mono").unwrap();
    r.op.assert_branch_available(&bare, "fabfos/dev").unwrap();
    git(&bare, &["branch", "feat/fabfos", "main"]);
    assert!(r.op.assert_branch_available(&bare, "feat/fabfos/dev").unwrap_err().message.contains("feat/fabfos"));
    assert!(r.op.assert_branch_available(&bare, "fabfos").unwrap_err().message.contains("fabfos/dev"));
    assert_eq!(r.op.assert_branch_available(&bare, "bad..name").unwrap_err().message, "'bad..name' is not a valid git branch name");
}

#[test]
fn a_container_directory_is_not_a_scope() {
    let r = common::rig();
    let proj = nested(&r);
    let bare = proj.join(".bare");
    let found = r.op.live_worktrees_under(&bare, &proj.join("fabfos")).unwrap();
    assert_eq!(found, [proj.join("fabfos/dev")]);
    assert!(r.op.live_worktrees_under(&bare, &proj.join("monorepo")).unwrap().is_empty());
    let e = r.op.cleanup_worktree(&bare, &proj.join("fabfos"), false).unwrap_err();
    assert!(e.class == "RuntimeError" && e.message.contains("not a scope"));
    assert!(proj.join("fabfos/dev/.git").exists());
    let req = CreateReq { project: "metasmith".into(), scope: Some("fabfos".into()), branch_name: Some("fabfos".into()), from_branch: Some("main".into()), ..Default::default() };
    assert!(r.op.create_scope(&req).is_err());
    assert!(proj.join("fabfos/dev/.git").exists(), "the refused request destroyed a live nested scope");
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

#[test]
fn a_failed_nested_create_leaves_its_group_sibling() {
    let r = common::rig_with(failing("registered", "RuntimeError"));
    let proj = nested(&r);
    let req = CreateReq { project: "metasmith".into(), scope: Some("engine/tmp".into()), from_branch: Some("main".into()), ..Default::default() };
    assert!(r.op.create_scope(&req).is_err());
    assert!(!proj.join("engine/tmp").exists());
    assert_eq!(git(&proj.join(".bare"), &["branch", "--list", "feat/engine/tmp"]), "");
    assert!(proj.join("engine/dev/.git").exists(), "rollback must not touch a sibling in the same group");
}

#[test]
fn admin_dirs_are_resolved_not_guessed_from_basenames() {
    let r = common::rig();
    let bare = common::bare_project(&r, P);
    let (a, b, flat, nest) = {
        let p = r.projects.join(P);
        (p.join("a"), p.join("b"), p.join("dev"), p.join("fabfos/dev"))
    };
    for (br, d) in [("feat/a", &a), ("feat/b", &b), ("feat/dev", &flat), ("fabfos/dev", &nest)] {
        git(&bare, &["worktree", "add", "-q", "-b", br, &d.to_string_lossy(), "main"]);
    }
    r.op.cleanup_worktree(&bare, &a, false).unwrap();
    assert!(!a.exists() && !bare.join("worktrees/a").exists());
    assert!(git_try(&b, &["status"]).0, "the sibling still works");
    let fa = r.op.worktree_admin_dir(&bare, &flat).unwrap().unwrap();
    let na = r.op.worktree_admin_dir(&bare, &nest).unwrap().unwrap();
    assert_ne!(fa, na);
    r.op.cleanup_worktree(&bare, &nest, false).unwrap();
    assert!(fa.exists() && git_try(&flat, &["status"]).0);
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
    r.op.claim(P, "s", "cc".repeat(17).as_str(), Some(std::process::id() as i64), Some("abcd1234"), false, false).unwrap();
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
fn complete_without_cleanup_retires_the_row_and_keeps_the_worktree() {
    let r = common::rig();
    let s = origin_scope(&r);
    let out = r.op.complete_scope(P, "s", false, false, false, false).unwrap();
    assert_eq!(out.to_json(), json!({"project": P, "scope": "s", "status": "completed", "message": "Scope completed", "session": null, "discarded": null}));
    assert!(s.exists() && common::agent_id(&r.op, P, "s").is_none());
    let found = common::search(&r.op, Some("completed"), None, None, None);
    assert_eq!(common::pairs(&found), [(P.to_string(), "s".to_string())]);
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

#[test]
fn delete_refuses_a_missing_scope_or_bare() {
    let r = common::rig();
    std::fs::create_dir_all(r.projects.join("nope/.bare")).unwrap();
    let e = r.op.delete_scope("nope", "nope", false, false).unwrap_err();
    assert_eq!((e.class.as_str(), e.message.as_str()), ("FileNotFoundError", "No scope 'nope' found in project 'nope'"));
    let e = r.op.complete_scope("gone", "x", false, false, false, false).unwrap_err();
    assert!(e.class == "FileNotFoundError" && e.message.starts_with("Bare repository not found at"));
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
fn scatter_fans_out_and_skips_a_dirty_peripheral() {
    let r = common::rig();
    let (dev, p1, p2) = hub(&r);
    commit(&dev, "hub.txt");
    std::fs::write(p1.join("dirty.txt"), "x").unwrap();
    let out = r.op.scatter("fan", "dev", &["p1".into(), "p2".into()], "merge").unwrap();
    let by = by_scope(&out);
    assert_eq!((by["p1"]["result"].clone(), by["p1"]["detail"].clone()), (json!("skipped"), json!("worktree dirty — skipped")));
    assert_eq!(by["p2"]["result"], json!("merged"));
    assert!(p2.join("hub.txt").exists());
    assert_eq!(out["summary"], json!({"skipped": 1, "merged": 1}));
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

// ---- repair, heal, audit -----------------------------------------------------------------

#[test]
fn repair_backfills_a_missing_row_once() {
    let r = common::rig();
    let bare = common::bare_project(&r, "ghost");
    let wt = r.projects.join("ghost/dev");
    git(&bare, &["worktree", "add", "-q", "-b", "feat/dev", &wt.to_string_lossy(), "main"]);
    assert_eq!(common::search(&r.op, Some("all"), Some("ghost"), None, None)["total"], json!(1), "the main checkout is not here, only dev");
    let out = r.op.repair_scope("ghost", "dev").unwrap();
    assert_eq!(out.status, "repaired");
    assert!(out.message.contains("dev"));
    let found = common::search(&r.op, Some("all"), Some("ghost"), None, None);
    assert_eq!(found["scopes"][0]["branch"], json!("feat/dev"));
    assert_eq!(r.op.repair_scope("ghost", "dev").unwrap().status, "skipped");

    std::fs::create_dir_all(r.projects.join("p1/.bare")).unwrap();
    assert!(r.op.repair_scope("p1", "nope").unwrap_err().message.contains("Worktree not found"));
    assert!(r.op.repair_scope("never-existed", "dev").unwrap_err().message.contains("bare repo"));
    std::fs::remove_file(wt.join(".git")).unwrap();
    r.op.write(|c| {
        c.execute("DELETE FROM agents", [])?;
        Ok(())
    })
    .unwrap();
    assert!(r.op.repair_scope("ghost", "dev").unwrap_err().message.contains("not a git worktree"));
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

#[test]
fn heal_scopes_runs_over_active_rows_and_never_on_the_cwd() {
    let r = common::rig();
    let wts = common::seeded(&r);
    let report = r.op.heal_scopes(None, false).unwrap();
    assert_eq!(report.len(), 1);
    assert_eq!((report[0]["scope"].clone(), report[0]["ok"].clone()), (json!("scope-1"), json!(true)));

    let set = |v: &str| {
        r.op.write(|c| {
            c.execute("UPDATE agents SET worktree=? WHERE scope='scope-1'", [v])?;
            Ok(())
        })
        .unwrap()
    };
    set("/tmp/awm-test-does-not-exist-xyz");
    let report = r.op.heal_scopes(None, false).unwrap();
    assert_eq!(report[0]["ok"], json!(false));
    assert_eq!(report[0]["error"], json!("worktree missing: /tmp/awm-test-does-not-exist-xyz"));

    set("");
    let report = r.op.heal_scopes(None, false).unwrap();
    assert_eq!(report[0]["worktree"], json!(r.projects.join("proj-a/scope-1").to_string_lossy()));

    let rel = r.projects.join("proj-a/rel-scope");
    std::fs::create_dir_all(&rel).unwrap();
    set("projects/proj-a/rel-scope");
    assert_eq!(r.op.heal_scopes(None, false).unwrap()[0]["ok"], json!(true));

    set(&wts[0].to_string_lossy());
    init(&wts[0], &[]);
    std::os::unix::fs::symlink("AGENTS.md", wts[0].join("CLAUDE.md")).unwrap();
    let dry = r.op.heal_scopes(None, true).unwrap();
    assert_eq!(dry[0]["dry_run"], json!(true));
    assert!(wts[0].join("AGENTS.md").exists());
    let report = r.op.heal_scopes(None, false).unwrap();
    assert_eq!(report[0]["actions"], json!({"agents_md": "deleted:untracked", "claude_md": "deleted:symlink", "data": null}));
    assert_eq!(r.op.heal_scopes(Some("proj-b"), false).unwrap(), Vec::<Value>::new());
}

#[test]
fn the_audit_lists_hand_made_worktrees_and_orphaned_rows() {
    let r = common::rig();
    let bare = common::bare_project(&r, P);
    let proj = r.projects.join(P);
    let by_hand = |path: &Path, branch: &str| git(&bare, &["worktree", "add", "-q", "-b", branch, &path.to_string_lossy(), "main"]);
    create_with(&r, "known", |_| {}).unwrap();
    by_hand(&proj.join("shards"), "feat/shards");
    by_hand(&proj.join("engine/a"), "feat/engine-a");
    by_hand(&proj.join(".claude/worktrees/w"), "wt-w");
    by_hand(&r.tmp.path().join("outside"), "feat/outside");
    by_hand(&proj.join("pruned"), "feat/pruned");
    std::fs::remove_dir_all(proj.join("pruned")).unwrap();
    git(&bare, &["worktree", "add", "-q", &proj.join("main").to_string_lossy(), "main"]);
    create_with(&r, "gone", |_| {}).unwrap();
    std::fs::remove_dir_all(proj.join("gone")).unwrap();
    create_with(&r, "done", |_| {}).unwrap();
    r.op.complete_scope(P, "done", false, false, false, false).unwrap();
    create_with(&r, "old", |_| {}).unwrap();
    r.op.delete_scope(P, "old", false, false).unwrap();
    std::fs::create_dir_all(r.projects.join("broken/.bare")).unwrap();

    let audit = r.op.audit_worktrees(None).unwrap();
    let mut found: Vec<(String, String)> = audit["unregistered"].as_array().unwrap().iter().map(|u| (u["scope"].as_str().unwrap().into(), u["branch"].as_str().unwrap().into())).collect();
    found.sort();
    assert_eq!(found, [("engine/a".into(), "feat/engine-a".into()), ("shards".into(), "feat/shards".into())]);
    let orphans: Vec<(&str, &str)> = audit["orphaned_rows"].as_array().unwrap().iter().map(|o| (o["scope"].as_str().unwrap(), o["status"].as_str().unwrap())).collect();
    assert_eq!(orphans, [("gone", "allocated")]);
    let errors: Vec<&str> = audit["errors"].as_array().unwrap().iter().map(|e| e["project"].as_str().unwrap()).collect();
    assert_eq!(errors, ["broken"]);

    let before = common::search(&r.op, Some("all"), None, None, None)["scopes"].clone();
    for dry in [true, false] {
        let out = r.call("heal", json!({"project": P, "dry_run": dry}), None).unwrap();
        assert_eq!(out["orphaned_rows"].as_array().unwrap().len(), 1);
        assert_eq!(out["audit_errors"], json!([]));
    }
    assert_eq!(common::search(&r.op, Some("all"), None, None, None)["scopes"], before, "heal writes no rows");
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

#[test]
fn new_project_alone_takes_tags_and_a_scope_is_otherwise_required() {
    let r = common::rig();
    let seed = common::seed(&r.tmp.path().join("seed"));
    let url = seed.to_string_lossy().into_owned();
    let e = r.call("create", json!({"project": "solo"}), None).unwrap_err();
    assert!(e.error_class == "ValueError" && e.message.contains("new_project=true"));
    let res = r.call("create", json!({"project": "solo", "new_project": true, "clone_url": url, "tags": ["tony"]}), None).unwrap();
    assert_eq!(res, json!({"project": "solo", "scope": "main", "status": "active", "message": "Created project at projects/solo (main checkout main)", "session": 1, "discarded": null}));
    let mut entries: Vec<String> = std::fs::read_dir(r.projects.join("solo")).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).filter(|n| n != ".bare").collect();
    entries.sort();
    assert_eq!(entries, ["main"]);
    r.call("create", json!({"project": "solo", "scope": "lab/ana", "tags": ["lab", "ana"]}), None).unwrap();
    let table = r.op.load_tags(Some("solo")).unwrap();
    assert_eq!(table[&("solo".to_string(), String::new())], ["tony"]);
    assert_eq!(table[&("solo".to_string(), "lab/ana".to_string())], ["ana", "lab"]);
    let e = r.call("create", json!({"project": "solo", "new_project": true}), None).unwrap_err();
    assert!(e.error_class == "FileExistsError" && e.message.contains("already exists"));
}

#[test]
fn a_fresh_project_gets_its_seed_commit_on_main() {
    let r = common::rig();
    let made = r.op.create_project("freshproj", None, None).unwrap();
    assert_eq!(made.mode, "fresh");
    assert_eq!(git(&r.projects.join("freshproj/.bare"), &["log", "--format=%s", "main"]), "Initial commit for freshproj");
    assert!(!r.projects.join("freshproj/main/.awm").exists());
    assert!(r.ws.join("data/freshproj/raw").is_dir() && r.ws.join("data/freshproj/staged").is_dir());
}

#[test]
fn a_failed_clone_names_the_cause() {
    let r = common::rig();
    let e = r.op.create_project("x", Some("/no/such/repo"), None).unwrap_err();
    assert_eq!(e.class, "RuntimeError");
    assert!(e.message.starts_with("git clone of '/no/such/repo' failed (exit 128): "), "{}", e.message);
    assert!(!e.message.contains("SSH_AUTH_SOCK"));
}

#[test]
fn ensure_refuses_an_unknown_project_unless_asked() {
    let r = common::rig();
    let e = r.call("ensure", json!({"project": "ghost", "scope": "s"}), None).unwrap_err();
    assert_eq!((e.error_class.as_str(), e.message.as_str()), ("KeyError", "\"project 'ghost' not found — pass new_project=true to create it\""));
    assert_eq!(r.call("ensure", json!({"project": "ghost", "scope": "s", "new_project": true}), None).unwrap(), json!({"project": "ghost", "scope": "s"}));
    assert!(common::agent_id(&r.op, "ghost", "s").is_some());
    assert_eq!(r.call("project_ensure", json!({"project": "old"}), None).unwrap(), json!({"project": "old"}));
    assert_eq!(r.call("ensure", json!({"project": "old", "scope": "s"}), None).unwrap(), json!({"project": "old", "scope": "s"}));
    let rec = r.op.agent_record("old", "s", true).unwrap().unwrap();
    assert_eq!((rec.branch.as_str(), rec.status.as_str()), ("feat/s", "allocated"));
}

#[test]
fn ref_resolve_classifies_every_literal_form() {
    let r = common::rig();
    common::seeded(&r);
    r.op.write(|c| {
        c.execute("INSERT INTO users (id, username) VALUES ('u1', 'tony')", [])?;
        Ok(())
    })
    .unwrap();
    let rr = |l: &str| r.call("ref_resolve", json!({"literal": l}), None).unwrap();
    assert_eq!(rr(""), json!({"kind": "system"}));
    assert_eq!(rr("system"), json!({"kind": "system"}));
    assert_eq!(rr("scope:proj-a/scope-2"), json!({"kind": "agent", "project": "proj-a", "scope": "scope-2"}));
    assert_eq!(rr("agent:proj-a"), Value::Null);
    assert_eq!(rr("user:tony"), json!({"kind": "user", "username": "tony"}));
    assert_eq!(rr("tony"), json!({"kind": "user", "username": "tony"}));
    assert_eq!(rr("nobody"), Value::Null);
    assert_eq!(rr("proj-a/none"), Value::Null);
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

#[test]
fn the_project_listing_counts_scopes_and_main_checkouts() {
    let r = common::rig();
    common::seeded(&r);
    let bare = common::bare_project(&r, "hand");
    git(&bare, &["worktree", "add", "-q", "../main", "main"]);
    let found = r.call("search", json!({"status": "all"}), None).unwrap();
    let counts: Vec<(String, Value)> = found["projects"].as_array().unwrap().iter().map(|p| (p["name"].as_str().unwrap().into(), p["scope_counts"].clone())).collect();
    assert_eq!(counts, [
        ("hand".into(), json!({"active": 1, "completed": 0, "deleted": 0})),
        ("proj-a".into(), json!({"active": 1, "completed": 1, "deleted": 0})),
        ("proj-b".into(), json!({"active": 0, "completed": 1, "deleted": 0})),
    ]);
    let limited = r.call("search", json!({"status": "all", "limit": 1}), None).unwrap();
    assert_eq!(limited["scopes"].as_array().unwrap().len(), 1);
    assert_eq!(limited["projects"].as_array().unwrap().len(), 3, "a scope limit never shortens the project list");
    let hand = r.call("search", json!({"project": "hand"}), None).unwrap();
    assert!(hand.get("projects").is_none());
    assert_eq!(hand["scopes"][0]["worktree"], json!(r.projects.join("hand/main").to_string_lossy()));
    assert_eq!((hand["scopes"][0]["scope"].clone(), hand["scopes"][0]["branch"].clone()), (json!("main"), json!("main")));
    assert_eq!(r.call("search", json!({"project": "hand", "status": "completed"}), None).unwrap()["scopes"], json!([]));
    assert_eq!(r.call("search", json!({"project": "hand", "query": "zzz"}), None).unwrap()["scopes"], json!([]));
    assert_eq!(common::pairs(&r.call("search", json!({"project": "hand", "query": "mai"}), None).unwrap()), [("hand".into(), "main".into())]);
    let q = r.call("search", json!({"query": "proj-b", "status": "all"}), None).unwrap();
    assert_eq!(q["projects"][0]["name"], json!("proj-b"));
}

#[test]
fn search_ranks_scopes_by_the_journals_brief_hits() {
    let r = common::rig();
    common::seeded(&r);
    r.out.answers.lock().unwrap().insert(
        "journal_query".into(),
        json!({"exact": [
            {"id": "a", "project": "proj-a", "scope": "scope-1", "title": "brief"},
            {"id": "b", "project": "proj-a", "scope": "scope-1", "title": "an older brief"},
            {"id": "c", "project": "proj-b", "scope": "scope-3", "title": "brief"},
            {"id": "d", "project": "proj-c", "scope": "gone", "title": "brief"},
        ], "veiled": true}),
    );
    let found = r.call("search", json!({"query": "bathymetry", "status": "all"}), None).unwrap();
    assert_eq!(common::pairs(&found), [("proj-a".into(), "scope-1".into()), ("proj-b".into(), "scope-3".into())]);
    assert!(found.get("degraded").is_none());
    let names: Vec<&str> = found["projects"].as_array().unwrap().iter().map(|p| p["name"].as_str().unwrap()).collect();
    assert_eq!(names, ["proj-a", "proj-b"]);
    let ask = &r.out.calls_to("journal_query")[0];
    assert_eq!((ask["query"].clone(), ask["tag"].clone()), (json!("bathymetry"), json!("brief")));
    assert_eq!(r.call("search", json!({"query": "bathymetry", "status": "completed"}), None).unwrap()["scopes"].as_array().unwrap().len(), 1);
    let named = r.call("search", json!({"query": "scope-2", "status": "all"}), None).unwrap();
    assert_eq!(common::pairs(&named)[0], ("proj-a".into(), "scope-2".into()), "a name match comes first");
}

#[test]
fn a_silent_journal_degrades_search_to_names() {
    let r = common::rig();
    common::seeded(&r);
    r.out.refuse.lock().unwrap().insert("journal_query".into());
    let found = r.call("search", json!({"query": "scope-1", "status": "all"}), None).unwrap();
    assert_eq!(common::pairs(&found), [("proj-a".into(), "scope-1".into())]);
    assert_eq!(found["degraded"]["semantic"], json!("error"));
    assert_eq!(found["degraded"]["fallback"], json!("keyword"));
}
