//! Tree partitioning: cover everything except the checkouts, the cache and the junk. This is
//! the one transfer that deletes, and the append-only archive sits beside it on chinook.

mod common;

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

#[test]
fn a_clean_tree_is_emitted_as_whole_recursive_dirs() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("a/deep/f"), "");
    touch(&tmp.join("b/g"), "");

    let items = backup::partition(&s(&tmp), &[], "/dest");

    assert_eq!(sources(&items), set(&[format!("{}/a/", s(&tmp)), format!("{}/b/", s(&tmp))]));
    assert!(items.iter().all(|i| i["recursive"] == true));
}

#[test]
fn an_excluded_path_is_dropped_and_its_siblings_survive() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("proj/assembly/out"), "");
    touch(&tmp.join("proj/keep/src"), "");

    let items = backup::partition(&s(&tmp), &[s(&tmp.join("proj/assembly"))], "/dest");

    assert_eq!(sources(&items), set(&[format!("{}/proj/keep/", s(&tmp))]));
}

#[test]
fn a_name_shared_with_an_excluded_dir_elsewhere_is_not_dropped() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("data/assembly/out.bam"), "");
    touch(&tmp.join("src/transforms/assembly/run.py"), "");

    let items = backup::partition(&s(&tmp), &[s(&tmp.join("data/assembly"))], "/dest");

    let srcs = sources(&items);
    assert!(srcs.contains(&format!("{}/src/", s(&tmp))));
    assert!(!srcs.iter().any(|x| x.contains("data/assembly")));
}

#[test]
fn destination_paths_are_rebased_under_the_prefix() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("keep/f"), "");
    touch(&tmp.join("drop/f"), "");

    let items = backup::partition(&s(&tmp), &[s(&tmp.join("drop"))], "/backups/node");

    let dests: Vec<&str> = items.iter().map(|i| i["destination_path"].as_str().unwrap()).collect();
    assert_eq!(dests, ["/backups/node/keep/"]);
}

#[test]
fn a_lone_file_beside_an_exclusion_is_emitted_non_recursively() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("top.txt"), "");
    touch(&tmp.join("drop/f"), "");

    let items = backup::partition(&s(&tmp), &[s(&tmp.join("drop"))], "/dest");

    assert_eq!(
        items,
        vec![json!({"DATA_TYPE": "transfer_item", "source_path": format!("{}/top.txt", s(&tmp)), "destination_path": "/dest/top.txt", "recursive": false})]
    );
}

#[test]
fn excluding_a_deep_path_still_covers_everything_above_it() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("a/b/c/excluded/f"), "");
    touch(&tmp.join("a/b/sibling/f"), "");
    touch(&tmp.join("a/other/f"), "");

    let items = backup::partition(&s(&tmp), &[s(&tmp.join("a/b/c/excluded"))], "/dest");

    assert_eq!(sources(&items), set(&[format!("{}/a/b/sibling/", s(&tmp)), format!("{}/a/other/", s(&tmp))]));
}

// -- pin discovery --------------------------------------------------------------------------

#[test]
fn dvc_output_paths_collects_every_out_not_just_the_first() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("proj/multi.dvc"), "outs:\n- md5: aaa\n  path: first\n- md5: bbb\n  path: second\n");

    assert_eq!(backup::dvc_output_paths(&tmp), vec![s(&tmp.join("proj/first")), s(&tmp.join("proj/second"))]);
}

#[test]
fn dvc_output_paths_resolves_relative_to_the_pin_not_the_root() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("deep/nest/d.dvc"), "outs:\n- md5: aaa\n  path: out\n");

    assert_eq!(backup::dvc_output_paths(&tmp), vec![s(&tmp.join("deep/nest/out"))]);
}

#[test]
fn an_unparseable_pin_does_not_abort_discovery() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("bad.dvc"), "outs:\n- md5: [unclosed\n");
    touch(&tmp.join("good.dvc"), "outs:\n- md5: aaa\n  path: out\n");

    assert_eq!(backup::dvc_output_paths(&tmp), vec![s(&tmp.join("out"))]);
}

// -- regenerable and cache-tagged directories -----------------------------------------------

#[test]
fn regenerable_dirs_go_to_globus_as_name_filters() {
    let rules = backup::filter_rules();

    let names: BTreeSet<&str> = rules.iter().map(|r| r["name"].as_str().unwrap()).collect();
    assert_eq!(names, backup::REGENERABLE_DIRS.into_iter().collect());
    assert!(rules.iter().all(|r| r["method"] == "exclude" && r["type"] == "dir"));
}

#[test]
fn a_regenerable_dir_does_not_split_its_parent_into_files() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("pkg/__pycache__/m.pyc"), "");
    touch(&tmp.join("pkg/mod.py"), "");

    let items = backup::partition(&s(&tmp), &[], "/dest");

    assert_eq!(sources(&items), set(&[format!("{}/pkg/", s(&tmp))]));
}

