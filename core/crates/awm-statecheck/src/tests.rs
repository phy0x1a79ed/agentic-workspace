use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use rusqlite::Connection;
use sha2::{Digest, Sha256};

use crate::cachetag::has_cache_tag;
use crate::decl::{Additive, Decl, Extra, Transient};
use crate::inventory::{PRE_RUST, PRE_RUST_TMP};
use crate::snapshot::{snapshot_with, SnapError};
use crate::{check, prune, restore, snapshot_once, RestoreError};

/// A state dir with a WAL database whose newest rows sit only in the WAL, a plain file,
/// a subdir and a symlink. The connection stays open so the WAL is not checkpointed.
fn state(dir: &Path) -> Connection {
    fs::create_dir_all(dir.join("sub")).unwrap();
    fs::write(dir.join("config.json"), "{}").unwrap();
    fs::write(dir.join("sub/lease.json"), "{}").unwrap();
    std::os::unix::fs::symlink("config.json", dir.join("link")).unwrap();
    let c = Connection::open(dir.join("store.db")).unwrap();
    c.execute_batch(
        "PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;
         CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, state TEXT);
         CREATE TABLE log (line TEXT);
         CREATE TABLE cache (k TEXT PRIMARY KEY, v TEXT);
         INSERT INTO tasks VALUES ('t1', 'one', 'open'), ('t2', 'two', 'open');
         INSERT INTO log VALUES ('a'), ('b');
         INSERT INTO cache VALUES ('k1', 'v1');",
    )
    .unwrap();
    c
}

fn decl(dir: &Path) -> Decl {
    let mut d = Decl::whole(dir);
    d.transient.push(Transient { path: dir.join("sub/lease.json"), table: None });
    d.transient.push(Transient { path: dir.join("store.db"), table: Some("cache".into()) });
    d.additive.push(Additive { path: dir.join("store.db"), table: Some("ops_notes".into()), column: None });
    d.additive.push(Additive { path: dir.join("store.db"), table: Some("tasks".into()), column: Some("body".into()) });
    d
}

fn plenty(_: &Path) -> io::Result<u64> {
    Ok(u64::MAX)
}

fn tree_hash(root: &Path) -> String {
    let mut h = Sha256::new();
    let mut paths: Vec<PathBuf> = vec![];
    let mut stack = vec![root.to_path_buf()];
    while let Some(p) = stack.pop() {
        paths.push(p.clone());
        if p.is_dir() {
            stack.extend(fs::read_dir(&p).unwrap().map(|d| d.unwrap().path()));
        }
    }
    paths.sort();
    for p in paths {
        h.update(p.to_string_lossy().as_bytes());
        if p.is_file() {
            h.update(fs::read(&p).unwrap());
        }
    }
    hex::encode(h.finalize())
}

#[test]
fn a_wal_database_round_trips_through_snapshot_check_and_restore() {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join("svc");
    let c = state(&dir);
    assert!(dir.join("store.db-wal").exists());
    let d = decl(&dir);
    let out = snapshot_once("svc", &d).unwrap();
    assert_eq!(out.taken, [dir.join(PRE_RUST)]);
    let pre = dir.join(PRE_RUST);
    assert!(has_cache_tag(&pre));
    assert!(!pre.join("state/store.db-wal").exists(), "a database is copied by VACUUM INTO, never with its WAL");
    let copy = Connection::open(pre.join("state/store.db")).unwrap();
    assert_eq!(copy.query_row("SELECT count(*) FROM tasks", [], |r| r.get::<_, i64>(0)).unwrap(), 2);
    assert_eq!(fs::read_link(pre.join("state/link")).unwrap(), PathBuf::from("config.json"));
    assert!(check(&d).unwrap().ok());

    c.execute("DELETE FROM tasks WHERE id = 't1'", []).unwrap();
    let r = check(&d).unwrap();
    assert_eq!(r.lost.len(), 1, "{r:?}");
    assert!(r.lost[0].contains("tasks lost 1 rows by key"));
    assert!(!r.lost[0].contains("t1"), "a report never names a key");
    drop(c);

    assert!(matches!(restore(&d, &[PathBuf::from("store.db")], false), Err(RestoreError::NotProven)));
    let done = restore(&d, &[PathBuf::from("store.db")], true).unwrap();
    assert!(done[0].contains("revert the code and keep the live state"));
    let aside: Vec<_> = fs::read_dir(&dir).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).filter(|n| n.starts_with(".rust-")).collect();
    assert_eq!(aside.len(), 1);
    assert!(has_cache_tag(&dir.join(&aside[0])));
    assert!(dir.join(&aside[0]).join("store.db").exists(), "the live copy is kept aside");
    assert!(check(&d).unwrap().ok(), "{:?}", check(&d).unwrap());
}

