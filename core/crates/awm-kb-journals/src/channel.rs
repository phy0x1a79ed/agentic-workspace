//! The scope channel: posting, fetching and searching posts, and threads.
//!
//! A comment names one post: it lives in the channel of its thread's first post, wherever the
//! commenter sits. A debrief journal names every earlier journal it built on: its citations,
//! and it stays in its writer's channel. Both links point one way.

use std::collections::{HashMap, HashSet};

use awm_index::grammar::{parse_source, parse_text};
use awm_index::Veil;
use awm_plugin_sdk::privacy;
use awm_pycompat::pystr::{lower, split};
use awm_pycompat::repr::str_repr;
use awm_pycompat::time::{iso_to_ms, now_ms};
use serde_json::{json, Map, Value};

use crate::caller::Exclusion;
use crate::posts::{author_stored, coerce_meta, eq, and, Names, Post};
use crate::schema::{FOLDED, INDEXED_TAGS, JOURNAL, KIND_TAGS, MESSAGE, MIGRATED_PREFIX, TYPE_TAGS};
use crate::tags::{names_arg, parse_filter, Filter};
use crate::{py_str, Fail, Journals, Result};

/// The most hits a search asks for.
pub const SEARCH_MAX: usize = 100;
/// Characters of a post's body around the first match, as the hit's snippet.
pub const SNIPPET_CHARS: usize = 240;

/// One new post.
#[derive(Debug, Clone, Default)]
pub struct NewPost {
    pub project: String,
    pub scope: String,
    pub author: String,
    pub body: String,
    pub tags: Option<Value>,
    pub meta: Value,
    pub reply_to: Vec<String>,
    /// The edge-stamped peer identity, stored as the author.
    pub origin: Option<String>,
    /// Tag ids the journal's own code adds.
    pub extra_tags: Vec<String>,
    pub ts: Option<i64>,
}

/// What a fetch narrows to.
#[derive(Default)]
pub struct Query {
    pub project: Option<String>,
    pub scope: Option<String>,
    pub query: Option<String>,
    pub author: Option<String>,
    pub limit: usize,
    pub offset: usize,
    pub before_ts: Option<String>,
    pub order: Option<String>,
    pub exclude: Option<Exclusion>,
    pub tags: Option<Value>,
}

impl Query {
    pub fn new() -> Query {
        Query { limit: 50, ..Query::default() }
    }
}

/// `reply_targets`: `reply_to` as a list of post ids.
pub fn reply_targets(raw: Option<&Value>) -> Result<Vec<String>> {
    let items: Vec<&Value> = match raw {
        None | Some(Value::Null) => return Ok(vec![]),
        Some(Value::String(s)) if s.is_empty() => return Ok(vec![]),
        Some(Value::Array(a)) if a.is_empty() => return Ok(vec![]),
        Some(v @ Value::String(_)) => vec![v],
        Some(Value::Array(a)) => a.iter().collect(),
        Some(_) => return Err(Fail::value("reply_to is a post id or a list of post ids")),
    };
    let mut out: Vec<String> = Vec::new();
    for x in items {
        match x {
            Value::String(s) if !s.is_empty() => {
                if !out.contains(s) {
                    out.push(s.clone());
                }
            }
            _ => return Err(Fail::value("reply_to is a post id or a list of post ids")),
        }
    }
    Ok(out)
}

/// `_snippet`: the body around the query's first occurrence, else around its first word that
/// occurs, else its start.
pub fn snippet(body: &str, query: &str) -> String {
    let low: Vec<char> = lower(body).chars().collect();
    let low_s: String = low.iter().collect();
    let mut words = vec![awm_pycompat::pystr::strip(query).to_string()];
    words.extend(split(query).into_iter().map(str::to_string));
    let at = words
        .iter()
        .filter(|w| !w.is_empty())
        .filter_map(|w| low_s.find(&lower(w)).map(|b| low_s[..b].chars().count()))
        .next()
        .unwrap_or(0);
    let start = at.saturating_sub(SNIPPET_CHARS / 4);
    let piece: String = body.chars().skip(start).take(SNIPPET_CHARS).collect();
    split(&piece).join(" ")
}

