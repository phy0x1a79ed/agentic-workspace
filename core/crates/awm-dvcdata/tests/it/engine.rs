//! The native engine: ids, manifests, checkout safety, status and the merge driver.

use std::fs;

use awm_dvcdata::checkout::{checkout, Outcome};
use awm_dvcdata::merge::{merge_trees, rewrite_first_out};
use awm_dvcdata::odb::{self, Entry, Manifest};
use awm_dvcdata::pyjson;
use awm_dvcdata::status::{changed_outs, State};
use awm_dvcdata::testkit;
use crate::common::*;

fn names(v: &[&str]) -> Vec<String> {
    v.iter().map(|s| s.to_string()).collect()
}

#[test]
fn a_file_id_is_the_md5_of_its_bytes_with_crlf_kept() {
    let w = dvc_project();
    w.provision(&w.main);
    fs::create_dir_all(w.main.join("data")).unwrap();
    fs::write(w.main.join("data/crlf.txt"), "a\r\nb\r\n").unwrap();
    let oid = testkit::add(&w.main, "data/crlf.txt").unwrap();
    assert_eq!(oid, odb::md5_bytes(b"a\r\nb\r\n"));
    assert_ne!(oid, odb::md5_bytes(b"a\nb\n"), "DVC 3 hashes bytes, never dos2unix text");
}

