//! The cutover from `scope_posts` to the `journals` source, the startup reconcile, and the
//! export-back that a rollback runs.
//!
//! The first start copies every post of the frozen tables into LanceDB, merged on id, and
//! refuses to serve until the source holds each of them and, with the model loaded, every post
//! has its chunks. A marker file in the source's directory records that the cutover passed; no
//! later start copies again, so the frozen tables never overwrite a newer post.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;

use awm_index::cells::{self, Cell};
use awm_pycompat::time::now_ms;
use awm_store::rusqlite::{params, Connection};
use futures_util::TryStreamExt;
use lancedb::arrow::arrow_array::RecordBatch;
use lancedb::query::{ExecutableQuery, QueryBase, Select};
use serde_json::{json, Map, Value};

use crate::posts::Post;
use crate::{Fail, Journals, Opts, Result};

const MARKER: &str = "cutover.json";
const BATCH: usize = 500;

/// What the cutover found and did.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Cutover {
    /// When the cutover passed, in ms.
    pub at_ms: i64,
    /// The posts `scope_posts` held then.
    pub posts: usize,
    /// Posts copied by this start; 0 after the first.
    pub copied: usize,
}

impl Cutover {
    pub fn to_json(&self) -> Value {
        json!({"at_ms": self.at_ms, "posts": self.posts, "copied": self.copied})
    }
}

/// Every post of the frozen tables, each with its tags and its citations.
fn frozen_posts(c: &Connection) -> Result<Vec<Post>> {
    let mut tags: HashMap<String, Vec<String>> = HashMap::new();
    let mut stmt = c.prepare("SELECT post_id, tag_id FROM post_tags ORDER BY rowid")?;
    for r in stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))? {
        let (p, t) = r?;
        tags.entry(p).or_default().push(t);
    }
    let mut cites: HashMap<String, Vec<String>> = HashMap::new();
    let mut stmt = c.prepare("SELECT post_id, cited_id FROM post_cites ORDER BY post_id, ord")?;
    for r in stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))? {
        let (p, t) = r?;
        cites.entry(p).or_default().push(t);
    }
    let mut stmt = c.prepare(
        "SELECT rowid, id, owner_project, owner_scope, author, body, meta, ts, reply_to, thread_id, version FROM scope_posts ORDER BY rowid",
    )?;
    let rows = stmt
        .query_map([], |r| {
            Ok(Post {
                seq: r.get(0)?,
                id: r.get(1)?,
                project: r.get(2)?,
                scope: r.get(3)?,
                author: r.get(4)?,
                body: r.get::<_, Option<String>>(5)?.unwrap_or_default(),
                meta_raw: r.get::<_, Option<String>>(6)?.unwrap_or_default(),
                ms: r.get(7)?,
                reply_to: r.get(8)?,
                thread: r.get(9)?,
                version: r.get::<_, Option<i64>>(10)?.unwrap_or(1).max(1),
                cites: vec![],
                tag_ids: vec![],
                comments: None,
                matched: None,
            })
        })?
        .collect::<std::result::Result<Vec<Post>, _>>()?;
    Ok(rows
        .into_iter()
        .map(|mut p| {
            p.tag_ids = tags.remove(&p.id).unwrap_or_default();
            p.cites = cites.remove(&p.id).unwrap_or_default();
            p
        })
        .collect())
}

/// Each post's row with its thread's text, computed from one snapshot of every post.
fn rows_of(all: &[Post], only: Option<&HashSet<String>>) -> Vec<Map<String, Value>> {
    let mut comments: HashMap<&str, Vec<Post>> = HashMap::new();
    for p in all {
        if let Some(t) = &p.thread {
            comments.entry(t.as_str()).or_default().push(p.clone());
        }
    }
    all.iter()
        .filter(|p| only.is_none_or(|o| o.contains(&p.id)))
        .map(|p| Journals::search_row(p, comments.get(p.id.as_str()).map(Vec::as_slice).unwrap_or(&[])))
        .collect()
}

impl Journals {
    fn marker(&self) -> std::path::PathBuf {
        Self::lance_path(&self.awm_dir).join(MARKER)
    }

