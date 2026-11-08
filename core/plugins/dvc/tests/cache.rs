//! Pin resolution, which the transfers lean on: what a scope needs, and what it cannot yet
//! know. The code lives in awm-dvcdata's `scope` and `odb`; nothing there tests it.

mod common;

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use awm_dvcdata::odb::object_relpath;
use awm_dvcdata::scope::{cache_dir_for, find_pins, open_scope, pin_hashes, resolve};
use common::touch;
use serde_json::json;

fn put_object(cache: &Path, h: &str, body: &str) {
    touch(&cache.join(object_relpath(h)), body);
}

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
fn object_relpath_shards_on_first_two_chars() {
    assert_eq!(object_relpath("abcdef"), "files/md5/ab/cdef");
}

#[test]
fn object_relpath_keeps_dir_suffix_in_the_filename() {
    assert_eq!(object_relpath("ab1234.dir"), "files/md5/ab/1234.dir");
}

#[test]
fn find_pins_excludes_the_dvc_config_dir_but_not_pins_named_like_it() {
    let (_t, _root, s) = scope();
    touch(&s.join("data/swissprot.dvc"), "outs:\n- md5: aaa\n");
    touch(&s.join(".dvc/tmp/stale.dvc"), "outs:\n- md5: bbb\n");

    let found: Vec<String> = find_pins(&s).iter().map(|p| p.file_name().unwrap().to_string_lossy().into_owned()).collect();

    assert_eq!(found, ["swissprot.dvc"]);
}

#[test]
fn pin_hashes_reads_outs_and_ignores_deps() {
    let (_t, _root, s) = scope();
    let pin = s.join("a.dvc");
    touch(&pin, "deps:\n- md5: dep000\n  path: x\nouts:\n- md5: out111\n  path: y\n");

    assert_eq!(pin_hashes(&pin), ["out111"]);
}

#[test]
fn unparseable_pin_is_skipped_not_fatal() {
    let (_t, _root, s) = scope();
    let pin = s.join("bad.dvc");
    touch(&pin, "outs:\n- md5: [unclosed\n");

    assert!(pin_hashes(&pin).is_empty());
}

#[test]
fn resolve_expands_a_local_manifest_into_its_leaves() {
    let (_t, _root, s) = scope();
    let cache = s.join(".dvc/cache");
    let manifest = "aa0000.dir";
    put_object(&cache, manifest, &json!([{"md5": "bb1111", "relpath": "f1"}, {"md5": "cc2222", "relpath": "f2"}]).to_string());
    put_object(&cache, "bb1111", "present");
    touch(&s.join("d.dvc"), &format!("outs:\n- md5: {manifest}\n  path: d\n"));

    let res = resolve(&s, &cache);

    assert_eq!(res.objects, set(&[manifest, "bb1111", "cc2222"]));
    assert!(res.unresolved.is_empty());
    assert_eq!(res.present, set(&[manifest, "bb1111"]));
    assert_eq!(res.missing, set(&["cc2222"]));
    assert_eq!(res.pins, 1);
}

#[test]
fn absent_manifest_is_unresolved_and_hides_its_leaves() {
    let (_t, _root, s) = scope();
    let cache = s.join(".dvc/cache");
    touch(&s.join("d.dvc"), "outs:\n- md5: aa0000.dir\n  path: d\n");

    let res = resolve(&s, &cache);

    assert_eq!(res.unresolved, set(&["aa0000.dir"]));
    assert_eq!(res.objects, set(&["aa0000.dir"]));
}

#[test]
fn cache_dir_config_local_wins_over_config() {
    let (_t, root, s) = scope();
    let shared = root.join("shared_cache");
    std::fs::create_dir_all(&shared).unwrap();
    touch(&s.join(".dvc/config"), "[cache]\n    dir = /wrong/path\n");
    touch(&s.join(".dvc/config.local"), &format!("[cache]\n    dir = {}\n", shared.display()));

    assert_eq!(cache_dir_for(&s), shared);
}

#[test]
fn cache_dir_relative_is_anchored_to_the_dvc_dir() {
    let (_t, _root, s) = scope();
    std::fs::create_dir_all(s.join("shared")).unwrap();
    touch(&s.join(".dvc/config"), "[cache]\n    dir = ../shared\n");

    assert_eq!(cache_dir_for(&s), s.join("shared"));
}

#[test]
fn cache_dir_relative_is_anchored_even_before_it_exists() {
    let (_t, _root, s) = scope();
    touch(&s.join(".dvc/config"), "[cache]\n    dir = ../shared\n");

    assert_eq!(cache_dir_for(&s), s.join("shared"));
}

#[test]
fn cache_dir_defaults_when_unconfigured() {
    let (_t, _root, s) = scope();
    assert_eq!(cache_dir_for(&s), s.join(".dvc/cache"));
}

#[test]
fn open_scope_rejects_a_non_dvc_path() {
    let (_t, root) = common::tempdir();
    let e = open_scope(&root, &root).unwrap_err();
    assert!(e.to_string().contains("not a DVC scope"), "{e}");
}

#[test]
fn open_scope_rejects_a_cache_outside_the_workspace() {
    let (_t, root, s) = scope();
    let outside = root.join("elsewhere");
    std::fs::create_dir_all(&outside).unwrap();
    touch(&s.join(".dvc/config"), &format!("[cache]\n    dir = {}\n", outside.display()));

    let e = open_scope(&s, &s).unwrap_err();
    assert!(e.to_string().contains("outside"), "{e}");
}

#[test]
fn open_scope_takes_a_workspace_relative_path() {
    let (_t, root, s) = scope();
    let (scope, cache) = open_scope(Path::new("scope"), &root).unwrap();
    assert_eq!(scope, s);
    assert_eq!(cache, s.join(".dvc/cache"));
}
