//! Pin resolution, which the transfers lean on: what a scope needs, and what it cannot yet
//! know. The code lives in awm-dvcdata's `scope` and `odb`.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use crate::common;
use awm_dvcdata::odb::object_relpath;
use awm_dvcdata::scope::{cache_dir_for, find_pins, open_scope, pin_hashes, resolve};
use common::touch;
use serde_json::json;

fn set(v: &[&str]) -> BTreeSet<String> {
    v.iter().map(|s| s.to_string()).collect()
}

fn scope() -> (tempfile::TempDir, PathBuf, PathBuf) {
    let (t, root) = common::tempdir();
    let s = root.join("scope");
    std::fs::create_dir_all(s.join(".dvc/cache")).unwrap();
    (t, root, s)
}

#[test]
fn pins_are_found_and_read_the_way_the_transfers_need() {
    assert_eq!(object_relpath("abcdef"), "files/md5/ab/cdef");
    assert_eq!(object_relpath("ab1234.dir"), "files/md5/ab/1234.dir");

    let (_t, _root, s) = scope();
    touch(&s.join("data/swissprot.dvc"), "outs:\n- md5: aaa\n");
    touch(&s.join(".dvc/tmp/stale.dvc"), "outs:\n- md5: bbb\n");
    let found: Vec<String> = find_pins(&s).iter().map(|p| p.file_name().unwrap().to_string_lossy().into_owned()).collect();
    assert_eq!(found, ["swissprot.dvc"], "the .dvc dir is skipped, a pin named like it is not");

    let pin = s.join("a.dvc");
    touch(&pin, "deps:\n- md5: dep000\n  path: x\nouts:\n- md5: out111\n  path: y\n");
    assert_eq!(pin_hashes(&pin), ["out111"], "deps are ignored");
    touch(&pin, "outs:\n- md5: [unclosed\n");
    assert!(pin_hashes(&pin).is_empty(), "an unparseable pin is skipped, not fatal");
}

#[test]
fn resolve_expands_manifests_and_hides_the_leaves_of_an_absent_one() {
    let (_t, _root, s) = scope();
    let cache = s.join(".dvc/cache");
    let put = |h: &str, body: &str| touch(&cache.join(object_relpath(h)), body);
    put("aa0000.dir", &json!([{"md5": "bb1111", "relpath": "f1"}, {"md5": "cc2222", "relpath": "f2"}]).to_string());
    put("bb1111", "present");
    touch(&s.join("d.dvc"), "outs:\n- md5: aa0000.dir\n  path: d\n");
    touch(&s.join("absent.dvc"), "outs:\n- md5: dd3333.dir\n  path: e\n");

    let res = resolve(&s, &cache);

    assert_eq!(res.objects, set(&["aa0000.dir", "bb1111", "cc2222", "dd3333.dir"]));
    assert_eq!(res.present, set(&["aa0000.dir", "bb1111"]));
    assert_eq!(res.missing, set(&["cc2222", "dd3333.dir"]));
    assert_eq!(res.unresolved, set(&["dd3333.dir"]));
    assert_eq!(res.pins, 2);
}

#[test]
fn the_cache_dir_comes_from_config_local_then_config_then_the_default() {
    let (_t, root, s) = scope();
    assert_eq!(cache_dir_for(&s), s.join(".dvc/cache"));
    touch(&s.join(".dvc/config"), "[cache]\n    dir = ../shared\n");
    assert_eq!(cache_dir_for(&s), s.join("shared"), "a relative dir is anchored to the .dvc dir, even before it exists");
    let shared = root.join("shared_cache");
    std::fs::create_dir_all(&shared).unwrap();
    touch(&s.join(".dvc/config.local"), &format!("[cache]\n    dir = {}\n", shared.display()));
    assert_eq!(cache_dir_for(&s), shared);
}

#[test]
fn open_scope_refuses_a_non_dvc_path_and_a_cache_outside_the_workspace() {
    let (_t, root, s) = scope();
    let (found, cache) = open_scope(Path::new("scope"), &root).unwrap();
    assert_eq!((found, cache), (s.clone(), s.join(".dvc/cache")));
    assert!(open_scope(&root, &root).unwrap_err().to_string().contains("not a DVC scope"));

    let outside = root.join("elsewhere");
    std::fs::create_dir_all(&outside).unwrap();
    touch(&s.join(".dvc/config"), &format!("[cache]\n    dir = {}\n", outside.display()));
    assert!(open_scope(&s, &s).unwrap_err().to_string().contains("outside"));
}
