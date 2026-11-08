//! The sync carries the cache and only the cache, and never deletes on chinook.

mod common;

use std::collections::BTreeSet;
use std::path::Path;

use awm_plugin_dvc::{jobs, sync, transfer};

const PREFIX: &str = "/Workspace_backups/someone/somenode";

fn ws_with_cache() -> common::Ws {
    let w = common::workspace();
    std::fs::create_dir_all(w.paths.shared_cache()).unwrap();
    w
}

#[test]
fn the_whole_transfer_is_one_recursive_item_for_the_shared_cache() {
    let w = ws_with_cache();
    let items = sync::build_items(&w.paths, &common::cfg(PREFIX));

    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["source_path"], format!("{}/", w.paths.shared_cache().display()));
    assert_eq!(items[0]["destination_path"], format!("{PREFIX}/data/.dvc_cache/"));
    assert_eq!(items[0]["recursive"], true);
}

#[test]
fn the_destination_is_where_pull_reads_objects_back_from() {
    // If these two disagree, a restore finds nothing and fails silently.
    let w = ws_with_cache();
    let cfg = common::cfg(PREFIX);
    let item = &sync::build_items(&w.paths, &cfg)[0];
    let hashes: BTreeSet<String> = [format!("ab{}", "c".repeat(30))].into();
    let obj = &transfer::transfer_items(&w.paths, &hashes, &w.paths.shared_cache(), &cfg, true)[0];

    assert!(obj["source_path"].as_str().unwrap().starts_with(item["destination_path"].as_str().unwrap()), "{obj} vs {item}");
}

#[test]
fn the_cache_lives_inside_the_workspace_so_it_has_a_remote_path_at_all() {
    let w = common::workspace();
    assert_eq!(w.paths.shared_cache().strip_prefix(&w.paths.workspace).unwrap(), Path::new("data/.dvc_cache"));
}

#[test]
fn sync_never_deletes_on_the_remote() {
    let w = ws_with_cache();
    let api = common::submitting_api();

    let out = sync::sync(&w.paths, &common::cfg(PREFIX), Some(api.as_ref()), false).unwrap();

    assert_eq!(out["task_id"], "task-1");
    let posts = api.posts();
    assert_eq!(posts.len(), 1);
    let doc = &posts[0];
    assert_eq!(doc["delete_destination_extra"], false);
    assert_eq!(doc["sync_level"], 1);
    assert_eq!(doc["skip_source_errors"], true);
    assert_eq!(doc["DATA"].as_array().unwrap().len(), 1);
    assert!(doc.get("filter_rules").is_none());
    assert_eq!(doc["label"], sync::SYNC_LABEL);
}

#[test]
fn a_dry_run_submits_nothing_and_says_what_it_would_do() {
    let w = ws_with_cache();

    let out = sync::sync(&w.paths, &common::cfg(PREFIX), None, true).unwrap();

    assert_eq!(out["dry_run"], true);
    assert_eq!(out["append_only"], true);
    assert_eq!(out["transfer_items"].as_array().unwrap().len(), 1);
    assert!(out.get("task_id").is_none());
}

#[test]
fn a_missing_cache_is_an_error_not_an_empty_backup() {
    let w = common::workspace();
    let api = common::submitting_api();

    let err = sync::sync(&w.paths, &common::cfg(PREFIX), Some(api.as_ref()), false).unwrap_err();

    assert_eq!(jobs::class_of(&err), "FileNotFoundError");
    assert!(api.calls().is_empty());
}