#[test]
fn a_manifest_serialises_as_python_json_dumps() {
    let m = Manifest::from_entries(vec![Entry::new("z.txt", "b"), Entry::new("d\u{e9}/\u{fc}\u{1f600}", "a"), Entry::new("a-c", "c"), Entry::new("a/b", "d")]);
    let want = r#"[{"md5": "c", "relpath": "a-c"}, {"md5": "d", "relpath": "a/b"}, {"md5": "a", "relpath": "d\u00e9/\u00fc\ud83d\ude00"}, {"md5": "b", "relpath": "z.txt"}]"#;
    assert_eq!(String::from_utf8(m.to_bytes()).unwrap(), want);
    assert_eq!(Manifest::parse(want.as_bytes(), "x").unwrap(), m);
    assert_eq!(m.oid(), format!("{}.dir", odb::md5_bytes(want.as_bytes())));
    let v = serde_json::json!({"k": "q\"b\\s\u{7f}\u{1}\t", "a": [1, true, null]});
    assert_eq!(pyjson::dumps(&v), r#"{"a": [1, true, null], "k": "q\"b\\s\u007f\u0001\t"}"#);
}

#[test]
fn a_legacy_pin_is_refused_never_hashed() {
    let w = dvc_project();
    w.provision(&w.main);
    fs::create_dir_all(w.main.join("data")).unwrap();
    fs::write(w.main.join("data/old.dvc"), "outs:\n- md5: 0123456789abcdef0123456789abcdef\n  path: old\n").unwrap();
    let err = changed_outs(&w.main, Some(&names(&["data/old"]))).unwrap_err().to_string();
    assert!(err.contains("legacy"), "{err}");
    let r = checkout(&w.main, Some(&names(&["data/old"]))).unwrap();
    assert!(r.failures().contains("legacy"), "{}", r.failures());
    assert!(!w.main.join("data/old").exists());

    // And an all-digit hash is read from the text, not as a number.
    let digits = "01234567890123456789012345678901";
    let p = awm_dvcdata::pin::Pin::parse(&format!("outs:\n- md5: {digits}\n  hash: md5\n  path: x\n"), std::path::Path::new("x.dvc")).unwrap();
    assert_eq!(p.outs[0].md5.as_deref(), Some(digits));
}

#[test]
fn checkout_refuses_to_replace_content_no_cache_holds() {
    let w = dvc_project();
    w.provision(&w.main);
    add_chunk(&w.main, "c", &[("f.txt", "v1\n")]);
    testkit::unprotect(&w.main.join("data/c")).unwrap();
    fs::write(w.main.join("data/c/f.txt"), "unsaved edit\n").unwrap();
    fs::write(w.main.join("data/c/extra.txt"), "unsaved extra\n").unwrap();
    let r = checkout(&w.main, Some(&names(&["data/c"]))).unwrap();
    let why = r.failures();
    assert!(why.contains("in no cache") && why.contains("data/c/f.txt") && why.contains("data/c/extra.txt"), "{why}");
    assert_eq!(fs::read_to_string(w.main.join("data/c/f.txt")).unwrap(), "unsaved edit\n");
    assert!(w.main.join("data/c/extra.txt").exists());
}

#[test]
fn checkout_removes_saved_files_the_pin_dropped() {
    let w = dvc_project();
    w.provision(&w.main);
    add_chunk(&w.main, "c", &[("f.txt", "f\n"), ("sub/g.txt", "g\n")]);
    let v1 = fs::read_to_string(w.main.join("data/c.dvc")).unwrap();
    testkit::unprotect(&w.main.join("data/c")).unwrap();
    fs::remove_file(w.main.join("data/c/sub/g.txt")).unwrap();
    testkit::add(&w.main, "data/c").unwrap();
    let v2 = fs::read_to_string(w.main.join("data/c.dvc")).unwrap();

    fs::write(w.main.join("data/c.dvc"), &v1).unwrap();
    let r = checkout(&w.main, Some(&names(&["data/c"]))).unwrap();
    assert_eq!(r.outs[0].outcome, Outcome::Updated { linked: 1, removed: 0 });
    assert!(w.main.join("data/c/sub/g.txt").is_file());

    fs::write(w.main.join("data/c.dvc"), &v2).unwrap();
    let r = checkout(&w.main, Some(&names(&["data/c"]))).unwrap();
    assert_eq!(r.outs[0].outcome, Outcome::Updated { linked: 0, removed: 1 });
    assert!(!w.main.join("data/c/sub/g.txt").exists());
    assert_eq!(checkout(&w.main, None).unwrap().outs[0].outcome, Outcome::Unchanged);
}

#[test]
fn a_missing_object_fails_the_output_with_its_files_in_place() {
    let w = dvc_project();
    w.provision(&w.main);
    let oid = add_chunk(&w.main, "c", &[("f.txt", "f\n"), ("g.txt", "g\n")]);
    let v1 = fs::read_to_string(w.main.join("data/c.dvc")).unwrap();
    testkit::unprotect(&w.main.join("data/c")).unwrap();
    fs::write(w.main.join("data/c/f.txt"), "f2\n").unwrap();
    testkit::add(&w.main, "data/c").unwrap();
    let f1 = Manifest::load(&w.cache(), &oid).unwrap().entries.iter().find(|e| e.relpath == "f.txt").unwrap().md5.clone();
    let obj = odb::object_path(&w.cache(), &f1);
    awm_dvcdata::provision::chmod_dirs_writable(&w.cache());
    fs::remove_file(obj).unwrap();
    fs::write(w.main.join("data/c.dvc"), &v1).unwrap();
    let r = checkout(&w.main, None).unwrap();
    assert!(r.failures().contains("missing"), "{}", r.failures());
    assert_eq!(fs::read_to_string(w.main.join("data/c/f.txt")).unwrap(), "f2\n", "nothing is removed before the plan holds");
}

#[test]
fn dvcignored_files_are_neither_hashed_nor_removed() {
    let w = dvc_project();
    w.provision(&w.main);
    fs::write(w.main.join(".dvcignore"), "*.log\n").unwrap();
    add_chunk(&w.main, "c", &[("f.txt", "f\n")]);
    fs::write(w.main.join("data/c/run.log"), "noise\n").unwrap();
    assert!(changed_outs(&w.main, None).unwrap().is_empty());
    assert_eq!(checkout(&w.main, None).unwrap().outs[0].outcome, Outcome::Unchanged);
    assert!(w.main.join("data/c/run.log").exists());
}

#[test]
fn status_names_each_changed_output() {
    let w = dvc_project();
    w.provision(&w.main);
    add_chunk(&w.main, "m", &[("f.txt", "m\n")]);
    add_chunk(&w.main, "d", &[("f.txt", "d\n")]);
    let gone = add_chunk(&w.main, "n", &[("f.txt", "n\n")]);
    add_chunk(&w.main, "ok", &[("f.txt", "ok\n")]);
    testkit::unprotect(&w.main.join("data/m")).unwrap();
    fs::write(w.main.join("data/m/f.txt"), "m2\n").unwrap();
    fs::remove_dir_all(w.main.join("data/d")).unwrap();
    fs::remove_file(odb::object_path(&w.cache(), &gone)).unwrap();
    let got = changed_outs(&w.main, None).unwrap();
    let flat: Vec<(String, String, State)> = got.iter().flat_map(|(s, o)| o.iter().map(move |(p, st)| (s.clone(), p.clone(), *st))).collect();
    assert_eq!(
        flat,
        vec![
            ("data/d.dvc".into(), "data/d".into(), State::Deleted),
            ("data/m.dvc".into(), "data/m".into(), State::Modified),
            ("data/n.dvc".into(), "data/n".into(), State::NotInCache),
        ]
    );
}

fn tree(pairs: &[(&str, &str)]) -> Manifest {
    Manifest::from_entries(pairs.iter().map(|(p, m)| Entry::new(*p, *m)).collect())
}

fn merged(a: &[(&str, &str)], o: &[(&str, &str)], t: &[(&str, &str)]) -> Result<Vec<(String, String)>, String> {
    merge_trees(&tree(a), &tree(o), &tree(t)).map(|m| m.entries.into_iter().map(|e| (e.relpath, e.md5)).collect()).map_err(|e| e.to_string())
}

fn pairs(v: &[(&str, &str)]) -> Vec<(String, String)> {
    v.iter().map(|(a, b)| (a.to_string(), b.to_string())).collect()
}

/// The cases probed against dvc_data 3.18's `_merge` with every change type allowed.
#[test]
fn the_tree_merge_matches_dvc() {
    assert_eq!(merged(&[("a", "1")], &[("a", "2")], &[("a", "1")]), Ok(pairs(&[("a", "2")])));
    assert_eq!(merged(&[("a", "1")], &[("a", "2")], &[("a", "2")]), Ok(pairs(&[("a", "2")])));
    assert_eq!(merged(&[("a", "1")], &[("a", "2")], &[("a", "3")]), Err("unable to auto-merge the following paths:\na".into()));
    assert_eq!(merged(&[("a", "1"), ("b", "1")], &[("a", "2"), ("b", "1")], &[("b", "1")]), Err("unable to auto-merge the following paths:\na".into()));
    assert_eq!(
        merged(&[("a", "1"), ("b", "1")], &[("b", "1")], &[("b", "1")]),
        Err("unable to auto-merge the following paths:\nboth deleted: ('a',)".into())
    );
    assert_eq!(merged(&[("a", "1")], &[("a", "1"), ("b", "1")], &[("a", "2")]), Ok(pairs(&[("a", "2"), ("b", "1")])));
    assert_eq!(merged(&[], &[("x", "1")], &[("x", "2")]), Err("unable to auto-merge the following paths:\nx".into()));
}

#[test]
fn the_pin_rewrite_keeps_every_other_line() {
    let text = "# keep me\nouts:\n- md5: aaa.dir\n  size: 10\n  nfiles: 2\n  hash: md5\n  path: c\n  desc: kept\n";
    assert_eq!(rewrite_first_out(text, "bbb.dir", Some(30), 3), "# keep me\nouts:\n- md5: bbb.dir\n  size: 30\n  nfiles: 3\n  hash: md5\n  path: c\n  desc: kept\n");
    assert_eq!(rewrite_first_out(text, "bbb.dir", None, 3), "# keep me\nouts:\n- md5: bbb.dir\n  nfiles: 3\n  hash: md5\n  path: c\n  desc: kept\n");
    let bare = "outs:\n- md5: aaa.dir\n  hash: md5\n  path: c\n";
    assert_eq!(rewrite_first_out(bare, "bbb.dir", Some(5), 1), "outs:\n- md5: bbb.dir\n  hash: md5\n  path: c\n  size: 5\n  nfiles: 1\n");
}
