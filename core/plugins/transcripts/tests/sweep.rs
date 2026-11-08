//! The sweep, restore and prune on a real fake tree, ported from `test_sweep.py`.

mod common;

use std::collections::HashSet;
use std::os::unix::fs::PermissionsExt;

use awm_plugin_transcripts::sweep;
use common::*;
use serde_json::json;

fn sweep7(r: &Rig) -> serde_json::Value {
    sweep::archive(&r.app.tree, 7.0, false, &HashSet::new(), None)
}

/// The defect this plugin exists to fix.
#[test]
fn sidecar_moves_with_its_transcript() {
    let r = rig();
    make_session(&r.projects(), "proj", "sess", 30.0, true, true);
    let out = sweep7(&r);
    assert_eq!(out["sessions"], 1);
    assert!(!r.projects().join("proj/sess.jsonl").exists());
    assert!(!r.projects().join("proj/sess").exists());
    for f in ["proj/sess.jsonl.gz", "proj/sess/tool-results/out.txt.gz", "proj/sess/subagents/a.jsonl.gz"] {
        assert!(r.archive().join(f).exists(), "{f}");
    }
    assert_eq!(out["files"], 3);
}

/// A project's memory/ sits where a sidecar sits and must survive.
#[test]
fn memory_is_never_swept() {
    let r = rig();
    let note = r.projects().join("proj/memory/MEMORY.md");
    write(&note, "remembered", 400.0);
    sweep7(&r);
    assert!(note.exists());
}

#[test]
fn orphaned_sidecar_is_swept() {
    let r = rig();
    make_session(&r.projects(), "proj", "orph", 30.0, true, false);
    let out = sweep7(&r);
    assert_eq!(out["orphans"], 1);
    assert!(!r.projects().join("proj/orph").exists());
    assert!(r.archive().join("proj/orph/tool-results/out.txt.gz").exists());
}

/// Age is the newest file anywhere in the session, not the transcript's.
#[test]
fn a_recent_subagent_keeps_the_session_live() {
    let r = rig();
    let p = make_session(&r.projects(), "proj", "sess", 30.0, true, true);
    age(&p.join("sess/subagents/a.jsonl"), 0.0);
    assert_eq!(sweep7(&r)["sessions"], 0);
    assert!(r.projects().join("proj/sess.jsonl").exists());
}

#[test]
fn young_sessions_are_left_alone() {
    let r = rig();
    make_session(&r.projects(), "proj", "sess", 1.0, true, true);
    assert_eq!(sweep7(&r)["sessions"], 0);
    assert!(r.projects().join("proj/sess.jsonl").exists());
}

#[test]
fn dry_run_moves_nothing() {
    let r = rig();
    make_session(&r.projects(), "proj", "sess", 30.0, true, true);
    let out = sweep::archive(&r.app.tree, 7.0, true, &HashSet::new(), None);
    assert_eq!(out["sessions"], 1);
    assert!(out["bytes_before"].as_u64().unwrap() > 0);
    assert!(r.projects().join("proj/sess.jsonl").exists());
    assert!(!r.archive().join("proj").exists());
}

#[test]
fn archived_transcript_round_trips_with_its_mtime_and_private_modes() {
    let r = rig();
    let p = make_session(&r.projects(), "proj", "sess", 30.0, true, true);
    let mtime = std::fs::metadata(p.join("sess.jsonl")).unwrap().modified().unwrap();
    sweep7(&r);
    let gz = r.archive().join("proj/sess.jsonl.gz");
    assert_eq!(gunzip(&gz), "{\"type\":\"user\"}\n");
    assert_eq!(std::fs::metadata(&gz).unwrap().modified().unwrap(), mtime);
    assert_eq!(std::fs::metadata(&gz).unwrap().permissions().mode() & 0o777, 0o600);
    assert_eq!(std::fs::metadata(r.archive().join("proj/sess/subagents")).unwrap().permissions().mode() & 0o777, 0o700);
    assert!(std::fs::read_dir(r.archive().join("proj")).unwrap().flatten().all(|e| !e.file_name().to_string_lossy().ends_with(".tmp")));
}

