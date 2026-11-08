//! The scope-data verbs resolve a scope from the workspace layout and git alone.

use crate::common;

use std::path::{Path, PathBuf};

use awm_plugin_dvc::scopedata::{self, GcArgs};
use common::git;

/// A bare repo `p` with a flat worktree `a` and a nested one `fab/dev`.
fn project() -> (common::Ws, PathBuf) {
    let w = common::workspace();
    let bare = common::bare_project(&w, "p");
    let p = w.projects().join("p");
    git(&bare, &["worktree", "add", "-q", "-b", "a", p.join("a").to_str().unwrap(), "main"]);
    git(&bare, &["worktree", "add", "-q", "-b", "n", p.join("fab/dev").to_str().unwrap(), "main"]);
    (w, p)
}

fn gc_args(projects: &[&str]) -> GcArgs {
    GcArgs { projects: projects.iter().map(|s| s.to_string()).collect(), dry_run: true, keep: "all-commits".into(), exclude: vec![], accept_missing: vec![] }
}

#[test]
fn a_project_name_that_could_escape_is_refused() {
    let w = common::workspace();
    for bad in ["", "../x", ".hidden", "a\\b", "a/b", "a\0"] {
        let e = scopedata::scope_status(&w.paths, bad, "s").unwrap_err();
        assert_eq!(e.error_class, "ValueError", "{bad:?}");
    }
    for bad in ["", "../x", "a/../b", "a//b", "/abs"] {
        let e = scopedata::scope_status(&w.paths, "p", bad).unwrap_err();
        assert_eq!(e.error_class, "ValueError", "{bad:?}");
    }
    assert_eq!(scopedata::project_worktrees(&w.paths, "nope").unwrap_err().error_class, "FileNotFoundError");
    assert_eq!(scopedata::gc(&w.paths, gc_args(&[])).unwrap()["result"], "refused");
}

#[test]
fn worktrees_are_enumerated_at_any_depth() {
    let (w, p) = project();
    let found: Vec<PathBuf> = scopedata::project_worktrees(&w.paths, "p").unwrap().iter().map(|x| std::fs::canonicalize(x).unwrap()).collect();
    assert!(found.contains(&std::fs::canonicalize(p.join("a")).unwrap()));
    assert!(found.contains(&std::fs::canonicalize(p.join("fab/dev")).unwrap()));
    assert!(!found.contains(&std::fs::canonicalize(p.join(".bare")).unwrap()));
}

#[test]
fn a_scope_with_no_worktree_directory_raises_naming_the_path() {
    let (w, _p) = project();
    let e = scopedata::scope_status(&w.paths, "p", "ghost").unwrap_err();
    assert_eq!(e.error_class, "FileNotFoundError");
    assert!(e.message.contains("projects/p/ghost"), "{e}");
    let e = scopedata::mount(&w.paths, Path::new("/bin/true"), "p", "ghost", Some(vec!["data/x".into()])).unwrap_err();
    assert_eq!(e.error_class, "FileNotFoundError");
    assert!(e.message.contains("projects/p/ghost"), "{e}");
}

#[test]
fn a_directory_that_is_not_a_registered_worktree_raises() {
    let (w, p) = project();
    std::fs::create_dir(p.join("stray")).unwrap();
    let e = scopedata::scope_status(&w.paths, "p", "stray").unwrap_err();
    assert_eq!(e.error_class, "ValueError");
    assert!(e.message.contains("stray"), "{e}");
    assert!(!p.join("stray/.awm").exists());
    let e = scopedata::mount(&w.paths, Path::new("/bin/true"), "p", "stray", Some(vec!["data/x".into()])).unwrap_err();
    assert_eq!(e.error_class, "ValueError");
    assert!(!p.join("stray/.awm").exists(), "mount does not write into an unregistered directory");
}

#[test]
fn a_detached_worktree_is_not_taken_for_a_scope() {
    let (w, p) = project();
    git(&p.join("a"), &["checkout", "-q", "--detach"]);
    let e = scopedata::scope_status(&w.paths, "p", "a").unwrap_err();
    assert_eq!(e.error_class, "ValueError");
    assert!(e.message.contains("detached"), "{e}");
}

#[test]
fn gc_refuses_the_whole_collection_when_a_worktree_is_gone() {
    let (w, p) = project();
    std::fs::remove_dir_all(p.join("fab/dev")).unwrap();
    let rep = scopedata::gc(&w.paths, gc_args(&["p"])).unwrap();
    assert_eq!(rep["result"], "refused");
    assert!(rep["detail"].as_str().unwrap().contains(&p.join("fab/dev").to_string_lossy().into_owned()), "{rep}");
    assert!(rep.get("deleted_anything").is_none());
}

