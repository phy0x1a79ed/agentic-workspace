//! `scope private` and `scope tag`, and how search, resolve and heal report them to local
//! and peer callers.

use crate::common;

use awm_plugin_sdk::privacy;
use serde_json::{json, Value};

fn private(r: &common::Rig, args: Value) -> Result<Value, awm_plugin_sdk::VerbError> {
    r.call("private", args, None)
}

fn marks_file(r: &common::Rig) -> Value {
    serde_json::from_str(&std::fs::read_to_string(r.awm_dir.join("marks/private.json")).unwrap()).unwrap()
}

fn search(r: &common::Rig, args: Value, as_: Option<&str>) -> Value {
    let mut a = json!({"status": "all"});
    a.as_object_mut().unwrap().extend(args.as_object().unwrap().clone());
    r.call("search", a, as_).unwrap()
}

fn tag(r: &common::Rig, args: Value, as_: Option<&str>) -> Result<Value, awm_plugin_sdk::VerbError> {
    r.call("tag", args, as_)
}

#[test]
fn mark_and_unmark_a_project() {
    let r = common::rig();
    common::seeded(&r);
    let out = private(&r, json!({"project": "proj-a", "private": true})).unwrap();
    let path = r.projects.join("proj-a").to_string_lossy().into_owned();
    assert_eq!(out, json!({"project": "proj-a", "scope": null, "private": true, "marks": [{"project": "proj-a", "scope": null, "path": path}]}));
    assert_eq!(marks_file(&r), json!({"version": 1, "marks": out["marks"]}));
    assert!(privacy::hidden("proj-a", Some("scope-1")));
    assert_eq!(std::fs::read_link(r.awm_dir.join("private.json")).unwrap(), std::path::Path::new("marks/private.json"));

    let out = private(&r, json!({"project": "proj-a", "private": false})).unwrap();
    assert_eq!((out["marks"].clone(), out["private"].clone()), (json!([]), json!(false)));
    assert_eq!(marks_file(&r)["marks"], json!([]));
}

#[test]
fn a_broken_marks_file_is_not_overwritten() {
    let r = common::rig();
    common::seeded(&r);
    let path = r.awm_dir.join("private.json");
    std::fs::write(&path, "{broken").unwrap();
    let e = private(&r, json!({"project": "proj-a", "private": true})).unwrap_err();
    assert_eq!(e.error_class, "MarksUnreadable");
    assert_eq!(std::fs::read_to_string(&path).unwrap(), "{broken");
}

#[test]
fn a_mark_whose_slug_covers_another_is_refused() {
    let r = common::rig();
    let wts = common::seeded(&r);
    std::fs::create_dir_all(r.projects.join("proj/.bare")).unwrap();
    let e = private(&r, json!({"project": "proj", "private": true})).unwrap_err();
    assert!(e.error_class == "ValueError" && e.message.contains("proj-a"), "{e:?}");
    std::fs::create_dir_all(r.projects.join("proj_a/.bare")).unwrap();
    assert!(private(&r, json!({"project": "proj_a", "private": true})).unwrap_err().message.contains("share its slug prefix"));
    assert!(!r.awm_dir.join("marks/private.json").exists());

    let sibling = format!("{}-b", wts[0].display());
    r.call("ensure", json!({"project": "proj-a", "scope": "scope-1-b", "worktree": sibling, "status": "active"}), None).unwrap();
    assert!(private(&r, json!({"project": "proj-a", "scope": "scope-1", "private": true})).unwrap_err().message.contains("scope-1-b"));
}

fn peer_marks(r: &common::Rig) {
    common::seeded(r);
    std::fs::create_dir_all(r.projects.join("loose/.bare")).unwrap();
    private(r, json!({"project": "proj-a", "scope": "scope-1", "private": true})).unwrap();
    private(r, json!({"project": "proj-b", "private": true})).unwrap();
    private(r, json!({"project": "loose", "private": true})).unwrap();
}