/// ASCII-case-insensitive containment, as SQLite's `LIKE '%q%'` matches.
fn like(hay: &str, needle: &str) -> bool {
    hay.to_ascii_lowercase().contains(&needle.to_ascii_lowercase())
}

/// The thread tree under `top`: each comment a post dict with its own `replies`.
pub fn nest(top: &Post, comments: &[Post], visible: &dyn Fn(&Post) -> bool, names: &Names) -> Vec<Value> {
    let mut children: HashMap<&str, Vec<&Post>> = HashMap::new();
    for c in comments {
        children.entry(c.reply_to.as_deref().unwrap_or("")).or_default().push(c);
    }
    fn walk(pid: &str, seen: &mut HashSet<String>, children: &HashMap<&str, Vec<&Post>>, visible: &dyn Fn(&Post) -> bool, names: &Names) -> Vec<Value> {
        let mut out = Vec::new();
        for c in children.get(pid).map(Vec::as_slice).unwrap_or(&[]) {
            if seen.contains(&c.id) || !visible(c) {
                continue;
            }
            seen.insert(c.id.clone());
            let mut d = c.to_dict(names);
            d["replies"] = Value::Array(walk(&c.id, seen, children, visible, names));
            out.push(d);
        }
        out
    }
    let mut seen = HashSet::from([top.id.clone()]);
    walk(&top.id, &mut seen, &children, visible, names)
}

/// How many posts a thread tree holds.
pub fn count(tree: &[Value]) -> i64 {
    tree.iter().map(|c| 1 + count(c["replies"].as_array().map(Vec::as_slice).unwrap_or(&[]))).sum()
}

fn degraded(error: &str) -> Value {
    let text = format!("RuntimeError({})", str_repr(error));
    json!({"semantic": "error", "error": awm_pycompat::pystr::head(&text, 300), "fallback": "keyword"})
}

impl Journals {
    /// `post_tag_ids`: the tag ids a new post carries. A post that names no type tag is a message.
    pub fn post_tag_ids(&self, tags: Option<&Value>, extra: &[String]) -> Result<Vec<String>> {
        let mut ids = self.resolve_tags(&names_arg(tags)?)?;
        for t in extra {
            if !ids.contains(t) {
                ids.push(t.clone());
            }
        }
        if !ids.iter().any(|t| TYPE_TAGS.contains(&t.as_str())) {
            ids.push(MESSAGE.into());
        }
        Ok(ids)
    }

    /// `require_journals`: `targets` when each is a journal post.
    async fn require_journals(&self, targets: &[String]) -> Result<Vec<String>> {
        let found = self.posts_by_id(targets).await?;
        for t in targets {
            if !found.get(t).is_some_and(|p| p.has(JOURNAL)) {
                return Err(Fail::value(format!(
                    "citation {t} is not a journal post. Journal for that session first, then cite the new journal (sessions.md R17)"
                )));
            }
        }
        Ok(targets.to_vec())
    }

    /// Append a post to a channel and publish it on the `posts` topic.
    pub async fn post(&self, new: NewPost) -> Result<Post> {
        let _w = self.write.lock().await;
        self.post_locked(new).await
    }