#[test]
fn a_regenerable_dir_is_never_emitted_as_an_item() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("proj/out/big.bam"), "");
    touch(&tmp.join("proj/node_modules/pkg/index.js"), "");
    touch(&tmp.join("proj/src.py"), "");

    let items = backup::partition(&s(&tmp), &[s(&tmp.join("proj/out"))], "/dest");

    assert_eq!(sources(&items), set(&[format!("{}/proj/src.py", s(&tmp))]));
}

#[test]
fn a_cache_tagged_dir_is_found_at_any_depth() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("a/b/conda_cache/CACHEDIR.TAG"), &tag());
    touch(&tmp.join("a/b/conda_cache/pkgs/x"), "");
    touch(&tmp.join("a/src.py"), "");

    assert_eq!(backup::cache_tagged_paths(&tmp, &[]), vec![s(&tmp.join("a/b/conda_cache"))]);
}

#[test]
fn a_tag_without_the_signature_is_ignored() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("keep/CACHEDIR.TAG"), "not a tag");

    assert!(backup::cache_tagged_paths(&tmp, &[]).is_empty());
}

#[test]
fn the_tag_walk_does_not_descend_into_skipped_paths() {
    let (_t, tmp) = common::tempdir();
    let cache = tmp.join("data/.dvc_cache");
    touch(&cache.join("files/md5/ab/CACHEDIR.TAG"), &tag());
    touch(&tmp.join("src/scratch/CACHEDIR.TAG"), &tag());

    let found = backup::cache_tagged_paths(&tmp, &[s(&cache)]);

    assert_eq!(found, vec![s(&tmp.join("src/scratch"))]);
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
fn no_destination_escapes_the_workspace_root() {
    let (_t, _ws, paths) = mini();

    let (items, _) = backup::build_items(&paths, &common::cfg("/backups/node"));

    assert!(!items.is_empty());
    for i in &items {
        assert!(i["destination_path"].as_str().unwrap().starts_with("/backups/node/workspace/"), "{i}");
    }
    assert!(!items.iter().any(|i| i["destination_path"].as_str().unwrap().starts_with("/backups/node/data/")));
}

#[test]
fn the_destination_root_is_a_sibling_of_the_cache_archive_not_its_parent() {
    let cfg = common::cfg("/backups/node");
    assert_eq!(backup::destination_root(&cfg), "/backups/node/workspace");
    assert!(!"/backups/node/data/.dvc_cache".starts_with(&format!("{}/", backup::destination_root(&cfg))));
}

#[test]
fn the_mirror_submits_with_delete_enabled_and_mtime_comparison() {
    let (_t, _ws, paths) = mini();
    let api = common::submitting_api();

    let out = backup::backup(&paths, &common::cfg("/backups/node"), Some(api.as_ref()), false).unwrap();

    assert_eq!(out["task_id"], "task-1");
    assert_eq!(out["submitted"], true);
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

#[test]
fn a_dry_run_submits_nothing_and_returns_the_document() {
    let (_t, _ws, paths) = mini();
    let api = common::submitting_api();

    let out = backup::backup(&paths, &common::cfg("/backups/node"), None, true).unwrap();

    assert_eq!(out["dry_run"], true);
    assert!(!out["transfer_items"].as_array().unwrap().is_empty());
    assert!(out.get("task_id").is_none());
    assert!(api.calls().is_empty());
}

// -- symlinks -------------------------------------------------------------------------------

#[test]
fn a_symlink_to_a_directory_is_never_emitted() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("real/f"), "");
    symlink(tmp.join("real"), tmp.join("link")).unwrap();

    let items = backup::partition(&s(&tmp), &[], "/dest");

    assert_eq!(sources(&items), set(&[format!("{}/real/", s(&tmp))]));
}

#[test]
fn a_symlink_to_a_file_is_skipped_too() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("real.txt"), "x");
    symlink(tmp.join("real.txt"), tmp.join("alias.txt")).unwrap();

    let items = backup::partition(&s(&tmp), &[], "/dest");

    assert_eq!(sources(&items), set(&[format!("{}/real.txt", s(&tmp))]));
}

#[test]
fn a_symlink_cannot_smuggle_an_excluded_tree_back_in() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("data/chunk/big.bin"), "payload");
    touch(&tmp.join("keep.txt"), "");
    std::fs::create_dir(tmp.join("awm")).unwrap();
    symlink(tmp.join("data"), tmp.join("awm/data")).unwrap();

    let items = backup::partition(&s(&tmp), &[s(&tmp.join("data/chunk"))], "/dest");

    let srcs = sources(&items);
    assert!(!srcs.iter().any(|x| x.contains("awm/data")));
    assert!(srcs.contains(&format!("{}/keep.txt", s(&tmp))));
}

#[test]
fn a_symlinked_dir_does_not_force_its_parent_to_be_walked() {
    let (_t, tmp) = common::tempdir();
    touch(&tmp.join("top/f"), "");
    symlink(tmp.join("top"), tmp.join("top/link")).unwrap();

    let items = backup::partition(&s(&tmp), &[], "/dest");

    assert_eq!(sources(&items), set(&[format!("{}/top/", s(&tmp))]));
}
