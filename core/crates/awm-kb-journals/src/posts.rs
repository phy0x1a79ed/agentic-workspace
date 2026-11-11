//! Posts as rows of the `journals` source, and the reads and writes over them.
//!
//! A row holds the post whole: its channel, author, body, `meta` text as stored, time, tag ids,
//! citations, reply and thread links and version. The search fields come with it: a thread's
//! first post with a body carries its title and the thread's text (its body, then each
//! comment's), so a hit on a comment finds the post it answers. A comment's search fields stay
//! empty, so it has no chunks of its own.

use std::collections::{BTreeMap, HashMap, HashSet};

use awm_index::cells::{self, Cell, Row};
use awm_index::sql::{id_list, quote};
use awm_index::{Class, Field, Fields, Kind, Spec};
use awm_pycompat::pystr::{head, splitlines, strip};
use awm_pycompat::time::ms_to_iso;
use futures_util::TryStreamExt;
use lancedb::arrow::arrow_array::RecordBatch;
use lancedb::query::{ExecutableQuery, QueryBase, Select};
use serde_json::{json, Map, Value};

use crate::{Journals, Result};

pub const SOURCE: &str = "journals";
const TITLE_CHARS: usize = 120;
const CHANNEL: &str = "project:";
const BATCH: usize = 500;

/// The `journals` source: its searchable fields, its hit fields and the columns kept whole.
pub fn spec() -> Spec {
    Spec {
        name: SOURCE.into(),
        fields: fields(),
        hit: vec!["title".into(), "ts".into()],
        extra: vec![
            ("owner_project".into(), Kind::Text),
            ("owner_scope".into(), Kind::Text),
            ("author".into(), Kind::Text),
            ("body".into(), Kind::Text),
            ("meta".into(), Kind::Text),
            ("ms".into(), Kind::Int),
            ("seq".into(), Kind::Int),
            ("reply_to".into(), Kind::Text),
            ("thread".into(), Kind::Text),
            ("version".into(), Kind::Int),
            ("session".into(), Kind::Text),
        ],
        section_by: Some("ts".into()),
        ..Default::default()
    }
}

/// The archive's field table, unchanged: `tag` holds tag ids, `replies_to` a journal's citations.
pub fn fields() -> Fields {
    Fields(vec![
        Field::new("title", Class::Content),
        Field::new("text", Class::Content),
        Field::new("ts", Class::Datetime),
        Field::new("project", Class::Categorical),
        Field::nested("scope", "/"),
        Field::new("tag", Class::Categorical),
        Field::new("replies_to", Class::Categorical),
    ])
}

const COLS: [&str; 13] =
    ["id", "owner_project", "owner_scope", "author", "body", "meta", "ms", "seq", "reply_to", "thread", "version", "tag", "replies_to"];

/// One post as the journal reads it.
#[derive(Debug, Clone, PartialEq)]
pub struct Post {
    pub id: String,
    pub project: String,
    pub scope: String,
    /// The stored author ref.
    pub author: String,
    pub body: String,
    /// `meta` as stored: `json.dumps` text.
    pub meta_raw: String,
    pub ms: i64,
    /// The write order, which breaks a tie between posts of one millisecond.
    pub seq: i64,
    pub reply_to: Option<String>,
    pub thread: Option<String>,
    pub cites: Vec<String>,
    pub tag_ids: Vec<String>,
    pub version: i64,
    /// Search hits and thread reads only.
    pub comments: Option<i64>,
    /// Search hits only: `{rank, snippet}`.
    pub matched: Option<Value>,
}

/// `channel._coerce_meta`: a dict, a JSON object's text, or a double-encoded one; else `{}`.
pub fn coerce_meta(raw: &Value) -> Map<String, Value> {
    match raw {
        Value::Object(m) => m.clone(),
        Value::String(s) => coerce_meta_text(s),
        _ => Map::new(),
    }
}

