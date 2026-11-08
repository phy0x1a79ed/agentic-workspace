//! Tree partitioning: cover everything except the checkouts, the cache and the junk. This is
//! the one transfer that deletes, and the append-only archive sits beside it on chinook.

use crate::common;

use std::collections::BTreeSet;
use std::os::unix::fs::symlink;
use std::path::Path;

use awm_dvcdata::cachetag::{write_cache_tag, CACHEDIR_SIGNATURE};
use awm_plugin_dvc::backup;
use common::touch;
use serde_json::{json, Value};

fn sources(items: &[Value]) -> BTreeSet<String> {
    items.iter().map(|i| i["source_path"].as_str().unwrap().to_string()).collect()
}

fn set(v: &[String]) -> BTreeSet<String> {
    v.iter().cloned().collect()
}

fn s(p: &Path) -> String {
    p.to_string_lossy().into_owned()
}

fn tag() -> String {
    format!("{}\n# a cache\n", String::from_utf8_lossy(CACHEDIR_SIGNATURE))
}

// -- partitioning ---------------------------------------------------------------------------

/// Each case: files to create, excluded paths (relative), and the expected sources (relative).
#[test]
fn partition_covers_everything_except_the_exclusions() {
    type Case = (&'static [&'static str], &'static [&'static str], &'static [&'static str]);
    let cases: [Case; 8] = [
        (&["a/deep/f", "b/g"], &[], &["a/", "b/"]),
        (&["proj/assembly/out", "proj/keep/src"], &["proj/assembly"], &["proj/keep/"]),
        (&["data/assembly/out.bam", "src/transforms/assembly/run.py"], &["data/assembly"], &["src/"]),
        (&["top.txt", "drop/f"], &["drop"], &["top.txt"]),
        (&["a/b/c/excluded/f", "a/b/sibling/f", "a/other/f"], &["a/b/c/excluded"], &["a/b/sibling/", "a/other/"]),
        (&["pkg/__pycache__/m.pyc", "pkg/mod.py"], &[], &["pkg/"]),
        (&["proj/out/big.bam", "proj/node_modules/pkg/index.js", "proj/src.py"], &["proj/out"], &["proj/src.py"]),
        (&["keep/f", "drop/f"], &["drop"], &["keep/"]),
    ];
    for (files, excluded, want) in cases {
        let (_t, tmp) = common::tempdir();
        for f in files {
            touch(&tmp.join(f), "");
        }
        let ex: Vec<String> = excluded.iter().map(|e| s(&tmp.join(e))).collect();
        let items = backup::partition(&s(&tmp), &ex, "/backups/node");
        let want: BTreeSet<String> = want.iter().map(|w| format!("{}/{w}", s(&tmp))).collect();
        assert_eq!(sources(&items), want, "{files:?} minus {excluded:?}");
        for i in &items {
            let rel = i["source_path"].as_str().unwrap().strip_prefix(&format!("{}/", s(&tmp))).unwrap().to_string();
            assert_eq!(i["destination_path"], format!("/backups/node/{rel}"));
            assert_eq!(i["recursive"], rel.ends_with('/'));
        }
    }
}

#[test]
fn regenerable_dirs_and_cache_tags_are_found_by_name_and_signature() {
    let rules = backup::filter_rules();
    let names: BTreeSet<&str> = rules.iter().map(|r| r["name"].as_str().unwrap()).collect();
    assert_eq!(names, backup::REGENERABLE_DIRS.into_iter().collect());
    assert!(rules.iter().all(|r| r["method"] == "exclude" && r["type"] == "dir"));

    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("a/b/conda_cache/CACHEDIR.TAG"), &tag());
    touch(&tmp.join("a/b/conda_cache/pkgs/x"), "");
    touch(&tmp.join("keep/CACHEDIR.TAG"), "not a tag");
    let cache = tmp.join("data/.dvc_cache");
    touch(&cache.join("files/md5/ab/CACHEDIR.TAG"), &tag());
    assert_eq!(backup::cache_tagged_paths(&tmp, &[s(&cache)]), vec![s(&tmp.join("a/b/conda_cache"))], "found at depth, no signature ignored, skipped paths not walked");
}

#[test]
fn dvc_output_paths_resolve_relative_to_the_pin_and_survive_a_bad_pin() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("proj/multi.dvc"), "outs:\n- md5: aaa\n  path: first\n- md5: bbb\n  path: second\n");
    touch(&tmp.join("deep/nest/d.dvc"), "outs:\n- md5: aaa\n  path: out\n");
    touch(&tmp.join("bad.dvc"), "outs:\n- md5: [unclosed\n");

    let got: BTreeSet<String> = backup::dvc_output_paths(&tmp).into_iter().collect();

    assert_eq!(got, set(&[s(&tmp.join("proj/first")), s(&tmp.join("proj/second")), s(&tmp.join("deep/nest/out"))]));
}

// -- the assembled document -----------------------------------------------------------------

/// A miniature workspace with a cache, a pinned output, and junk.
fn mini() -> (tempfile::TempDir, std::path::PathBuf, awm_plugin_dvc::Paths) {
    let (t, tmp) = common::tempdir();
    let paths = common::paths_at(&tmp);
    touch(&paths.shared_cache().join("files/md5/ab/cdef"), "");
    touch(&tmp.join("projects/p/out.dvc"), "outs:\n- md5: aaa\n  path: out\n");
    touch(&tmp.join("projects/p/out/big.bam"), "");
    touch(&tmp.join("projects/p/__pycache__/m.pyc"), "");
    touch(&tmp.join("projects/p/src.py"), "");
    touch(&tmp.join("projects/p/tmp/CACHEDIR.TAG"), &tag());
    touch(&tmp.join(".awm/state.json"), "");
    (t, tmp, paths)
}

