//! Agreement with the real DVC, and with real caches. Each test runs only when its
//! variable is set, and passes as a no-op otherwise:
//! - `AWM_DVC_BIN`: a DVC 3 binary to compare against.
//! - `AWM_DVC_ROUNDTRIP_CACHE`: a cache dir whose every `.dir` object must re-serialise
//!   to its own bytes. The test prints counts only, never a path or a name.

mod common;

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use awm_dvcdata::odb::{self, Manifest};
use awm_dvcdata::status::changed_outs;
use awm_dvcdata::testkit;
use common::*;

fn dvc_bin() -> Option<PathBuf> {
    std::env::var_os("AWM_DVC_BIN").map(PathBuf::from)
}

fn dvc(bin: &Path, cwd: &Path, args: &[&str]) -> (i32, String) {
    let o = Command::new(bin).args(args).current_dir(cwd).env("DVC_NO_ANALYTICS", "1").output().unwrap();
    (o.status.code().unwrap_or(-1), format!("{}{}", String::from_utf8_lossy(&o.stdout), String::from_utf8_lossy(&o.stderr)))
}

/// `dvc status --json` reduced to stage -> output -> state, as `changed_outs` reports it.
fn dvc_changed(bin: &Path, wt: &Path) -> std::collections::BTreeMap<String, std::collections::BTreeMap<String, String>> {
    let (_, out) = dvc(bin, wt, &["status", "--json"]);
    let start = out.find('{').unwrap_or(0);
    let v: serde_json::Value = serde_json::from_str(out[start..].trim()).unwrap_or_else(|e| panic!("{e}: {out}"));
    let mut m = std::collections::BTreeMap::new();
    for (stage, entries) in v.as_object().unwrap() {
        for e in entries.as_array().unwrap() {
            if let Some(co) = e.get("changed outs").and_then(|c| c.as_object()) {
                for (p, s) in co {
                    m.entry(stage.clone()).or_insert_with(std::collections::BTreeMap::new).insert(p.clone(), s.as_str().unwrap().to_string());
                }
            }
        }
    }
    m
}

fn ours(wt: &Path) -> std::collections::BTreeMap<String, std::collections::BTreeMap<String, String>> {
    changed_outs(wt, None).unwrap().into_iter().map(|(k, v)| (k, v.into_iter().map(|(p, s)| (p, s.as_str().to_string())).collect())).collect()
}

#[test]
fn status_agrees_with_dvc() {
    let Some(bin) = dvc_bin() else { return };
    let w = dvc_project();
    w.provision(&w.main);
    add_chunk(&w.main, "clean", &[("f.txt", "clean\n")]);
    add_chunk(&w.main, "edited", &[("f.txt", "e\n"), ("sub/g.txt", "g\n")]);
    add_chunk(&w.main, "gone", &[("f.txt", "gone\n")]);
    let lost = add_chunk(&w.main, "lost", &[("f.txt", "lost\n")]);
    commit_all(&w.main, "chunks");
    assert_eq!(dvc_changed(&bin, &w.main), ours(&w.main), "a clean tree");
    testkit::unprotect(&w.main.join("data/edited")).unwrap();
    fs::write(w.main.join("data/edited/sub/g.txt"), "g2\n").unwrap();
    fs::remove_dir_all(w.main.join("data/gone")).unwrap();
    fs::remove_file(odb::object_path(&w.cache(), &lost)).unwrap();
    assert_eq!(dvc_changed(&bin, &w.main), ours(&w.main));
}

#[test]
fn a_native_checkout_leaves_dvc_nothing_to_report() {
    let Some(bin) = dvc_bin() else { return };
    let w = dvc_project();
    w.provision(&w.main);
    add_chunk(&w.main, "c", &[("f.txt", "f\n"), ("a/b.txt", "b\n"), ("a-c", "c\n")]);
    commit_all(&w.main, "chunk");
    let s = w.scope("s");
    w.provision(&s);
    let (code, out) = dvc(&bin, &s, &["status", "--quiet"]);
    assert_eq!(code, 0, "{out}");
    assert_eq!(dvc(&bin, &w.main, &["status", "--quiet"]).0, 0);
}

#[test]
fn the_merge_driver_writes_what_dvc_writes() {
    let Some(bin) = dvc_bin() else { return };
    let w = dvc_project();
    w.provision(&w.main);
    add_chunk(&w.main, "c", &[("base.txt", "base\n"), ("drop.txt", "drop\n")]);
    let anc = fs::read_to_string(w.main.join("data/c.dvc")).unwrap();
    let side = |edit: &dyn Fn(&Path)| {
        fs::write(w.main.join("data/c.dvc"), &anc).unwrap();
        assert!(awm_dvcdata::checkout::checkout(&w.main, None).unwrap().ok());
        testkit::unprotect(&w.main.join("data/c")).unwrap();
        edit(&w.main.join("data/c"));
        testkit::add(&w.main, "data/c").unwrap();
        fs::read_to_string(w.main.join("data/c.dvc")).unwrap()
    };
    let our = side(&|d| fs::write(d.join("ours.txt"), "ours\n").unwrap());
    let their = side(&|d| fs::remove_file(d.join("drop.txt")).unwrap());
    let tmp = w.main.clone();
    fs::create_dir_all(&tmp).unwrap();
    for (n, t) in [("anc", &anc), ("their", &their), ("our-dvc", &our), ("our-native", &our)] {
        fs::write(tmp.join(n), t).unwrap();
    }
    let p = |n: &str| tmp.join(n).to_string_lossy().into_owned();
    let (code, out) = dvc(&bin, &w.main, &["git-hook", "merge-driver", "--ancestor", &p("anc"), "--our", &p("our-dvc"), "--their", &p("their")]);
    assert_eq!(code, 0, "{out}");
    awm_dvcdata::merge::merge_driver(&w.main, &tmp.join("anc"), &tmp.join("our-native"), &tmp.join("their")).unwrap();
    assert_eq!(fs::read_to_string(tmp.join("our-native")).unwrap(), fs::read_to_string(tmp.join("our-dvc")).unwrap());
}

#[test]
fn every_dir_object_of_a_real_cache_round_trips() {
    let Some(cache) = std::env::var_os("AWM_DVC_ROUNDTRIP_CACHE").map(PathBuf::from) else { return };
    let (mut ok, mut bad) = (0, 0);
    for shard in fs::read_dir(cache.join("files/md5")).unwrap().flatten() {
        let Ok(rd) = fs::read_dir(shard.path()) else { continue };
        for o in rd.flatten() {
            let name = o.file_name().to_string_lossy().into_owned();
            if !name.ends_with(".dir") {
                continue;
            }
            let oid = format!("{}{name}", shard.file_name().to_string_lossy());
            let bytes = fs::read(o.path()).unwrap();
            let same = Manifest::parse(&bytes, &oid).map(|m| m.to_bytes() == bytes && m.oid() == oid).unwrap_or(false);
            if same {
                ok += 1;
            } else {
                bad += 1;
            }
        }
    }
    eprintln!("round-trip: {ok} .dir objects identical, {bad} differ");
    assert_eq!(bad, 0);
    assert!(ok > 0);
}