    pub(crate) async fn post_locked(&self, new: NewPost) -> Result<Post> {
        if new.origin.is_none() && new.author.starts_with("peer:") {
            return Err(Fail::value("author 'peer:...' is reserved for edge-stamped peer identities"));
        }
        let tag_ids = self.post_tag_ids(new.tags.as_ref(), &new.extra_tags)?;
        let targets = new.reply_to.clone();
        let (mut project, mut scope) = (new.project.clone(), new.scope.clone());
        let mut cites = Vec::new();
        let mut reply_to = None;
        if tag_ids.iter().any(|t| t == JOURNAL) {
            cites = self.require_journals(&targets).await?;
        } else if targets.len() > 1 {
            return Err(Fail::value("a comment answers one post; only a debrief journal lists several"));
        } else if let Some(t) = targets.first() {
            reply_to = Some(t.clone());
        }
        let mut thread = None;
        if let Some(r) = &reply_to {
            let parent = self.get_post(r).await?.ok_or_else(|| Fail::value(format!("no post {r} to reply to")))?;
            thread = Some(parent.root().to_string());
            let root = match &parent.thread {
                None => parent.clone(),
                Some(t) => self.get_post(t).await?.unwrap_or(parent.clone()),
            };
            project = root.project.clone();
            scope = root.scope.clone();
        }
        self.require_scope(&project, &scope).await?;
        let ms = new.ts.unwrap_or_else(now_ms);
        let mut meta = coerce_meta(&new.meta);
        if let (Some(_), false) = (&new.origin, new.author.is_empty()) {
            meta.insert("claimed_author".into(), json!(new.author));
        }
        let post = Post {
            id: uuid::Uuid::new_v4().to_string(),
            project: project.clone(),
            scope: scope.clone(),
            author: new.origin.clone().unwrap_or_else(|| author_stored(&new.author)),
            body: new.body.clone(),
            meta_raw: awm_pycompat::json::dumps(&Value::Object(meta)),
            ms,
            seq: self.next_seq(),
            reply_to,
            thread,
            cites,
            tag_ids,
            version: 1,
            comments: None,
            matched: None,
        };
        self.write_posts(std::slice::from_ref(&post)).await?;
        let names = self.tag_names()?;
        self.emit_post(&project, &scope, &post.to_dict(&names));
        Ok(post)
    }

    /// `set_tags`: add and remove several tags on one post at once.
    pub async fn set_tags(&self, post_id: &str, add: Option<&Value>, remove: Option<&Value>) -> Result<Post> {
        let _w = self.write.lock().await;
        let mut p = self.get_post(post_id).await?.ok_or_else(|| Fail::value(format!("no post {post_id}")))?;
        let add_ids = self.resolve_tags(&names_arg(add)?)?;
        let remove_ids = self.resolve_tags(&names_arg(remove)?)?;
        let names = self.tag_names()?;
        let both: Vec<String> = add_ids.iter().filter(|t| remove_ids.contains(t)).cloned().collect();
        if !both.is_empty() {
            return Err(Fail::tag(format!(
                "tag {} is both added and removed",
                crate::tags::names_of(&names, &both).join(", ")
            )));
        }
        let fixed: Vec<String> = add_ids.iter().chain(&remove_ids).filter(|t| TYPE_TAGS.contains(&t.as_str())).cloned().collect();
        if !fixed.is_empty() {
            return Err(Fail::tag(format!(
                "{} is set when a post is written and cannot change after it",
                crate::tags::names_of(&names, &fixed).join(", ")
            )));
        }
        for t in add_ids {
            if !p.tag_ids.contains(&t) {
                p.tag_ids.push(t);
            }
        }
        p.tag_ids.retain(|t| !remove_ids.contains(t));
        self.write_posts(std::slice::from_ref(&p)).await?;
        Ok(p)
    }

    /// Add one of the code's own tags (closed, superseded, folded) to a post.
    pub(crate) async fn mark(&self, post_id: &str, tag_id: &str) -> Result<()> {
        if let Some(mut p) = self.get_post(post_id).await? {
            if !p.has(tag_id) {
                p.tag_ids.push(tag_id.to_string());
                self.write_posts(std::slice::from_ref(&p)).await?;
            }
        }
        Ok(())
    }

    /// The posts a fetch's own filters keep, before ordering: channel, author, time, tags,
    /// folded addenda and the caller's privacy exclusion.
    async fn filtered(&self, q: &Query, roots_only: bool) -> Result<(Vec<Post>, Names)> {
        let mut conds = Vec::new();
        if let Some(p) = &q.project {
            conds.push(eq("owner_project", p));
        }
        if let Some(s) = &q.scope {
            conds.push(eq("owner_scope", s));
        }
        if let Some(a) = q.author.as_deref().filter(|a| !a.is_empty()) {
            let stored = if privacy::from_peer(Some(a)) { a.to_string() } else { author_stored(a) };
            conds.push(eq("author", &stored));
        }
        if let Some(bms) = q.before_ts.as_deref().and_then(iso_to_ms) {
            conds.push(format!("ms < {bms}"));
        }
        if roots_only {
            conds.push("thread IS NULL".into());
        }
        let terms = parse_filter(q.tags.as_ref())?;
        let filter: Filter = self.tag_filter(q.tags.as_ref())?;
        let folded_named = terms.iter().any(|(_, n)| n.to_lowercase() == "folded");
        let names = self.tag_names()?;
        let posts = self
            .scan(&and(&conds))
            .await?
            .into_iter()
            .filter(|p| filter.keeps(&p.tag_ids))
            .filter(|p| folded_named || !p.has(FOLDED))
            .filter(|p| !q.exclude.as_ref().is_some_and(|x| x.hides(p, &names)))
            .collect();
        Ok((posts, names))
    }