#[test]
fn vacuum_into_leaves_deleted_text_behind_and_the_inventory_holds_no_value() {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join("svc");
    let c = state(&dir);
    c.execute_batch("PRAGMA secure_delete=OFF;").unwrap();
    c.execute("INSERT INTO tasks VALUES ('t9', ?1, 'x')", ["GONE-TEXT-7731 ".repeat(2000)]).unwrap();
    c.execute("DELETE FROM tasks WHERE id = 't9'", []).unwrap();
    c.execute("INSERT INTO tasks VALUES ('t3', 'CELL-VALUE-5512', 'x')", []).unwrap();
    c.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);").unwrap();
    let live = fs::read(dir.join("store.db")).unwrap();
    assert!(live.windows(14).any(|w| w == b"GONE-TEXT-7731"), "the live file still holds the deleted text");
    snapshot_once("svc", &decl(&dir)).unwrap();
    let copy = fs::read(dir.join(PRE_RUST).join("state/store.db")).unwrap();
    assert!(!copy.windows(14).any(|w| w == b"GONE-TEXT-7731"));
    let inv = fs::read_to_string(dir.join(PRE_RUST).join("inventory.json")).unwrap();
    assert!(!inv.contains("CELL-VALUE-5512") && !inv.contains("\"t3\""));
    let live_inv = serde_json::to_string(&crate::inventory("svc", &decl(&dir)).unwrap()).unwrap();
    assert!(!live_inv.contains("CELL-VALUE-5512"));
    assert!(live_inv.contains("\"rows\":3"));
}

#[test]
fn a_loss_fails_and_a_declared_change_passes() {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join("svc");
    let c = state(&dir);
    let d = decl(&dir);
    snapshot_once("svc", &d).unwrap();
    c.execute_batch(
        "DELETE FROM cache; CREATE TABLE ops_notes (id TEXT PRIMARY KEY); ALTER TABLE tasks ADD COLUMN body TEXT;
         CREATE TABLE stray (x);",
    )
    .unwrap();
    fs::remove_file(dir.join("sub/lease.json")).unwrap();
    let r = check(&d).unwrap();
    assert!(r.ok(), "{r:?}");
    assert_eq!(r.notes.len(), 1, "{r:?}");
    assert!(r.notes[0].contains("stray"));

    c.execute_batch("ALTER TABLE tasks DROP COLUMN state; DELETE FROM log WHERE line = 'a';").unwrap();
    fs::remove_file(dir.join("config.json")).unwrap();
    let r = check(&d).unwrap();
    let lost = r.lost.join("\n");
    assert!(lost.contains("column tasks.state is gone"), "{lost}");
    assert!(lost.contains("table log fell from 2 to 1 rows"), "{lost}");
    assert!(lost.contains("config.json: file is gone"), "{lost}");
    c.execute_batch("DROP TABLE log;").unwrap();
    assert!(check(&d).unwrap().lost.iter().any(|l| l.contains("table log is gone")));
}

#[test]
fn the_tag_comes_first_and_a_cut_short_copy_is_redone() {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join("svc");
    let _c = state(&dir);
    let d = decl(&dir);
    let tmp = dir.join(PRE_RUST_TMP);
    let mut calls = 0;
    let err = snapshot_with("svc", &d, &plenty, &mut |_| {
        calls += 1;
        if calls == 1 {
            assert!(has_cache_tag(&tmp), "the tag is written before the first item");
            return Ok(());
        }
        Err(io::Error::other("cut short"))
    });
    assert!(matches!(err, Err(SnapError::Io { .. })));
    assert!(tmp.exists());
    assert!(!dir.join(PRE_RUST).exists(), "a cut-short copy is never taken for a whole one");
    assert!(check(&d).unwrap().lost[0].contains("no .pre-rust/ snapshot"));
    let out = snapshot_once("svc", &d).unwrap();
    assert_eq!(out.taken.len(), 1);
    assert!(!tmp.exists());
    assert!(check(&d).unwrap().ok());
}

