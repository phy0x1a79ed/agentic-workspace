//! The append-only sync of the shared DVC cache to chinook: the daily archive.
//!
//! Chinook is the remote for the cache the way GitHub is the remote for the code. It
//! accumulates and never prunes, which is what makes a local gc survivable. The path this
//! writes is the path `transfer` reads back: `<prefix>/data/.dvc_cache/files/md5/...`. It is
//! a sibling of the mirror's `<prefix>/workspace/`, never its parent.

use serde_json::{json, Value};

use crate::config::ChinookConfig;
use crate::globus::{self, Api, Submit};
use crate::Paths;

pub const SYNC_LABEL: &str = "agentic_workspace dvc cache daily";

/// The transfer document: the shared cache, recursively, and nothing else.
pub fn build_items(paths: &Paths, cfg: &ChinookConfig) -> Vec<Value> {
    let cache = paths.shared_cache();
    let rel = cache.strip_prefix(&paths.workspace).unwrap_or(&cache).to_string_lossy().into_owned();
    vec![json!({
        "DATA_TYPE": "transfer_item",
        "source_path": format!("{}/", cache.display()),
        "destination_path": format!("{}/{rel}/", cfg.prefix),
        "recursive": true,
    })]
}

/// The append-only flags, in one place.
pub fn options(cfg: &ChinookConfig) -> Submit<'_> {
    Submit {
        source: &cfg.local_endpoint,
        destination: &cfg.remote_endpoint,
        label: SYNC_LABEL,
        // Objects are immutable and content-addressed: a same-size file is the right file.
        sync_level: 1,
        // The property that makes chinook a remote rather than a mirror.
        delete_destination_extra: false,
        // A scan of a 50k-object store always races a concurrent add or gc.
        skip_source_errors: true,
        filter_rules: None,
    }
}

/// Build the cache document and submit it. Single flight is the run table's job (`jobs`).
pub fn sync(paths: &Paths, cfg: &ChinookConfig, api: Option<&dyn Api>, dry_run: bool) -> anyhow::Result<Value> {
    let cache = paths.shared_cache();
    if !cache.is_dir() {
        anyhow::bail!(
            "shared cache {} does not exist — refusing to sync nothing, which on an append-only remote is a silent no-op rather than an error you would notice",
            cache.display()
        );
    }
    let items = build_items(paths, cfg);
    let mut out = json!({
        "source": cache.to_string_lossy(),
        "destination": format!("{}:{}", cfg.remote_endpoint, items[0]["destination_path"].as_str().unwrap_or("")),
        "append_only": true,
    });
    if dry_run {
        out["dry_run"] = json!(true);
        out["transfer_items"] = json!(items);
        return Ok(out);
    }
    let api = api.ok_or_else(|| anyhow::anyhow!("no Globus client"))?;
    let task = globus::submit(api, &items, &options(cfg))?;
    out["submitted"] = json!(true);
    out["task_id"] = json!(task);
    Ok(out)
}
