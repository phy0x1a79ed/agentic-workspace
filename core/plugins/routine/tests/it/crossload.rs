//! The land state crosses between land.py and `routine land` both ways.
//!
//! `py_land/` holds the state land.py wrote for a synthetic repo: a batch stopped on a
//! conflict with its lease (`mid`), then the bounce and the landed batch (`end`). Rust reads
//! each file and writes it back byte for byte.

use std::path::{Path, PathBuf};

use awm_plugin_routine::land::json::{self, J};
use awm_plugin_routine::land::{fleetconf, Land};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/py_land")
}

fn files(dir: &Path) -> Vec<PathBuf> {
    let mut out = vec![];
    for e in std::fs::read_dir(dir).unwrap() {
        let p = e.unwrap().path();
        if p.is_dir() {
            out.extend(files(&p));
        } else {
            out.push(p);
        }
    }
    out.sort();
    out
}

#[test]
fn rust_writes_back_what_python_wrote() {
    let all = files(&fixtures());
    assert_eq!(all.len(), 7);
    for f in all {
        let text = std::fs::read_to_string(&f).unwrap();
        assert_eq!(json::dumps(&json::loads(&text).unwrap()), text, "{}", f.display());
    }
}

/// A land dir for `proj` holding a copy of one fixture state, beside an empty bare repo.
fn state(tmp: &Path, which: &str) -> Land {
    let ws = tmp.join("ws");
    std::fs::create_dir_all(ws.join("projects/proj/.bare")).unwrap();
    let dir = tmp.join("awm/land/proj");
    for f in files(&fixtures().join(which)) {
        let to = dir.join(f.strip_prefix(fixtures().join(which)).unwrap());
        std::fs::create_dir_all(to.parent().unwrap()).unwrap();
        std::fs::copy(&f, &to).unwrap();
    }
    Land::new("proj", &ws, &tmp.join("awm"), "mizar", fleetconf::Fleet::default()).unwrap()
}

#[test]
fn rust_reads_a_stopped_batch_and_a_landed_one() {
    let tmp = tempfile::tempdir().unwrap();
    let mid = state(tmp.path(), "mid");
    let b = mid.batch().unwrap().unwrap();
    assert!(b.str("failed").contains("conflicts with the batch in app.txt"));
    // The merger holds the role while its lease runs, and loses it once the lease runs out.
    let lease = mid.dir.join("lease.json");
    let mut l = json::loads(&std::fs::read_to_string(&lease).unwrap()).unwrap();
    l.set("until", J::from(1e12));
    std::fs::write(&lease, json::dumps(&l)).unwrap();
    assert_eq!(mid.merger().unwrap(), "d85df33d66e9");
    l.set("until", J::from(0i64));
    std::fs::write(&lease, json::dumps(&l)).unwrap();
    assert_eq!(mid.merger().unwrap(), "");
    let status = mid.status().unwrap();
    assert_eq!(status.get("queued").unwrap().as_arr().len(), 0);
    let a = b.get("tickets").unwrap().as_arr()[1].as_str().unwrap().to_string();
    assert_eq!(mid.wait(&a, 0.0).unwrap().str("verdict"), "merger");

    let tmp = tempfile::tempdir().unwrap();
    let end = state(tmp.path(), "end");
    assert!(end.batch().unwrap().is_none());
    let w = end.wait(&a, 0.0).unwrap();
    assert_eq!((w.str("verdict"), w.str("release").len()), ("landed", 40));
    let tickets = end.queued().unwrap();
    assert!(tickets.is_empty());
}