#[test]
fn a_second_copy_and_a_full_disk_are_refused() {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join("svc");
    let c = state(&dir);
    let d = decl(&dir);
    let low = |_: &Path| Ok(10);
    assert!(matches!(snapshot_with("svc", &d, &low, &mut |_| Ok(())), Err(SnapError::NoSpace { .. })));
    assert!(!dir.join(PRE_RUST_TMP).exists() && !dir.join(PRE_RUST).exists());
    snapshot_once("svc", &d).unwrap();
    let inv = dir.join(PRE_RUST).join("inventory.json");
    let before = fs::read(&inv).unwrap();
    c.execute("INSERT INTO tasks VALUES ('t4', 'four', 'open')", []).unwrap();
    let again = snapshot_once("svc", &d).unwrap();
    assert!(again.taken.is_empty());
    assert_eq!(again.existing, [dir.join(PRE_RUST)]);
    assert_eq!(fs::read(&inv).unwrap(), before);
}

#[test]
fn outside_trees_are_recorded_or_copied_but_never_written() {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join("svc");
    let _c = state(&dir);
    let big = t.path().join("big");
    fs::create_dir_all(big.join("inner")).unwrap();
    fs::write(big.join("inner/blob"), vec![7u8; 4096]).unwrap();
    let units = t.path().join("units");
    fs::create_dir_all(&units).unwrap();
    fs::write(units.join("awm-routine-a.timer"), "[Timer]").unwrap();
    fs::write(units.join("other.timer"), "[Timer]").unwrap();
    let (big_before, units_before) = (tree_hash(&big), tree_hash(&units));
    let mut d = decl(&dir);
    d.size_only.push(big.clone());
    d.size_only.push(dir.join("sub"));
    d.extra.push(Extra { name: "units".into(), path: units.join("awm-routine-*") });
    d.extra.push(Extra { name: "whole".into(), path: units.clone() });
    let out = snapshot_with("svc", &d, &|_| Ok(1_000_000), &mut |_| Ok(())).unwrap();
    assert_eq!(out.taken.len(), 1);
    assert_eq!((tree_hash(&big), tree_hash(&units)), (big_before, units_before));
    let pre = dir.join(PRE_RUST);
    assert!(pre.join("extra/whole/units/other.timer").is_file(), "a directory match copies with its contents");
    assert!(pre.join("extra/units/awm-routine-a.timer").is_file());
    assert!(!pre.join("extra/units/other.timer").exists());
    assert!(!pre.join("state/sub").exists(), "a size_only path inside a state dir is recorded, not copied");
    let inv = crate::check::read_inventory(&dir).unwrap();
    let rec = inv.size_only.iter().find(|s| s.path == big).unwrap();
    assert_eq!((rec.size, rec.files), (4096, 1));
    assert!(check(&d).unwrap().ok());
    fs::remove_file(units.join("awm-routine-a.timer")).unwrap();
    assert!(check(&d).unwrap().lost.iter().any(|l| l.contains("awm-routine-a.timer")));
}

#[test]
fn an_absent_dir_gets_an_inventory_only_snapshot() {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join("never");
    let d = Decl::whole(&dir);
    snapshot_once("svc", &d).unwrap();
    let inv = crate::check::read_inventory(&dir).unwrap();
    assert!(!inv.dirs[0].exists && inv.dirs[0].entries.is_empty());
    assert!(check(&d).unwrap().ok());
}

#[test]
fn prune_takes_tonys_dated_ok() {
    let t = tempfile::tempdir().unwrap();
    let dir = t.path().join("svc");
    let _c = state(&dir);
    fs::create_dir(dir.join("ro")).unwrap();
    fs::write(dir.join("ro/f"), "x").unwrap();
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(dir.join("ro"), fs::Permissions::from_mode(0o500)).unwrap();
    let d = decl(&dir);
    snapshot_once("svc", &d).unwrap();
    let today = chrono::NaiveDate::from_ymd_opt(2026, 11, 20).unwrap();
    assert!(matches!(prune(&d, "soon", today), Err(RestoreError::BadDate(_))));
    assert!(matches!(prune(&d, "2026-11-21", today), Err(RestoreError::FutureDate(_))));
    assert_eq!(prune(&d, "2026-11-20", today).unwrap(), [dir.join(PRE_RUST)]);
    assert!(!dir.join(PRE_RUST).exists() && dir.join("store.db").exists());
    fs::set_permissions(dir.join("ro"), fs::Permissions::from_mode(0o700)).unwrap();
}