    /// Copy `scope_posts` into the source once, then check the gate.
    pub(crate) async fn run_cutover(&self) -> Result<Cutover> {
        if let Ok(text) = std::fs::read_to_string(self.marker()) {
            let v: Value = serde_json::from_str(&text).unwrap_or_default();
            return Ok(Cutover { at_ms: v["at_ms"].as_i64().unwrap_or(0), posts: v["posts"].as_u64().unwrap_or(0) as usize, copied: 0 });
        }
        let posts = self.db.blocking_read(frozen_posts)?;
        let rows = rows_of(&posts, None);
        let emb = self.embedder();
        for part in rows.chunks(BATCH) {
            self.index.upsert(part, emb.as_deref()).await?;
        }
        let ids: Vec<String> = posts.iter().map(|p| p.id.clone()).collect();
        let held = self.posts_by_id(&ids).await?.len();
        if held != posts.len() {
            return Err(Fail::runtime(format!(
                "cutover gate: the journals source holds {held} of the {} posts in scope_posts; refusing to serve",
                posts.len()
            )));
        }
        if emb.is_some() {
            let waiting = self.index.count(Some("_chunked IS NULL")).await?;
            if waiting > 0 {
                return Err(Fail::runtime(format!("cutover gate: {waiting} posts have no chunks yet; refusing to serve")));
            }
        }
        let max = posts.iter().map(|p| p.seq).max().unwrap_or(0);
        self.seq.fetch_max(max, std::sync::atomic::Ordering::SeqCst);
        let cut = Cutover { at_ms: now_ms(), posts: posts.len(), copied: posts.len() };
        std::fs::write(self.marker(), json!({"at_ms": cut.at_ms, "posts": cut.posts}).to_string())?;
        tracing::info!("journal: cutover copied {} posts into the journals source", cut.copied);
        Ok(cut)
    }

    /// Apply each edit whose version row is ahead of its post, and rebuild each thread text
    /// that misses a comment. Chunks that wait for the model follow when it loads.
    pub async fn reconcile(&self) -> Result<Value> {
        let newest: Vec<(String, i64, String)> = self.db.blocking_read(|c| {
            let mut stmt = c.prepare(
                "SELECT v.post_id, v.version, v.body FROM post_versions v \
                 JOIN (SELECT post_id, MAX(version) AS m FROM post_versions GROUP BY post_id) n \
                 ON n.post_id = v.post_id AND n.m = v.version",
            )?;
            let rows = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?.collect::<std::result::Result<Vec<_>, _>>()?;
            Ok::<_, Fail>(rows)
        })?;
        let ids: Vec<String> = newest.iter().map(|r| r.0.clone()).collect();
        let held = self.posts_by_id(&ids).await?;
        let mut ahead = Vec::new();
        for (id, version, body) in newest {
            if let Some(p) = held.get(&id).filter(|p| p.version < version) {
                let mut p = p.clone();
                p.version = version;
                p.body = body;
                ahead.push(p);
            }
        }
        let _w = self.write.lock().await;
        if !ahead.is_empty() {
            tracing::warn!("journal: reconcile applies {} edits whose post rows were behind", ahead.len());
            self.write_posts(&ahead).await?;
        }
        let all = self.scan("true").await?;
        let mut stored: BTreeMap<String, (String, String)> = BTreeMap::new();
        for sec in self.index.sections() {
            let batches: Vec<RecordBatch> =
                sec.entries.query().select(Select::columns(&["id", "title", "text"])).execute().await?.try_collect().await?;
            for r in cells::rows(&batches) {
                let get = |k: &str| r.get(k).and_then(Cell::as_str).unwrap_or("").to_string();
                stored.insert(get("id"), (get("title"), get("text")));
            }
        }
        let stale: Vec<Map<String, Value>> = rows_of(&all, None)
            .into_iter()
            .filter(|row| {
                let id = row["id"].as_str().unwrap_or("");
                let want = (row["title"].as_str().unwrap_or("").to_string(), row["text"].as_str().unwrap_or("").to_string());
                stored.get(id) != Some(&want)
            })
            .collect();
        if !stale.is_empty() {
            tracing::warn!("journal: reconcile rebuilds {} thread texts", stale.len());
            let emb = self.embedder();
            for part in stale.chunks(BATCH) {
                self.index.upsert(part, emb.as_deref()).await?;
            }
        }
        Ok(json!({"edits": ahead.len(), "threads": stale.len()}))
    }

