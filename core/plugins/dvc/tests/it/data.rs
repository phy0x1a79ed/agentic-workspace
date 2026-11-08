//! The dvc verbs over the native data layer: a project that keeps no remote is refused by
//! push and pull and left out of the shared-cache gc, and gc finds a wired project.

use crate::common;

use std::path::{Path, PathBuf};

use awm_dvcdata::provision::{self, Layout};
use awm_dvcdata::wiring;
use awm_plugin_dvc::scopedata::{self, GcArgs};
use awm_plugin_dvc::transfer;
use common::git;

const TAG: &str = "Signature: 8a477f597d28d172789f06886806bc55\n# no remote\n";
const HOOK_BIN: &str = "/bin/true";

struct Project {
    w: common::Ws,
    bare: PathBuf,
    wt: PathBuf,
    layout: Layout,
}

/// A bare repo with one worktree `main` that carries a tracked `.dvc/config`.
fn dvc_project() -> Project {
    let w = common::workspace();
    let bare = common::bare_project(&w, "p");
    let wt = w.projects().join("p/main");
    git(&bare, &["worktree", "add", "-q", wt.to_str().unwrap(), "main"]);
    std::fs::create_dir_all(wt.join(".dvc")).unwrap();
    std::fs::write(wt.join(".dvc/config"), "").unwrap();
    std::fs::write(wt.join(".dvc/.gitignore"), "/config.local\n/tmp\n/cache\n").unwrap();
    git(&wt, &["add", "-A"]);
    git(&wt, &["commit", "-q", "-m", "dvc init"]);
    let mut layout = Layout::new(w.ws());
    layout.tmpfs = w.root.join("no-shm");
    Project { w, bare, wt, layout }
}

fn tag(p: &Project) {
    std::fs::write(p.bare.parent().unwrap().join("CACHEDIR.TAG"), TAG).unwrap();
}

fn gc(p: &Project, projects: &[&str]) -> serde_json::Value {
    let args = GcArgs { projects: projects.iter().map(|s| s.to_string()).collect(), dry_run: true, keep: "all-commits".into(), exclude: vec![], accept_missing: vec![] };
    scopedata::gc(&p.w.paths, args).unwrap()
}

#[test]
fn push_and_pull_refuse_a_tagged_project() {
    let p = dvc_project();
    tag(&p);
    wiring::ensure_cache_config(&p.wt, &p.layout).unwrap();
    p.w.write_config();
    let api = common::submitting_api();
    let scope = p.wt.to_string_lossy().into_owned();

    for (verb, out) in [("push", transfer::push(&p.w.paths, api.as_ref(), &scope, true)), ("pull", transfer::pull(&p.w.paths, api.as_ref(), &scope, true))] {
        let e = out.unwrap_err();
        assert_eq!(e.error_class, "DvcScopeError", "{verb}");
        assert!(e.message.contains("keeps no remote"), "{verb}: {e}");
    }
    assert!(api.calls().is_empty());
}

#[test]
fn a_tagged_project_is_not_wired_to_the_shared_cache() {
    let p = dvc_project();
    let r = wiring::ensure_repo_wiring(&p.wt, Path::new(HOOK_BIN));
    assert_eq!(r["result"], "ok", "{r}");
    assert!(scopedata::dvc_wired_projects(&p.w.paths).contains("p"));
    tag(&p);
    assert!(!scopedata::dvc_wired_projects(&p.w.paths).contains("p"));
}

#[test]
fn data_gc_finds_the_wired_project() {
    let p = dvc_project();
    provision::provision_scope_data(&p.layout, "p", "main", &p.wt, Path::new(HOOK_BIN));
    let wt2 = p.bare.parent().unwrap().join("other");
    git(&p.bare, &["worktree", "add", "-q", "-b", "other", wt2.to_str().unwrap(), "main"]);
    wiring::ensure_cache_config(&wt2, &p.layout).unwrap();

    assert_eq!(gc(&p, &[])["result"], "refused");
    let rep = gc(&p, &["p"]);
    assert_eq!(rep["result"], "ok", "{rep}");
    assert_eq!(rep["projects"]["p"]["worktrees"], 2, "{rep}");
}