pub fn coerce_meta_text(s: &str) -> Map<String, Value> {
    if s.is_empty() {
        return Map::new();
    }
    match serde_json::from_str::<Value>(s) {
        Ok(Value::Object(m)) => m,
        Ok(Value::String(inner)) => match serde_json::from_str::<Value>(&inner) {
            Ok(Value::Object(m)) => m,
            _ => Map::new(),
        },
        _ => Map::new(),
    }
}

/// `_author_to_display`.
pub fn author_display(stored: &str) -> String {
    if stored.is_empty() || stored == "system" {
        return "system".into();
    }
    if stored.starts_with("agent:") || stored.starts_with("user:") {
        return stored.into();
    }
    if let Some(rest) = stored.strip_prefix("scope:") {
        return format!("agent:{rest}");
    }
    if stored.contains('/') {
        return format!("agent:{stored}");
    }
    stored.into()
}

/// `_author_to_stored`.
pub fn author_stored(display: &str) -> String {
    if display.is_empty() || display == "system" {
        return "system".into();
    }
    if display.starts_with("agent:") || display.starts_with("user:") {
        return display.into();
    }
    if let Some(rest) = display.strip_prefix("scope:") {
        return format!("agent:{rest}");
    }
    if display.contains('/') {
        return format!("agent:{display}");
    }
    format!("user:{display}")
}

/// The tag registry's names by id, for showing a post's tags.
pub type Names = HashMap<String, String>;

impl Post {
    pub fn meta(&self) -> Map<String, Value> {
        coerce_meta_text(&self.meta_raw)
    }

    pub fn root(&self) -> &str {
        self.thread.as_deref().unwrap_or(&self.id)
    }

    pub fn ts(&self) -> String {
        ms_to_iso(self.ms).unwrap_or_default()
    }

    pub fn has(&self, tag_id: &str) -> bool {
        self.tag_ids.iter().any(|t| t == tag_id)
    }

    /// The post's tag ids that the registry holds, ordered by name.
    pub fn ordered_tags(&self, names: &Names) -> Vec<String> {
        let mut ids: Vec<&String> = self.tag_ids.iter().filter(|t| names.contains_key(*t)).collect();
        ids.sort_by_key(|t| names[*t].to_lowercase());
        ids.dedup();
        ids.into_iter().cloned().collect()
    }

    pub fn tag_names(&self, names: &Names) -> Vec<String> {
        self.ordered_tags(names).iter().map(|t| names[t].clone()).collect()
    }

    /// `ScopePost.to_dict`.
    pub fn to_dict(&self, names: &Names) -> Value {
        let mut d = Map::new();
        d.insert("id".into(), json!(self.id));
        d.insert("project".into(), json!(self.project));
        d.insert("scope".into(), json!(self.scope));
        d.insert("author".into(), json!(author_display(&self.author)));
        d.insert("tags".into(), json!(self.tag_names(names)));
        d.insert("body".into(), json!(self.body));
        d.insert("meta".into(), Value::Object(self.meta()));
        d.insert("ts".into(), json!(self.ts()));
        if let Some(r) = &self.reply_to {
            d.insert("reply_to".into(), json!([r]));
            d.insert("thread".into(), json!(self.thread));
        } else if !self.cites.is_empty() {
            d.insert("reply_to".into(), json!(self.cites));
        }
        if let Some(n) = self.comments {
            d.insert("comments".into(), json!(n));
        }
        if self.version > 1 {
            d.insert("version".into(), json!(self.version));
        }
        if let Some(m) = &self.matched {
            d.insert("match".into(), m.clone());
        }
        Value::Object(d)
    }

    fn from_row(r: &Row) -> Post {
        let text = |n: &str| cells::text(r, n);
        let opt = |n: &str| r.get(n).and_then(Cell::as_str).map(str::to_string);
        let int = |n: &str| match r.get(n) {
            Some(Cell::Int(i)) => *i,
            _ => 0,
        };
        let list = |n: &str| match r.get(n) {
            Some(Cell::Texts(v)) => v.clone(),
            _ => vec![],
        };
        Post {
            id: text("id"),
            project: text("owner_project"),
            scope: text("owner_scope"),
            author: text("author"),
            body: text("body"),
            meta_raw: text("meta"),
            ms: int("ms"),
            seq: int("seq"),
            reply_to: opt("reply_to"),
            thread: opt("thread"),
            cites: list("replies_to"),
            tag_ids: list("tag"),
            version: int("version").max(1),
            comments: None,
            matched: None,
        }
    }