    /// Copy every post the source holds and the frozen tables lack, or hold otherwise, into the
    /// frozen tables: the rollback path, so the Python journal reads every post again.
    pub async fn export_back(&self) -> Result<Value> {
        let posts = self.scan("true").await?;
        let frozen: HashMap<String, Post> = self.db.blocking_read(frozen_posts)?.into_iter().map(|p| (p.id.clone(), p)).collect();
        let same = |a: &Post, b: &Post| {
            let set = |v: &[String]| v.iter().cloned().collect::<HashSet<_>>();
            (&a.project, &a.scope, &a.author, &a.body, &a.meta_raw, a.ms, &a.reply_to, &a.thread, a.version, &a.cites)
                == (&b.project, &b.scope, &b.author, &b.body, &b.meta_raw, b.ms, &b.reply_to, &b.thread, b.version, &b.cites)
                && set(&a.tag_ids) == set(&b.tag_ids)
        };
        let mut changed: Vec<Post> = posts.iter().filter(|p| !frozen.get(&p.id).is_some_and(|f| same(p, f))).cloned().collect();
        changed.sort_by_key(|p| (p.ms, p.seq));
        let n = changed.len();
        self.db.blocking_write(move |tx| {
            for p in &changed {
                tx.execute(
                    "INSERT INTO scope_posts (id, owner_project, owner_scope, author, body, meta, ts, reply_to, thread_id, version) \
                     VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET owner_project=excluded.owner_project, \
                     owner_scope=excluded.owner_scope, author=excluded.author, body=excluded.body, meta=excluded.meta, ts=excluded.ts, \
                     reply_to=excluded.reply_to, thread_id=excluded.thread_id, version=excluded.version",
                    params![p.id, p.project, p.scope, p.author, p.body, p.meta_raw, p.ms, p.reply_to, p.thread, p.version],
                )?;
                tx.execute("DELETE FROM post_tags WHERE post_id=?", [&p.id])?;
                for t in &p.tag_ids {
                    tx.execute("INSERT OR IGNORE INTO post_tags (post_id, tag_id) VALUES (?, ?)", params![p.id, t])?;
                }
                tx.execute("DELETE FROM post_cites WHERE post_id=?", [&p.id])?;
                for (i, c) in p.cites.iter().enumerate() {
                    tx.execute("INSERT OR IGNORE INTO post_cites (post_id, cited_id, ord) VALUES (?, ?, ?)", params![p.id, c, i as i64])?;
                }
            }
            Ok::<_, Fail>(())
        })?;
        Ok(json!({"posts": posts.len(), "exported": n, "unchanged": posts.len() - n}))
    }
}

/// The rollback tool: open the node's journal and copy every post back into the frozen tables.
pub async fn export_back(awm_dir: &Path, node: &str) -> anyhow::Result<Value> {
    let j = Journals::open(awm_dir, None, Opts::for_node(awm_dir, node)).await?;
    Ok(j.export_back().await?)
}

/// The cutover's dry run: copy a snapshot of journal.db into a scratch state dir, run the
/// cutover there and report its gate. The snapshot itself is not written.
pub async fn dry_run(snapshot: &Path, scratch: &Path, embedder: Option<std::sync::Arc<dyn awm_embed::Embedder>>) -> anyhow::Result<Value> {
    let db = Journals::db_path(scratch);
    if let Some(dir) = db.parent() {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::copy(snapshot, &db)?;
    let j = Journals::open(scratch, embedder, Opts { node: String::new(), other_dbs: vec![] }).await?;
    let posts = j.index.count(None).await?;
    let waiting = j.index.count(Some("_chunked IS NULL")).await?;
    Ok(json!({"scope_posts": j.cutover.posts, "lance_posts": posts, "waiting": waiting, "gate": "passed"}))
}