    /// `channel.fetch`: a channel's posts, or with `query` the search's ranked thread starts.
    pub async fn fetch(&self, q: &Query) -> Result<Vec<Post>> {
        if q.query.as_deref().is_some_and(|s| !s.is_empty()) {
            return Ok(self.search(q).await?.0);
        }
        let (mut posts, _) = self.filtered(q, false).await?;
        let desc = match q.order.as_deref() {
            Some("asc") => false,
            Some("desc") => true,
            _ => q.scope.is_none(),
        };
        posts.sort_by(|a, b| {
            let o = (a.ms, a.seq).cmp(&(b.ms, b.seq));
            if desc {
                o.reverse()
            } else {
                o
            }
        });
        Ok(posts.into_iter().skip(q.offset).take(q.limit).collect())
    }

    /// `indexed.may_hold`: whether a tag filter can match a thread the search index ranks. A
    /// filter that requires a kind tag outside the indexed ones, such as `brief`, cannot.
    pub fn may_hold(&self, tags: Option<&Value>) -> Result<bool> {
        for (negated, name) in parse_filter(tags)? {
            if let Some(t) = self.tag_by_name(&name)? {
                let kind = KIND_TAGS.contains(&t.id.as_str()) || t.id.starts_with(MIGRATED_PREFIX);
                if !negated && !INDEXED_TAGS.contains(&t.id.as_str()) && kind {
                    return Ok(false);
                }
            }
        }
        Ok(true)
    }

    /// The search index's hits on the journal's own thread starts, best first: its `exact`
    /// section, then `related`.
    async fn ranked(&self, query: &str, project: Option<&str>, scope: Option<&str>, before_ts: Option<&str>, want: usize) -> Result<Vec<String>, String> {
        let mut filter = Map::new();
        if let Some(p) = project.filter(|p| !p.is_empty()) {
            filter.insert("project".into(), json!(p));
        }
        if let Some(s) = scope.filter(|s| !s.is_empty()) {
            filter.insert("scope".into(), json!(s));
        }
        if let Some(b) = before_ts.filter(|b| !b.is_empty()) {
            filter.insert("ts".into(), json!({"$lt": b}));
        }
        let text = parse_text(&json!(query)).map_err(|e| e.to_string())?;
        let source = parse_source(&Value::Object(filter), &crate::posts::fields(), "journals", "query.journals").map_err(|e| e.to_string())?;
        let max = want.clamp(1, SEARCH_MAX);
        let answer = self
            .answer(Some(&text), &source, &Veil::None, max, None, crate::source::Scope::Threads)
            .await
            .map_err(|e| format!("search source journals: {}", e.message))?;
        let mut seen = HashSet::new();
        let mut out = Vec::new();
        for h in answer.exact.iter().chain(answer.related.iter().flatten()) {
            if let Some(id) = h.get("id").and_then(Value::as_str) {
                if seen.insert(id.to_string()) {
                    out.push(id.to_string());
                }
            }
        }
        Ok(out)
    }

