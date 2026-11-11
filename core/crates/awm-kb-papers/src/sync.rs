//! The daily Zotero sync, inside kb: the library items that moved since the served
//! generation's cursor join it in place. A matched semdb paper gains [`LIBRARY`] and keeps its
//! vectors. An item semdb lacks is written whole and embedded here.
//!
//! kb holds a long poll open on zotero's `items`, which answers once a library item moves past
//! the served cursor, and syncs then. The poll goes through zotero's singleton door, which
//! carries calls but no event stream. A session stop on `reflection/sessions` is the backstop, at
//! most once per [`SYNC_GAP_S`]. The cursor lives in the generation, so a new generation keeps
//! its own.

use crate::import::{held_row, item_keys, norm_title, write_atomic, LIBRARY};
use crate::{Generation, Papers};
use anyhow::{Context as _, Result};
use awm_index::sql::quote;
use awm_plugin_sdk::{CallOpts, Context, Hub, SubscriptionOpts, SupervisedSubscription};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

/// The shortest gap between two syncs.
pub const SYNC_GAP_S: f64 = 24.0 * 3600.0;
/// The cursor file in a generation.
pub const CURSOR: &str = "zotero.json";
const PAGE: usize = 200;
/// How long the long poll asks zotero to hold, under its singleton door's 130 s.
const WAIT_S: u64 = 100;
/// The pause after a failed poll or sync.
const RETRY: Duration = Duration::from_secs(60);
/// One sync at a time, whichever path started it.
static SYNCING: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

/// `{versions: {library: version}, synced: <unix s>}`, as a generation holds it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Cursor {
    pub versions: BTreeMap<String, i64>,
    pub synced: Option<f64>,
}

impl Cursor {
    pub fn load(dir: &Path) -> Cursor {
        let Ok(v) = std::fs::read_to_string(dir.join(CURSOR)).map_err(|_| ()).and_then(|t| serde_json::from_str::<Value>(&t).map_err(|_| ())) else {
            return Cursor::default();
        };
        let versions = v["versions"].as_object().into_iter().flatten().filter_map(|(k, n)| Some((k.clone(), n.as_i64()?))).collect();
        Cursor { versions, synced: v["synced"].as_f64() }
    }

    pub fn save(&self, dir: &Path) -> Result<()> {
        write_atomic(&dir.join(CURSOR), &json!({"versions": self.versions, "synced": self.synced}).to_string())
    }

    /// Raise each library's version to the highest item version seen.
    pub fn take(&mut self, items: &[Map<String, Value>]) {
        for i in items {
            let (Some(lib), Some(v)) = (i.get("library").and_then(Value::as_str), i.get("version").and_then(Value::as_i64)) else { continue };
            let slot = self.versions.entry(lib.to_string()).or_insert(v);
            *slot = (*slot).max(v);
        }
    }

    pub fn due(&self, now: f64) -> bool {
        self.synced.is_none_or(|t| now - t > SYNC_GAP_S)
    }
}

fn now() -> f64 {
    awm_pycompat::time::now_us() as f64 / 1e6
}

/// The node to ask for Zotero items: `AWM_ZOTERO_PEER`, or none, which lets the hub route the
/// call to zotero's singleton.
fn zotero_peer() -> Option<String> {
    std::env::var("AWM_ZOTERO_PEER").ok().filter(|p| !p.is_empty() && p != "local")
}

/// A zotero reply, unwrapped from the `result` a routed call puts it in.
fn page(reply: Value) -> Value {
    match reply.get("result") {
        Some(inner) if reply.get("items").is_none() => inner.clone(),
        _ => reply,
    }
}

/// Every item that moved since `since`, page by page, with the library versions at the end.
async fn moved(hub: &Hub, since: &BTreeMap<String, i64>) -> Result<Vec<Map<String, Value>>> {
    let peer = zotero_peer();
    let mut out = Vec::new();
    let mut offset = Some(0u64);
    while let Some(at) = offset {
        let page = page(hub.call("zotero", "items", json!({"since": since, "offset": at, "limit": PAGE}), peer.as_deref()).await?);
        out.extend(page["items"].as_array().into_iter().flatten().filter_map(|i| i.as_object().cloned()));
        offset = page["next"].as_u64();
    }
    Ok(out)
}

/// The served paper an item names by its match keys, or `None` when it names none.
async fn matching(g: &Generation, keys: crate::import::ItemKeys) -> Result<Option<String>> {
    if let Some(doi) = keys.doi {
        if let Some(id) = g.index.ids(&format!("doi = {}", quote(&doi)), Some(1)).await?.pop() {
            return Ok(Some(id));
        }
    }
    let Some((title, y)) = keys.title_year else { return Ok(None) };
    let cond = format!("ts >= timestamp '{y:04}-01-01' AND ts < timestamp '{:04}-01-01'", y + 1);
    let hits = g.index.hits(&g.index.ids(&cond, None).await?).await?;
    Ok(hits.into_iter().find(|h| h.get("title").and_then(Value::as_str).is_some_and(|t| norm_title(t) == title)).and_then(|h| h.get("id")?.as_str().map(str::to_string)))
}

