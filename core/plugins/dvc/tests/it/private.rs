//! Private marks: a peer caller sees no worktree, and reaches no scope, a mark covers.
//!
//! The marks file sits at `$AWM_DIR/private.json` and relative scope paths resolve against
//! `$AWM_WORKSPACE`. Both are process-wide, so every test here holds one lock while it points
//! them at its own temp workspace.

use crate::common;

use std::path::PathBuf;
use std::sync::{Arc, Mutex, MutexGuard};

use awm_plugin_dvc::service::Dvc;
use awm_plugin_sdk::privacy;
use common::git;
use serde_json::{json, Value};

pub static ENV: Mutex<()> = Mutex::new(());

struct Marked {
    _lock: MutexGuard<'static, ()>,
    w: common::Ws,
    dvc: Arc<Dvc>,
    avatar: PathBuf,
}

/// Two scopes, `proj/dev` and `avatar/dev`, with a mark on the avatar project.
fn marked() -> Marked {
    let lock = ENV.lock().unwrap_or_else(|p| p.into_inner());
    let w = common::workspace();
    std::env::set_var("AWM_DIR", &w.paths.awm_dir);
    std::env::set_var("AWM_WORKSPACE", w.ws());
    for project in ["proj", "avatar"] {
        let scope = w.projects().join(project).join("dev");
        std::fs::create_dir_all(&scope).unwrap();
        git(&scope, &["init", "-q", "-b", "feat/dev"]);
        std::fs::write(scope.join("README"), "x\n").unwrap();
    }
    std::fs::create_dir_all(w.paths.shared_cache()).unwrap();
    privacy::write_marks(&[json!({"project": "avatar", "scope": null, "path": w.projects().join("avatar").to_string_lossy()})]).unwrap();
    let dvc = Arc::new(Dvc {
        paths: w.paths.clone(),
        dao: w.dao(),
        api: common::FakeApi::new(|m, p, _| panic!("a private scope reached Globus: {m} {p}")),
        watcher: None,
        hub: None,
        plugin_dir: w.root.join("plugin"),
        hook_bin: "/bin/true".into(),
    });
    let avatar = w.projects().join("avatar/dev");
    Marked { _lock: lock, w, dvc, avatar }
}

async fn listed(m: &Marked, peer: bool) -> (Vec<String>, Value) {
    let out = m.dvc.handle("coverage", json!({"all": true}), peer).await.unwrap();
    let names = out["worktrees"].as_array().unwrap().iter().map(|r| r["scope"].as_str().unwrap().to_string()).collect();
    (names, out)
}

#[tokio::test]
async fn a_peer_coverage_lists_no_private_worktree_and_a_broken_marks_file_hides_all() {
    let m = marked();
    let (names, out) = listed(&m, true).await;
    assert_eq!(names, ["projects/proj/dev"]);
    assert_eq!((out["scopes"].as_u64(), out["scopes_at_risk"].as_u64()), (Some(1), Some(1)));
    assert_eq!(listed(&m, false).await.0, ["projects/avatar/dev", "projects/proj/dev"], "the owner sees every worktree");
    std::fs::write(privacy::marks_path(), "{broken").unwrap();
    assert!(listed(&m, true).await.0.is_empty(), "a broken marks file shows a peer nothing");
}


#[tokio::test]
async fn a_peer_meets_a_private_scope_path_as_no_scope() {
    let m = marked();
    for verb in ["status", "resolve", "pull", "push"] {
        for scope in [m.avatar.to_string_lossy().into_owned(), "projects/avatar/dev".to_string()] {
            let e = m.dvc.handle(verb, json!({"scope": scope}), true).await.unwrap_err();
            assert_eq!(e.error_class, "DvcScopeError", "{verb} {scope}");
            assert!(e.message.contains("not a DVC scope"), "{verb} {scope}: {e}");
        }
    }
}

#[tokio::test]
async fn a_peer_meets_a_private_scope_name_as_no_worktree() {
    let m = marked();
    for verb in ["scope_status", "mount"] {
        let e = m.dvc.handle(verb, json!({"project": "avatar", "scope": "dev"}), true).await.unwrap_err();
        assert_eq!(e.error_class, "FileNotFoundError", "{verb}");
        assert!(e.message.contains("No worktree directory"), "{verb}: {e}");
    }
}

#[tokio::test]
async fn a_peer_gc_never_names_or_reaches_a_private_project() {
    let m = marked();
    for args in [json!({"projects": ["proj", "avatar"]}), json!({"projects": ["proj"], "exclude": ["avatar"]})] {
        let e = m.dvc.handle("gc", args.clone(), true).await.unwrap_err();
        assert_eq!(e.error_class, "FileNotFoundError", "{args}");
        assert!(e.message.contains("no bare repo"), "{args}: {e}");
    }
    // Wire both projects: a bare repo carrying awm's merge driver.
    for project in ["proj", "avatar"] {
        let bare = m.w.projects().join(project).join(".bare");
        common::git_in(&m.w.root, &["init", "-q", "--bare", bare.to_str().unwrap()]);
        git(&bare, &["config", "merge.dvc.driver", "/bin/true merge-driver"]);
    }
    let out = m.dvc.handle("gc", json!({"projects": ["proj"]}), true).await.unwrap();
    assert_eq!(out["result"], "refused");
    assert!(!out.to_string().contains("avatar"), "{out}");
}