    /// The search archive's project and scope of a channel: a project channel names its
    /// project and no scope.
    pub fn search_owner(&self) -> (Vec<String>, Vec<String>) {
        if self.project.is_empty() {
            if let Some(p) = self.scope.strip_prefix(CHANNEL) {
                return (vec![p.to_string()], vec![]);
            }
        }
        let p = if self.project.is_empty() { vec![] } else { vec![self.project.clone()] };
        let s = if self.scope.is_empty() { vec![] } else { vec![self.scope.clone()] };
        (p, s)
    }

    /// The row this post writes, with the search title and text it carries.
    fn row(&self, title: &str, text: &str) -> Map<String, Value> {
        let (project, scope) = self.search_owner();
        let session = self.meta().get("session").and_then(Value::as_str).map(str::to_string);
        let v = json!({
            "id": self.id,
            "title": title,
            "text": text,
            "ts": self.ts(),
            "project": project,
            "scope": scope,
            "tag": self.tag_ids,
            "replies_to": self.cites,
            "owner_project": self.project,
            "owner_scope": self.scope,
            "author": self.author,
            "body": self.body,
            "meta": self.meta_raw,
            "ms": self.ms,
            "seq": self.seq,
            "reply_to": self.reply_to,
            "thread": self.thread,
            "version": self.version,
            "session": session,
        });
        v.as_object().cloned().unwrap_or_default()
    }
}

/// `feed._title`: `meta.title`, else the body's first non-blank line, cut to 120 characters.
pub fn title_of(meta: &Map<String, Value>, body: &str) -> String {
    if let Some(Value::String(t)) = meta.get("title") {
        if !strip(t).is_empty() {
            return strip(t).to_string();
        }
    }
    let first = splitlines(body, false).into_iter().map(strip).find(|l| !l.is_empty()).unwrap_or("");
    head(first, TITLE_CHARS).to_string()
}

/// `indexed.thread_text`: the first post, then each comment's text, oldest first.
pub fn thread_text(root: &Post, comments: &[Post]) -> String {
    let mut parts = vec![root.body.as_str()];
    let mut sorted: Vec<&Post> = comments.iter().filter(|c| !c.body.is_empty()).collect();
    sorted.sort_by(|a, b| (a.ms, &a.id).cmp(&(b.ms, &b.id)));
    parts.extend(sorted.iter().map(|c| c.body.as_str()));
    parts.into_iter().filter(|p| !p.is_empty()).collect::<Vec<_>>().join("\n\n")
}

/// A Lance condition `col = 'value'`.
pub fn eq(col: &str, value: &str) -> String {
    format!("{col} = {}", quote(value))
}

pub fn has_tag(tag_id: &str) -> String {
    format!("array_has(tag, {})", quote(tag_id))
}

/// `a AND b AND ...`, leaving out `true`.
pub fn and(parts: &[String]) -> String {
    let kept: Vec<&str> = parts.iter().map(String::as_str).filter(|p| !p.is_empty() && *p != "true").collect();
    match kept.len() {
        0 => "true".into(),
        1 => kept[0].to_string(),
        _ => kept.iter().map(|p| format!("({p})")).collect::<Vec<_>>().join(" AND "),
    }
}

impl Journals {
    pub(crate) async fn max_seq(&self) -> Result<i64> {
        let mut most = 0;
        for sec in self.index.sections() {
            let batches: Vec<RecordBatch> =
                sec.entries.query().select(Select::columns(&["seq"])).execute().await?.try_collect().await?;
            for r in cells::rows(&batches) {
                if let Some(Cell::Int(i)) = r.get("seq") {
                    most = most.max(*i);
                }
            }
        }
        Ok(most)
    }