#[test]
fn a_peer_search_lists_no_private_scope_or_project() {
    let r = common::rig();
    peer_marks(&r);
    for as_ in ["peer", "peer:altair"] {
        let out = search(&r, json!({}), Some(as_));
        assert_eq!(common::pairs(&out), [("proj-a".to_string(), "scope-2".to_string())]);
        let names: Vec<&str> = out["projects"].as_array().unwrap().iter().map(|p| p["name"].as_str().unwrap()).collect();
        assert_eq!(names, ["proj-a"]);
        assert_eq!(search(&r, json!({"project": "proj-b"}), Some(as_))["scopes"], json!([]));
        assert_eq!(common::pairs(&search(&r, json!({"query": "scope"}), Some(as_))), [("proj-a".to_string(), "scope-2".to_string())]);
        assert!(!search(&r, json!({"query": "proj-b"}), Some(as_)).to_string().contains("proj-b"));
    }
    for as_ in [None, Some("user:tony")] {
        let out = search(&r, json!({}), as_);
        assert_eq!(out["scopes"].as_array().unwrap().len(), 3);
        assert_eq!(out["projects"].as_array().unwrap().len(), 3);
    }
}

#[test]
fn a_broken_marks_file_shows_a_peer_no_scope() {
    let r = common::rig();
    peer_marks(&r);
    std::fs::write(r.awm_dir.join("marks/private.json"), "{broken").unwrap();
    let out = search(&r, json!({}), Some("peer"));
    assert_eq!((out["scopes"].clone(), out["projects"].clone()), (json!([]), json!([])));
    assert_eq!(search(&r, json!({}), None)["scopes"].as_array().unwrap().len(), 3);
}

#[test]
fn a_peer_resolves_a_private_scope_as_missing() {
    let r = common::rig();
    peer_marks(&r);
    let resolve = |p: &str, s: &str, as_| r.call("resolve", json!({"project": p, "scope": s}), as_).unwrap();
    assert_eq!(resolve("proj-b", "scope-3", Some("peer")), json!({"exists": false, "project": "proj-b", "scope": "scope-3"}));
    assert_eq!(resolve("proj-b", "scope-3", None)["exists"], json!(true));
    assert_eq!(resolve("proj-a", "scope-2", Some("peer"))["exists"], json!(true));
    assert_eq!(r.call("ref_resolve", json!({"literal": "agent:proj-a/scope-1"}), Some("peer:altair")).unwrap(), Value::Null);
    assert_eq!(r.call("ref_resolve", json!({"literal": "proj-a/scope-1"}), None).unwrap(), json!({"kind": "agent", "project": "proj-a", "scope": "scope-1"}));
}

#[test]
fn a_peer_heal_reports_no_private_scope() {
    let r = common::rig();
    peer_marks(&r);
    let peer = r.call("heal", json!({"dry_run": true}), Some("peer")).unwrap();
    let local = r.call("heal", json!({"dry_run": true}), None).unwrap();
    let scopes: Vec<&str> = local["scopes"].as_array().unwrap().iter().map(|s| s["scope"].as_str().unwrap()).collect();
    assert_eq!(scopes, ["scope-1"]);
    assert_eq!((peer["scopes"].clone(), peer["count"].clone()), (json!([]), json!(0)));
    for key in ["unregistered_worktrees", "orphaned_rows", "audit_errors"] {
        assert!(peer[key].as_array().unwrap().iter().all(|row| !["proj-b", "loose"].contains(&row["project"].as_str().unwrap())));
    }
    let local_errors: Vec<&str> = local["audit_errors"].as_array().unwrap().iter().map(|e| e["project"].as_str().unwrap()).collect();
    assert!(local_errors.contains(&"proj-b") && local_errors.contains(&"loose"));
    assert_eq!(r.call("heal", json!({"project": "proj-b", "dry_run": true}), Some("peer")).unwrap()["audit_errors"], json!([]));
}

#[test]
fn a_peer_cannot_set_or_clear_a_mark() {
    let r = common::rig();
    common::seeded(&r);
    private(&r, json!({"project": "proj-a", "scope": "scope-1", "private": true})).unwrap();
    let before = privacy::marks().unwrap();
    for as_ in ["peer", "peer:altair"] {
        for args in [
            json!({"project": "proj-a", "private": true}),
            json!({"project": "proj-a", "private": false}),
            json!({"project": "no-such-project", "private": true}),
            json!({"project": "proj-a", "private": "maybe"}),
        ] {
            let e = r.call("private", args, Some(as_)).unwrap_err();
            assert_eq!((e.error_class.as_str(), e.message.as_str()), ("RuntimeError", "unknown function 'scope_private'"));
        }
    }
    assert_eq!(privacy::marks().unwrap(), before);
    for as_ in [None, Some("user:tony"), Some("agent:x")] {
        assert_eq!(r.call("private", json!({"project": "proj-a", "private": true}), as_).unwrap()["private"], json!(true));
    }
}