#[test]
fn the_document_excludes_the_cache_the_checkouts_and_the_junk() {
    let (_t, ws, paths) = mini();

    let (items, counts) = backup::build_items(&paths, &common::cfg("/backups/node"));
    let srcs = sources(&items);

    assert!(!srcs.iter().any(|x| x.contains(".dvc_cache")));
    assert!(!srcs.iter().any(|x| x.trim_end_matches('/').ends_with("/projects/p/out")));
    assert!(!srcs.iter().any(|x| x.contains("__pycache__")));
    assert!(!srcs.iter().any(|x| x.trim_end_matches('/').ends_with("/projects/p/tmp")));
    assert!(srcs.contains(&format!("{}/projects/p/src.py", s(&ws))));
    assert!(srcs.contains(&format!("{}/projects/p/out.dvc", s(&ws))));
    assert!(srcs.contains(&format!("{}/.awm/", s(&ws))));
    assert_eq!(counts, json!({"dvc_outputs": 1, "shared_stores": 0, "shared_cache": 1, "cache_tagged_dirs": 1, "total": 3}));
    for i in &items {
        assert!(i["destination_path"].as_str().unwrap().starts_with("/backups/node/workspace/"), "{i}");
    }
}

#[test]
fn a_tagged_pre_rust_snapshot_is_left_out_of_the_mirror() {
    let (_t, ws, paths) = mini();
    let snap = ws.join(".awm/services/dvc/.pre-rust");
    touch(&snap.join("dvc.db"), "old bytes");
    write_cache_tag(&snap, "a test").unwrap();
    touch(&ws.join(".awm/services/dvc/dvc.db"), "live");

    let tagged = backup::cache_tagged_paths(&ws, &[s(&paths.shared_cache())]);
    assert!(tagged.contains(&s(&snap)), "{tagged:?}");

    let (items, counts) = backup::build_items(&paths, &common::cfg("/backups/node"));
    let srcs = sources(&items);
    assert!(!srcs.iter().any(|x| x.contains(".pre-rust")), "{srcs:?}");
    assert!(srcs.contains(&format!("{}/.awm/services/dvc/dvc.db", s(&ws))), "{srcs:?}");
    assert_eq!(counts["cache_tagged_dirs"], 2);
}

#[test]
fn the_shared_chunk_store_is_excluded_like_a_checkout() {
    let (_t, ws, paths) = mini();
    std::fs::create_dir_all(ws.join("projects/p/.bare")).unwrap();
    touch(&ws.join("projects/p/data/ref/abc123/big.hmm"), "");

    let (items, counts) = backup::build_items(&paths, &common::cfg("/backups/node"));
    assert!(!sources(&items).iter().any(|x| x.contains("/projects/p/data")));
    assert_eq!(counts["shared_stores"], 1);
}

#[test]
fn the_mirror_submits_with_delete_enabled_and_mtime_comparison() {
    let (_t, _ws, paths) = mini();
    let api = common::submitting_api();

    let out = backup::backup(&paths, &common::cfg("/backups/node"), Some(api.as_ref()), false).unwrap();

    assert_eq!(out["task_id"], "task-1");
    assert_eq!(out["submitted"], true);
    assert_eq!(backup::destination_root(&common::cfg("/backups/node")), "/backups/node/workspace", "a sibling of the cache archive, not its parent");
    let dry = backup::backup(&paths, &common::cfg("/backups/node"), None, true).unwrap();
    assert_eq!(dry["dry_run"], true);
    assert!(dry.get("task_id").is_none() && api.posts().len() == 1);
    let posts = api.posts();
    assert_eq!(posts.len(), 1);
    let doc = &posts[0];
    assert_eq!(doc["delete_destination_extra"], true);
    assert_eq!(doc["sync_level"], 2);
    assert_eq!(doc["skip_source_errors"], true);
    assert_eq!(doc["filter_rules"], json!(backup::filter_rules()));
    assert_eq!(doc["label"], backup::BACKUP_LABEL);
    assert_eq!((doc["source_endpoint"].as_str(), doc["destination_endpoint"].as_str()), (Some("local-uuid"), Some("remote-uuid")));
}

// -- symlinks -------------------------------------------------------------------------------

#[test]
fn a_symlink_is_never_emitted_and_cannot_smuggle_an_excluded_tree_back_in() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("real/f"), "");
    touch(&tmp.join("real.txt"), "x");
    touch(&tmp.join("data/chunk/big.bin"), "payload");
    symlink(tmp.join("real"), tmp.join("link")).unwrap();
    symlink(tmp.join("real.txt"), tmp.join("alias.txt")).unwrap();
    std::fs::create_dir(tmp.join("awm")).unwrap();
    symlink(tmp.join("data"), tmp.join("awm/data")).unwrap();
    symlink(tmp.join("real"), tmp.join("real/self")).unwrap();

    let items = backup::partition(&s(&tmp), &[s(&tmp.join("data/chunk"))], "/dest");

    let srcs = sources(&items);
    assert!(srcs.contains(&format!("{}/real/", s(&tmp))) && srcs.contains(&format!("{}/real.txt", s(&tmp))));
    assert!(!srcs.iter().any(|x| x.contains("awm/data") || x.contains("link") || x.contains("alias") || x.contains("self")), "{srcs:?}");
}
