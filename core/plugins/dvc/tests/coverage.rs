//! The inventory of what neither remote holds.

mod common;

use std::collections::BTreeSet;
use std::path::PathBuf;

use awm_plugin_dvc::coverage;
use common::git;
use serde_json::Value;

/// `projects/proj/dev`, a committed repo on `feat/dev`, and an empty shared cache.
fn scoped() -> (common::Ws, PathBuf) {
    let w = common::workspace();
    let scope = w.projects().join("proj/dev");
    std::fs::create_dir_all(&scope).unwrap();
    git(&scope, &["init", "-q", "-b", "feat/dev"]);
    std::fs::write(scope.join("README"), "hello\n").unwrap();
    git(&scope, &["add", "README"]);
    git(&scope, &["commit", "-qm", "first"]);
    std::fs::create_dir_all(w.paths.shared_cache()).unwrap();
    (w, scope)
}

fn push_upstream(w: &common::Ws, scope: &std::path::Path) {
    let bare = w.root.join("bare.git");
    git(scope, &["init", "-q", "--bare", bare.to_str().unwrap()]);
    git(scope, &["remote", "add", "origin", bare.to_str().unwrap()]);
    git(scope, &["push", "-q", "-u", "origin", "feat/dev"]);
}

fn first_row(w: &common::Ws) -> Value {
    coverage::report(&w.paths, true, false)["worktrees"][0].clone()
}

#[test]
fn scopes_are_found_at_project_scope_depth_and_no_deeper() {
    let (w, scope) = scoped();
    let vendored = scope.join("vendor/someone-elses-repo");
    std::fs::create_dir_all(&vendored).unwrap();
    git(&vendored, &["init", "-q"]);

    assert_eq!(coverage::find_worktrees(w.ws()), vec![scope]);
}

#[test]
fn nested_scopes_are_found() {
    let w = common::workspace();
    let proj = w.projects().join("metasmith");
    std::fs::create_dir_all(&proj).unwrap();
    let seed = w.root.join("seed");
    std::fs::create_dir_all(&seed).unwrap();
    git(&seed, &["init", "-q", "-b", "main"]);
    std::fs::write(seed.join("README"), "x\n").unwrap();
    git(&seed, &["add", "README"]);
    git(&seed, &["commit", "-qm", "init"]);
    let bare = proj.join(".bare");
    common::git_in(&w.root, &["clone", "--bare", "-q", seed.to_str().unwrap(), bare.to_str().unwrap()]);
    let flat = proj.join("monorepo");
    let nested = proj.join("fabfos/dev");
    git(&bare, &["worktree", "add", "-q", "-b", "feat/monorepo", flat.to_str().unwrap(), "main"]);
    git(&bare, &["worktree", "add", "-q", "-b", "fabfos/dev", nested.to_str().unwrap(), "main"]);

    let found: BTreeSet<PathBuf> = coverage::find_worktrees(w.ws()).iter().map(|p| std::fs::canonicalize(p).unwrap()).collect();
    let want: BTreeSet<PathBuf> = [flat, nested].iter().map(|p| std::fs::canonicalize(p).unwrap()).collect();
    assert_eq!(found, want);
    assert!(!found.contains(&std::fs::canonicalize(&bare).unwrap()));
}

#[test]
fn a_committed_pushed_scope_with_no_pins_is_not_at_risk() {
    let (w, scope) = scoped();
    push_upstream(&w, &scope);

    let out = coverage::report(&w.paths, true, false);

    assert_eq!(out["scopes"], 1);
    assert_eq!(out["scopes_at_risk"], 0);
    assert_eq!(out["worktrees"], serde_json::json!([]));
    assert_eq!(out["truncated"], true);
    assert_eq!(out["cache_exists"], true);
}

#[test]
fn untracked_and_modified_files_put_a_scope_at_risk() {
    let (w, scope) = scoped();
    std::fs::write(scope.join("README"), "edited\n").unwrap();
    std::fs::write(scope.join("scratch.out"), "nowhere else\n").unwrap();

    let row = first_row(&w);

    assert_eq!(row["uncommitted"]["modified"], 1);
    assert_eq!(row["uncommitted"]["untracked"], 1);
    assert!(row["uncommitted"]["untracked_sample"].as_array().unwrap().iter().any(|s| s == "scratch.out"));
    assert_eq!(row["at_risk"], true);
}

#[test]
fn a_branch_with_no_upstream_is_as_lost_as_one_never_pushed() {
    let (w, _scope) = scoped();

    let row = first_row(&w);

    assert_eq!(row["unpushed"]["branch"], "feat/dev");
    assert_eq!(row["unpushed"]["no_upstream"], true);
    assert_eq!(row["at_risk"], true);
}

#[test]
fn commits_ahead_of_the_upstream_are_counted() {
    let (w, scope) = scoped();
    push_upstream(&w, &scope);
    std::fs::write(scope.join("README"), "more\n").unwrap();
    git(&scope, &["commit", "-qam", "second"]);

    let row = first_row(&w);

    assert_eq!(row["unpushed"]["ahead"], 1);
    assert_eq!(row["unpushed"]["behind"], 0);
    assert_eq!(row["at_risk"], true);
}

#[test]
fn a_pin_the_shared_cache_cannot_satisfy_is_reported_missing() {
    let (w, scope) = scoped();
    std::fs::create_dir_all(scope.join(".dvc")).unwrap();
    common::touch(&scope.join("data/chunk.dvc"), &format!("outs:\n- md5: {}\n  path: chunk\n", "a".repeat(32)));

    let row = first_row(&w);

    assert_eq!(row["pins"]["dvc"], true);
    assert_eq!(row["pins"]["pins"], 1);
    assert_eq!(row["pins"]["missing"], 1);
}

#[test]
fn a_missing_dir_manifest_is_reported_separately_from_missing_leaves() {
    let (w, scope) = scoped();
    std::fs::create_dir_all(scope.join(".dvc")).unwrap();
    common::touch(&scope.join("data/tree.dvc"), &format!("outs:\n- md5: {}.dir\n  path: tree\n", "b".repeat(32)));

    let row = first_row(&w);

    assert_eq!(row["pins"]["unresolved_manifests"], 1);
    assert_eq!(row["at_risk"], true);
}

#[test]
fn all_includes_the_scopes_with_nothing_at_risk() {
    let (w, _scope) = scoped();

    let out = coverage::report(&w.paths, false, false);

    assert_eq!(out["worktrees"].as_array().unwrap().len(), 1);
    assert_eq!(out["scopes"], 1);
    assert_eq!(out["truncated"], false);
}
