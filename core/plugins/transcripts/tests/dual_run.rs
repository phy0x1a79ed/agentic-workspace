//! The dual run against the Python transcripts, on a tree the Python capture script built.
//! Run with `DUAL_RUN_DIR=<dir> cargo test --test dual_run -- --ignored`, after the Python swept
//! `<dir>/py`. The Rust sweeps `<dir>/rs`, and the two archives must hold the same files with
//! the same contents and mtimes. Then the Rust restores every session from the Python archive.

use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};

use awm_plugin_transcripts::layout::{sessions, walk_files, Side, Tree};
use awm_plugin_transcripts::sweep;

fn listing(root: &Path) -> BTreeMap<PathBuf, (Vec<u8>, std::time::SystemTime)> {
    walk_files(root)
        .into_iter()
        .map(|f| {
            let raw = std::fs::read(&f).unwrap();
            let body = if f.extension().is_some_and(|e| e == "gz") {
                let mut out = vec![];
                std::io::Read::read_to_end(&mut flate2::read::MultiGzDecoder::new(&raw[..]), &mut out).unwrap();
                out
            } else {
                raw
            };
            (f.strip_prefix(root).unwrap().to_path_buf(), (body, std::fs::metadata(&f).unwrap().modified().unwrap()))
        })
        .collect()
}

#[test]
#[ignore]
fn the_rust_sweep_matches_the_python_one() {
    let dir = PathBuf::from(std::env::var("DUAL_RUN_DIR").expect("DUAL_RUN_DIR"));
    let rs = Tree { projects: dir.join("rs/projects"), archive: dir.join("rs/archive") };
    let py = Tree { projects: dir.join("py/projects"), archive: dir.join("py/archive") };
    let out = sweep::archive(&rs, 7.0, false, &HashSet::new(), None);
    println!("{out}");
    assert_eq!(listing(&rs.archive), listing(&py.archive), "the archives");
    assert_eq!(listing(&rs.projects), listing(&py.projects), "what stays live");

    let back = Tree { projects: dir.join("back/projects"), archive: py.archive.clone() };
    for s in sessions(&py.archive, Side::Archive, None) {
        sweep::restore(&back, &s.project, &s.session_id).unwrap();
    }
    let fixture = listing(&dir.join("fixture"));
    for (rel, file) in listing(&back.projects) {
        assert_eq!(fixture.get(&rel), Some(&file), "{} restored from the Python archive", rel.display());
    }
}
