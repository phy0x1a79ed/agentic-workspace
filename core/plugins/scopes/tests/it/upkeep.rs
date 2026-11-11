//! The hygiene pass (scopes.md R45) on real git: what it refuses to touch.

use crate::common;

use std::path::PathBuf;

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
    r.op.claim(P, "busy", &session, Some(std::process::id() as i64), false, false).unwrap();
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
fn a_peer_may_not_sweep() {
    let r = rig();
    let e = r.call("awm_sweep", json!({"dry_run": true}), Some("peer:altair")).unwrap_err();
    assert_eq!((e.error_class.as_str(), e.message.as_str()), ("PermissionError", "scope_awm_sweep runs on its own node"));
    assert_eq!(r.call("awm_sweep", json!({"dry_run": true}), None).unwrap()["dry_run"], json!(true));
}