#[test]
fn project_tags_reach_its_scopes_and_a_nearer_level_replaces_them() {
    let r = common::rig();
    common::seeded(&r);
    let out = tag(&r, json!({"project": "proj-a", "tags": ["tony"]}), None).unwrap();
    assert_eq!(out, json!({"project": "proj-a", "scope": null, "tags": ["tony"], "effective": ["tony"], "effective_from": "proj-a"}));
    let read = tag(&r, json!({"project": "proj-a", "scope": "scope-1"}), None).unwrap();
    assert_eq!((read["tags"].clone(), read["effective"].clone(), read["effective_from"].clone()), (json!([]), json!(["tony"]), json!("proj-a")));

    tag(&r, json!({"project": "proj-a", "tags": "tony"}), None).unwrap();
    tag(&r, json!({"project": "proj-a", "scope": "lab", "tags": ["lab"]}), None).unwrap();
    tag(&r, json!({"project": "proj-a", "scope": "lab/ana", "tags": ["lab", "ana"]}), None).unwrap();
    let eff = |p: &str, s: &str| r.op.effective_tags(p, Some(s)).unwrap();
    assert_eq!(eff("proj-a", "lab/bo"), ["lab"]);
    assert_eq!(eff("proj-a", "lab/ana/deep"), ["ana", "lab"]);
    assert_eq!(eff("proj-a", "scope-1"), ["tony"]);
    assert!(eff("proj-b", "scope-3").is_empty());
    let lab = tag(&r, json!({"project": "proj-a", "scope": "lab/ana/deep"}), None).unwrap();
    assert_eq!(lab["effective_from"], json!("proj-a/lab/ana"));
}

#[test]
fn tag_refuses_an_unknown_project_and_a_peer_write() {
    let r = common::rig();
    common::seeded(&r);
    assert_eq!(tag(&r, json!({"project": "nope", "tags": ["lab"]}), None).unwrap_err().error_class, "FileNotFoundError");
    tag(&r, json!({"project": "proj-a", "tags": ["tony"]}), None).unwrap();
    assert_eq!(tag(&r, json!({"project": "proj-a"}), Some("peer:capella")).unwrap()["tags"], json!(["tony"]));
    let e = tag(&r, json!({"project": "proj-a", "tags": ["lab"]}), Some("peer:capella")).unwrap_err();
    assert_eq!((e.error_class.as_str(), e.message.as_str()), ("ValueError", "scope tag: a peer may read tags, not set them"));
    private(&r, json!({"project": "proj-a", "private": true})).unwrap();
    assert_eq!(tag(&r, json!({"project": "proj-a"}), Some("peer")).unwrap_err().error_class, "FileNotFoundError");
}

#[test]
fn search_carries_tags_and_filters_by_them() {
    let r = common::rig();
    common::seeded(&r);
    tag(&r, json!({"project": "proj-a", "tags": ["tony"]}), None).unwrap();
    tag(&r, json!({"project": "proj-a", "scope": "scope-2", "tags": ["lab", "ana"]}), None).unwrap();
    let out = search(&r, json!({}), None);
    let by: Vec<(String, Value)> = out["scopes"].as_array().unwrap().iter().map(|s| (s["scope"].as_str().unwrap().into(), s["tags"].clone())).collect();
    assert_eq!(by, [("scope-1".into(), json!(["tony"])), ("scope-2".into(), json!(["ana", "lab"])), ("scope-3".into(), json!([]))]);
    let ptags: Vec<Value> = out["projects"].as_array().unwrap().iter().map(|p| p["tags"].clone()).collect();
    assert_eq!(ptags, [json!(["tony"]), json!([])]);
    assert_eq!(out["tags"], json!({"ana": 1, "lab": 1, "tony": 1}));

    assert_eq!(common::pairs(&search(&r, json!({"tag": "ana"}), None)), [("proj-a".into(), "scope-2".into())]);
    assert_eq!(common::pairs(&search(&r, json!({"tag": "Tony"}), None)), [("proj-a".into(), "scope-1".into())]);
    assert_eq!(search(&r, json!({"tag": "lab"}), None)["tags"], json!({"ana": 1, "lab": 1, "tony": 1}));
    assert_eq!(search(&r, json!({"tag": "collab"}), None)["scopes"], json!([]));
    let one = search(&r, json!({"project": "proj-a"}), None);
    assert!(one.get("tags").is_none() && one.get("projects").is_none());
    assert_eq!(one["scopes"][0]["tags"], json!(["tony"]));
    assert_eq!(r.call("resolve", json!({"project": "proj-a", "scope": "scope-1"}), None).unwrap()["tags"], json!(["tony"]));
}