    /// `channel.search`: thread first posts matching `query`, best first, a `degraded` block,
    /// and which engine ranked them: `search`, or `none` for a substring match.
    pub async fn search(&self, q: &Query) -> Result<(Vec<Post>, Option<Value>, &'static str)> {
        let query = q.query.clone().unwrap_or_default();
        let (allowed, _) = self.filtered(q, true).await?;
        let mut degraded = None;
        let mut semantic = "none";
        let mut hits = None;
        if self.may_hold(q.tags.as_ref())? {
            let excluding = !matches!(q.exclude, None | Some(Exclusion::Nothing));
            let narrowed = q.author.as_deref().is_some_and(|a| !a.is_empty())
                || excluding
                || q.project.as_deref() == Some("")
                || !parse_filter(q.tags.as_ref())?.is_empty();
            let want = if narrowed { SEARCH_MAX } else { q.offset + q.limit };
            match self.ranked(&query, q.project.as_deref(), q.scope.as_deref(), q.before_ts.as_deref(), want).await {
                Ok(h) => {
                    hits = Some(h);
                    semantic = "search";
                }
                Err(e) => degraded = Some(degraded_block(&e)),
            }
        }
        let mut posts: Vec<Post> = match hits {
            None => {
                let mut bodies: HashMap<String, Vec<String>> = HashMap::new();
                for c in self.scan("thread IS NOT NULL").await? {
                    if let Some(t) = &c.thread {
                        bodies.entry(t.clone()).or_default().push(c.body);
                    }
                }
                let mut found: Vec<Post> = allowed
                    .into_iter()
                    .filter(|p| like(&p.body, &query) || bodies.get(&p.id).is_some_and(|bs| bs.iter().any(|b| like(b, &query))))
                    .collect();
                found.sort_by_key(|p| std::cmp::Reverse((p.ms, p.seq)));
                found.into_iter().skip(q.offset).take(q.limit).collect()
            }
            Some(ids) => {
                let mut by_id: HashMap<String, Post> = allowed.into_iter().map(|p| (p.id.clone(), p)).collect();
                let mut kept: Vec<Post> = ids.iter().filter_map(|i| by_id.remove(i)).collect();
                for (rank, p) in kept.iter_mut().enumerate() {
                    p.matched = Some(json!({"rank": rank + 1, "snippet": snippet(&p.body, &query)}));
                }
                kept.into_iter().skip(q.offset).take(q.limit).collect()
            }
        };
        let ids: Vec<String> = posts.iter().map(|p| p.id.clone()).collect();
        let counts = self.comment_counts(&ids).await?;
        for p in &mut posts {
            p.comments = Some(counts.get(&p.id).copied().unwrap_or(0));
        }
        match q.order.as_deref() {
            Some("asc") => posts.sort_by_key(|p| p.ms),
            Some("desc") => posts.sort_by_key(|p| std::cmp::Reverse(p.ms)),
            _ => {}
        }
        Ok((posts, degraded, semantic))
    }

    /// `_channel_project`: the project to filter a scope's posts by. A literal scope name
    /// repeats across projects, so a scope with no project is refused and the projects that
    /// have it are named.
    pub async fn channel_project(&self, project: Option<String>, scope: Option<&str>) -> Result<Option<String>> {
        let Some(scope) = scope.filter(|s| !s.is_empty()) else { return Ok(project) };
        if project.is_some() {
            return Ok(project);
        }
        if scope.contains(':') || scope == "workspace" {
            return Ok(Some(String::new()));
        }
        let mut have: Vec<String> = self
            .scan(&and(&[eq("owner_scope", scope), "owner_project != ''".into()]))
            .await?
            .into_iter()
            .map(|p| p.project)
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .collect();
        have.truncate(7);
        let mut named = have.iter().take(6).cloned().collect::<Vec<_>>().join(", ");
        if have.len() > 6 {
            named.push_str(", …");
        }
        let r = str_repr(scope);
        let mut msg = format!("scope {r} needs project: scope names repeat across projects");
        if !have.is_empty() {
            msg.push_str(&format!(" ({r} has posts in {named})"));
        }
        msg.push_str(&format!(
            ". Pass project=<p>. For the messages waiting for you, use journal(verb=\"inbox\", args={{project:<p>, scope:{r}}})."
        ));
        Err(Fail::value(msg))
    }
}

fn degraded_block(e: &str) -> Value {
    degraded(e)
}

/// `py_str` of a value, for messages.
pub fn shown(v: &Value) -> String {
    py_str(v)
}