    /// Every post that passes the Lance condition `cond`, in no order.
    pub async fn scan(&self, cond: &str) -> Result<Vec<Post>> {
        let mut out = Vec::new();
        for sec in self.index.sections() {
            let mut q = sec.entries.query().select(Select::columns(&COLS));
            if cond != "true" {
                q = q.only_if(cond);
            }
            let batches: Vec<RecordBatch> = q.execute().await?.try_collect().await?;
            out.extend(cells::rows(&batches).iter().map(Post::from_row));
        }
        Ok(out)
    }

    pub async fn get_post(&self, id: &str) -> Result<Option<Post>> {
        Ok(self.scan(&eq("id", id)).await?.into_iter().next())
    }

    /// The posts with these ids, by id.
    pub async fn posts_by_id(&self, ids: &[String]) -> Result<HashMap<String, Post>> {
        let mut out = HashMap::new();
        for batch in ids.chunks(BATCH) {
            for p in self.scan(&id_list(batch)).await? {
                out.insert(p.id.clone(), p);
            }
        }
        Ok(out)
    }

    /// Every comment of a thread, oldest first.
    pub async fn thread_posts(&self, root: &str) -> Result<Vec<Post>> {
        let mut out = self.scan(&eq("thread", root)).await?;
        out.sort_by(|a, b| (a.ms, &a.id).cmp(&(b.ms, &b.id)));
        Ok(out)
    }

    /// How many comments each thread holds.
    pub async fn comment_counts(&self, roots: &[String]) -> Result<HashMap<String, i64>> {
        let mut out: HashMap<String, i64> = roots.iter().map(|r| (r.clone(), 0)).collect();
        for batch in roots.chunks(BATCH) {
            let cond = format!("thread IN ({})", batch.iter().map(|r| quote(r)).collect::<Vec<_>>().join(", "));
            for p in self.scan(&cond).await? {
                if let Some(t) = &p.thread {
                    *out.entry(t.clone()).or_default() += 1;
                }
            }
        }
        Ok(out)
    }

    /// Write `posts` whole, each merged on its id, with the thread text of every first post
    /// they touch. Their chunks follow in the same call; without the model they wait.
    pub(crate) async fn write_posts(&self, posts: &[Post]) -> Result<()> {
        let mut batch: BTreeMap<String, Post> = posts.iter().map(|p| (p.id.clone(), p.clone())).collect();
        let roots: HashSet<String> = posts.iter().map(|p| p.root().to_string()).collect();
        let mut rows = Vec::new();
        let mut done = HashSet::new();
        for root_id in &roots {
            let root = match batch.get(root_id) {
                Some(r) => Some(r.clone()),
                None => self.get_post(root_id).await?,
            };
            let Some(root) = root else { continue };
            let mut comments: HashMap<String, Post> =
                self.scan(&eq("thread", root_id)).await?.into_iter().map(|c| (c.id.clone(), c)).collect();
            for p in batch.values().filter(|p| p.thread.as_deref() == Some(root_id.as_str())) {
                comments.insert(p.id.clone(), p.clone());
            }
            let comments: Vec<Post> = comments.into_values().collect();
            rows.push(Self::search_row(&root, &comments));
            done.insert(root.id.clone());
            batch.entry(root.id.clone()).or_insert(root);
        }
        for p in posts {
            if !done.contains(&p.id) {
                rows.push(Self::search_row(p, &[]));
            }
        }
        let emb = self.embedder();
        let written = self.index.upsert(&rows, emb.as_deref()).await?;
        if let Some(e) = written.error {
            tracing::warn!("journal: the model failed, {} posts wait for it: {e}", written.waiting);
        }
        Ok(())
    }

    /// A post's row: a first post with a body carries its title and the thread's text.
    pub(crate) fn search_row(p: &Post, comments: &[Post]) -> Map<String, Value> {
        if p.body.is_empty() {
            return p.row("", "");
        }
        let title = title_of(&p.meta(), &p.body);
        if p.thread.is_some() {
            return p.row(&title, &p.body);
        }
        p.row(&title, &thread_text(p, comments))
    }
}