impl Papers {
    /// Take every item that moved since the cursor into the served generation.
    pub async fn sync(&self, hub: &Hub) -> Result<Value> {
        let g = self.generation().await.map_err(|e| anyhow::anyhow!(e.0))?;
        let dir = self.root.join(&g.name);
        let mut cursor = Cursor::load(&dir);
        let items = moved(hub, &cursor.versions).await.context("zotero items")?;
        let embedder = g.embedder.clone().context("the generation has no model to embed held items with")?;
        let (mut joined, mut held, mut rows) = (0, 0, Vec::new());
        for item in &items {
            let Some(keys) = item_keys(item) else { continue };
            match matching(&g, keys).await? {
                Some(id) => {
                    let Some(mut doc) = g.index.document(&id, "true").await? else { continue };
                    let mut have: Vec<Value> = doc.get("collection").and_then(Value::as_array).cloned().unwrap_or_default();
                    if have.iter().any(|c| c == LIBRARY) {
                        continue;
                    }
                    have.push(json!(LIBRARY));
                    doc.insert("collection".into(), Value::Array(have));
                    rows.push(doc);
                    joined += 1;
                }
                None => {
                    held += 1;
                    rows.push(held_row(item));
                }
            }
        }
        let written = g.index.upsert(&rows, Some(embedder.as_ref())).await?;
        if let Some(e) = written.error {
            anyhow::bail!("the moved items were not embedded: {e}");
        }
        cursor.take(&items);
        cursor.synced = Some(now());
        cursor.save(&dir)?;
        Ok(json!({"items": items.len(), "joined": joined, "held": held, "embedded": written.embedded, "generation": g.name}))
    }
}

async fn sync_due(papers: &Papers) -> bool {
    match papers.generation().await {
        Ok(g) => Cursor::load(&papers.root.join(&g.name)).due(now()),
        Err(_) => false,
    }
}

async fn run_once(ctx: &Context, papers: &Papers) -> Result<()> {
    let _one = SYNCING.lock().await;
    let started = std::time::Instant::now();
    let out = papers.sync(ctx.hub()).await;
    ctx.usage("papers-sync", out.is_ok(), started.elapsed().as_millis() as u64);
    tracing::info!("papers: zotero sync: {}", out?);
    Ok(())
}

/// Whether zotero holds an item past the served cursor, waiting up to [`WAIT_S`] for one.
async fn moved_past(hub: &Hub, papers: &Papers) -> Result<bool> {
    let g = papers.generation().await.map_err(|e| anyhow::anyhow!(e.0))?;
    let since = Cursor::load(&papers.root.join(&g.name)).versions;
    let opts = CallOpts { peer: zotero_peer(), timeout: Duration::from_secs(WAIT_S + 30), ..CallOpts::default() };
    let reply = page(hub.call_with("zotero", "items", json!({"since": since, "limit": 1, "wait": WAIT_S}), &opts).await?);
    Ok(reply["total"].as_u64().unwrap_or(0) > 0)
}

/// Sync each time zotero answers the long poll with a moved item.
async fn watch(ctx: Context, papers: Arc<Papers>) {
    loop {
        let step = match moved_past(ctx.hub(), &papers).await {
            Ok(true) => run_once(&ctx, &papers).await,
            moved => moved.map(drop),
        };
        if let Err(e) = step {
            tracing::info!("papers: zotero watch, retrying in {}s: {e:#}", RETRY.as_secs());
            tokio::time::sleep(RETRY).await;
        }
    }
}

/// Watch zotero for moved items, and sync when a session stops and the last sync is past the gap.
pub async fn follow(ctx: Context, papers: Arc<Papers>) {
    tokio::spawn(watch(ctx.clone(), papers.clone()));
    let hub = ctx.hub().clone();
    let connect = move || {
        let hub = hub.clone();
        async move { hub.subscribe("reflection", "sessions", None, None).await }
    };
    let handler = move |event: Value| {
        let (ctx, papers) = (ctx.clone(), papers.clone());
        async move {
            if event.get("event").and_then(Value::as_str) == Some("stopped") && sync_due(&papers).await {
                if let Err(e) = run_once(&ctx, &papers).await {
                    tracing::warn!("papers: zotero sync failed: {e:#}");
                }
            }
            Ok(())
        }
    };
    SupervisedSubscription::spawn("papers-sync", SubscriptionOpts::default(), connect, handler);
}
