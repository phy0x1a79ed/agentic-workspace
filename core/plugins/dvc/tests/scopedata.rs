//! The scope-data verbs resolve a scope from the workspace layout and git alone.

mod common;

use std::path::{Path, PathBuf};

use awm_plugin_dvc::scopedata::{self, GcArgs};
use awm_plugin_dvc::service::{verbs, VerbSpec};
use common::git;

fn verb(name: &str) -> VerbSpec {
    verbs().into_iter().find(|v| v.name == name).unwrap()
}

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
fn the_three_verbs_are_declared_on_every_surface() {
    for name in ["scope_status", "mount", "gc"] {
        assert!(!verb(name).cli_http_only, "{name}");
    }
}

#[test]
fn gc_still_requires_the_caller_to_name_its_projects() {
    let gc = verb("gc");
    let req: Vec<bool> = gc.params.as_array().unwrap().iter().filter(|p| p["name"] == "projects").map(|p| p["required"].as_bool().unwrap()).collect();
    assert_eq!(req, [true]);
    assert!(gc.description.contains("DRY RUN BY DEFAULT"));
}

#[test]
fn the_plugin_manifest_names_the_tool_and_turns_it_on() {
    let text = std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("awm-plugin.toml")).unwrap();
    let doc: toml::Table = text.parse().unwrap();
    let tools: Vec<(String, String)> = doc["tool"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| (t["name"].as_str().unwrap().to_string(), t["tier"].as_str().unwrap().to_string()))
        .collect();
    assert_eq!(tools, [("dvc".to_string(), "on".to_string())]);
}

#[test]
fn a_project_name_that_could_escape_is_refused() {
    let w = common::workspace();
    for bad in ["", "../x", ".hidden", "a\\b", "a/b", "a\0"] {
        let e = scopedata::scope_status(&w.paths, bad, "s").unwrap_err();
        assert_eq!(e.error_class, "ValueError", "{bad:?}");
    }
}

#[test]
fn a_scope_name_that_could_escape_is_refused() {
    let w = common::workspace();
    for bad in ["", "../x", "a/../b", "a//b", "/abs"] {
        let e = scopedata::scope_status(&w.paths, "p", bad).unwrap_err();
        assert_eq!(e.error_class, "ValueError", "{bad:?}");
    }
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
fn an_unknown_project_raises_rather_than_returning_nothing() {
    let w = common::workspace();
    assert_eq!(scopedata::project_worktrees(&w.paths, "nope").unwrap_err().error_class, "FileNotFoundError");
}

#[test]
fn scope_status_of_a_plain_nested_scope_reports_no_dvc() {
    let (w, _p) = project();
    let rep = scopedata::scope_status(&w.paths, "p", "fab/dev").unwrap();
    assert_eq!(rep["mode"], "none", "{rep}");
}

#[test]
fn gc_with_nothing_listed_is_refused_not_run() {
    let (w, _p) = project();
    assert_eq!(scopedata::gc(&w.paths, gc_args(&[])).unwrap()["result"], "refused");
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
}

#[test]
fn mount_does_not_write_into_an_unregistered_directory() {
    let (w, p) = project();
    std::fs::create_dir(p.join("stray")).unwrap();
    let e = scopedata::mount(&w.paths, Path::new("/bin/true"), "p", "stray", Some(vec!["data/x".into()])).unwrap_err();
    assert_eq!(e.error_class, "ValueError");
    assert!(e.message.contains("stray"), "{e}");
    assert!(!p.join("stray/.awm").exists());
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

#[test]
fn parse_worktrees_drops_the_bare_entry_and_keeps_branches() {
    let text = "worktree /ws/projects/p/.bare\nbare\n\nworktree /ws/projects/p/a\nHEAD 0123\nbranch refs/heads/feat/a\n\nworktree /ws/projects/p/d\nHEAD 4567\ndetached\n";
    let got = scopedata::parse_worktrees(text);
    assert_eq!(got, vec![(PathBuf::from("/ws/projects/p/a"), Some("feat/a".to_string())), (PathBuf::from("/ws/projects/p/d"), None)]);
}