#[test]
fn restore_puts_the_whole_session_back() {
    let r = rig();
    make_session(&r.projects(), "proj", "sess", 30.0, true, true);
    sweep7(&r);
    let out = sweep::restore(&r.app.tree, "proj", "sess").unwrap();
    assert_eq!(out["files"], 3);
    assert_eq!(out["path"], json!(r.projects().join("proj/sess.jsonl").to_string_lossy()));
    assert!(r.projects().join("proj/sess.jsonl").exists());
    assert_eq!(std::fs::read_to_string(r.projects().join("proj/sess/tool-results/out.txt")).unwrap(), "tool output");
}

#[test]
fn restore_refuses_to_overwrite_a_live_session() {
    let r = rig();
    make_session(&r.projects(), "proj", "sess", 30.0, true, true);
    sweep7(&r);
    make_session(&r.projects(), "proj", "sess", 0.0, true, true);
    let e = sweep::restore(&r.app.tree, "proj", "sess").unwrap_err();
    assert_eq!(e.class, "FileExistsError");
    assert_eq!(e.message, "proj/sess already exists in the live tree; refusing to overwrite it");
}

#[test]
fn restore_refuses_a_path_outside_the_archive() {
    let r = rig();
    for (p, s) in [("..", "x"), ("proj", "../x"), ("a/b", "x")] {
        assert_eq!(sweep::restore(&r.app.tree, p, s).unwrap_err().class, "FileNotFoundError");
    }
}

#[test]
fn prune_defaults_to_reporting_and_deletes_when_told() {
    let r = rig();
    make_session(&r.projects(), "proj", "sess", 400.0, true, true);
    sweep7(&r);
    let out = sweep::prune(&r.app.tree, 180.0, true, None);
    assert_eq!((out["sessions"].clone(), out["dry_run"].clone()), (json!(1), json!(true)));
    assert!(r.archive().join("proj/sess.jsonl.gz").exists());
    let out = sweep::prune(&r.app.tree, 180.0, false, None);
    assert_eq!(out["sessions"], 1);
    assert!(!r.archive().join("proj/sess.jsonl.gz").exists());
    assert!(!r.archive().join("proj/sess").exists());
}

/// A background job parked for a fortnight is idle, not finished.
#[test]
fn a_session_a_process_still_holds_is_never_swept() {
    let r = rig();
    make_session(&r.projects(), "proj", "parked", 30.0, true, true);
    let out = sweep::archive(&r.app.tree, 7.0, false, &HashSet::from(["parked".to_string()]), None);
    assert_eq!(out["sessions"], 0);
    assert!(r.projects().join("proj/parked.jsonl").exists());
}

/// Claude Code writes into these trees while the sweep runs: a name that went counts, and the
/// rest of the session still moves.
#[test]
fn a_file_that_vanishes_mid_walk_does_not_abort_the_session() {
    let r = rig();
    let p = make_session(&r.projects(), "proj", "sess", 30.0, true, true);
    std::os::unix::fs::symlink(p.join("sess/gone.txt"), p.join("sess/tool-results/vanished.txt")).unwrap();
    let out = sweep7(&r);
    assert_eq!((out["sessions"].clone(), out["vanished"].clone()), (json!(1), json!(1)));
    assert!(r.archive().join("proj/sess.jsonl.gz").exists());
    assert!(r.archive().join("proj/sess/subagents/a.jsonl.gz").exists());
}

/// A file the sweep cannot read fails its session whole: every live file stays, and no
/// half-moved sidecar is left.
#[test]
fn an_unreadable_file_keeps_its_whole_session_live() {
    let r = rig();
    let p = make_session(&r.projects(), "proj", "sess", 30.0, true, true);
    make_session(&r.projects(), "proj", "other", 30.0, true, true);
    let locked = p.join("sess/tool-results/out.txt");
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o000)).unwrap();
    let out = sweep7(&r);
    std::fs::set_permissions(&locked, std::fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(out["sessions"], 1, "the other session still moves");
    let failed = out["failed"].as_array().unwrap();
    assert_eq!(failed.len(), 1);
    assert_eq!((failed[0]["project"].clone(), failed[0]["session"].clone()), (json!("proj"), json!("sess")));
    assert!(failed[0]["error"].as_str().unwrap().starts_with("[Errno 13] Permission denied: '"), "{}", failed[0]["error"]);
    for f in ["sess.jsonl", "sess/tool-results/out.txt", "sess/subagents/a.jsonl"] {
        assert!(p.join(f).exists(), "{f} stays live");
    }
    assert!(!r.archive().join("proj/sess.jsonl.gz").exists());
    assert!(!r.archive().join("proj/sess/subagents/a.jsonl.gz").exists());
}
