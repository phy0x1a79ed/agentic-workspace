//! A scope's inbox: a bounded digest of its channel and the scope's read cursor.
//!
//! The cursor belongs to the scope, not to a session, and is a position `(ts, id)` that only
//! moves forward. A post the scope wrote itself is never unread. The inbox also holds each
//! comment on a thread the scope started, in whatever channel that thread lives.

use std::collections::HashMap;
use std::sync::LazyLock;

use awm_pycompat::pystr::{split, splitlines, strip};
use awm_pycompat::time::{ms_to_iso, now_ms};
use awm_store::rusqlite::{params, OptionalExtension};
use regex::Regex;
use serde_json::{json, Map, Value};

use crate::caller::Exclusion;
use crate::posts::{author_display, eq, and, Post};
use crate::{truthy, Fail, Journals, Result};

pub const DEFAULT_LIMIT: i64 = 20;
pub const MAX_LIMIT: i64 = 50;
pub const GIST_WIDTH: usize = 120;

static TAG_RUN_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<[^>\n]*>").unwrap());
static SPACE_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s+").unwrap());
static GREETING_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^\s*hi,? this is claude on behalf of tony[.,!:]?\s*").unwrap());
static HEADING_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^#{1,6}\s+(.*)").unwrap());
static ASK_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\?|\b(please|request(?:s|ed)?|can you|could you|would you|needs? you|action required|todo|blocked|bug|broken|fix(?:es)?)\b").unwrap()
});

fn one_line(text: &str, width: usize) -> String {
    let text = SPACE_RE.replace_all(&TAG_RUN_RE.replace_all(text, ""), " ").to_string();
    let text = strip(&text);
    if text.chars().count() <= width {
        return text.to_string();
    }
    let cut: String = text.chars().take(width - 1).collect();
    format!("{}…", awm_pycompat::pystr::rstrip(&cut))
}

/// `re.split(r"(?<=[.!?])\s", s, 1)[0]`.
fn first_sentence(s: &str) -> &str {
    let mut prev = None;
    for (i, c) in s.char_indices() {
        if awm_pycompat::chars::is_space(c) && matches!(prev, Some('.' | '!' | '?')) {
            return &s[..i];
        }
        prev = Some(c);
    }
    s
}

/// One line for a post: its `meta.title`, else a leading heading, else its first sentence.
/// The courtesy greeting that opens a post sent on Tony's behalf is dropped first.
pub fn gist(body: &str, meta: &Map<String, Value>) -> String {
    if let Some(Value::String(t)) = meta.get("title") {
        if !strip(t).is_empty() {
            return one_line(t, GIST_WIDTH);
        }
    }
    let text = GREETING_RE.replace(body, "").to_string();
    let stripped = strip(&text);
    let paragraph = stripped.split("\n\n").next().unwrap_or("");
    let first = if strip(paragraph).is_empty() { "" } else { splitlines(strip(paragraph), false).first().copied().unwrap_or("") };
    if let Some(h) = HEADING_RE.captures(first) {
        return one_line(&h[1], GIST_WIDTH);
    }
    let joined = split(paragraph).join(" ");
    one_line(first_sentence(&joined), GIST_WIDTH)
}

/// Whether a post looks like it asks the reader to act. A keyword heuristic.
pub fn asks(body: &str, line: &str) -> bool {
    let head: String = body.chars().take(600).collect();
    ASK_RE.is_match(&format!("{line} {head}"))
}

fn age(ts_ms: i64, now: i64) -> String {
    let minutes = (now - ts_ms).max(0) / 60000;
    if minutes < 60 {
        format!("{minutes}m")
    } else if minutes < 48 * 60 {
        format!("{}h", minutes / 60)
    } else {
        format!("{}d", minutes / 1440)
    }
}

/// The author ref a channel's own posts carry.
pub fn self_ref(project: &str, scope: &str) -> String {
    if project.is_empty() && scope.starts_with("user:") {
        return scope.to_string();
    }
    format!("agent:{project}/{scope}")
}

fn default_tags(tags: Option<&Value>) -> Value {
    match tags {
        Some(v) if !(v.is_null() || v.as_str() == Some("") || v.as_array().is_some_and(Vec::is_empty)) => v.clone(),
        _ => json!("message"),
    }
}

impl Journals {
    fn cursor_row(&self, project: &str, scope: &str) -> Result<Option<(i64, String, i64, String)>> {
        let (p, s) = (project.to_string(), scope.to_string());
        self.db.blocking_read(move |c| {
            Ok::<_, Fail>(
                c.query_row(
                    "SELECT through_ts, through_id, acked_at, acked_by FROM scope_cursors WHERE owner_project=? AND owner_scope=?",
                    params![p, s],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
                )
                .optional()?,
            )
        })
    }

    pub fn cursor(&self, project: &str, scope: &str) -> Result<Value> {
        Ok(match self.cursor_row(project, scope)? {
            None => Value::Null,
            Some((ts, id, at, by)) => json!({
                "through_id": id,
                "through_ts": ms_to_iso(ts),
                "acked_at": ms_to_iso(at),
                "acked_by": if by.is_empty() { Value::Null } else { Value::String(by) },
            }),
        })
    }

    /// The scope's inbox: its channel, and comments on threads it started.
    async fn mine(&self, project: &str, scope: &str) -> Result<Vec<Post>> {
        let mut out: HashMap<String, Post> = HashMap::new();
        for p in self.scan(&and(&[eq("owner_project", project), eq("owner_scope", scope)])).await? {
            out.insert(p.id.clone(), p);
        }
        let roots: Vec<String> = self
            .scan(&and(&[eq("author", &self_ref(project, scope)), "thread IS NULL".into()]))
            .await?
            .into_iter()
            .map(|p| p.id)
            .collect();
        if !roots.is_empty() {
            for c in self.comment_scan(&roots).await? {
                out.insert(c.id.clone(), c);
            }
        }
        Ok(out.into_values().collect())
    }

    async fn comment_scan(&self, roots: &[String]) -> Result<Vec<Post>> {
        let mut out = Vec::new();
        for batch in roots.chunks(500) {
            let list: Vec<String> = batch.iter().map(|r| awm_index::sql::quote(r)).collect();
            out.extend(self.scan(&format!("thread IN ({})", list.join(", "))).await?);
        }
        Ok(out)
    }

    /// The inbox posts that pass the tag filter (default `message`) and the exclusion.
    async fn inbox_base(&self, project: &str, scope: &str, tags: Option<&Value>, exclude: &Exclusion) -> Result<Vec<Post>> {
        let filter = self.tag_filter(Some(&default_tags(tags)))?;
        let names = self.tag_names()?;
        Ok(self
            .mine(project, scope)
            .await?
            .into_iter()
            .filter(|p| filter.keeps(&p.tag_ids) && !exclude.hides(p, &names))
            .collect())
    }

    fn unread_test(&self, project: &str, scope: &str) -> Result<impl Fn(&Post) -> bool> {
        let me = self_ref(project, scope);
        let cur = self.cursor_row(project, scope)?.map(|(ts, id, _, _)| (ts, id));
        Ok(move |p: &Post| p.author != me && cur.as_ref().is_none_or(|(ts, id)| p.ms > *ts || (p.ms == *ts && p.id > *id)))
    }

    pub async fn unread_count(&self, project: &str, scope: &str, tags: Option<&Value>, exclude: &Exclusion) -> Result<i64> {
        let unread = self.unread_test(project, scope)?;
        Ok(self.inbox_base(project, scope, tags, exclude).await?.iter().filter(|p| unread(p)).count() as i64)
    }

    /// `inbox.digest`: the newest `limit` unread posts, one line each, with the counts around them.
    pub async fn digest(&self, project: &str, scope: &str, tags: Option<&Value>, limit: i64, include_read: bool, exclude: &Exclusion) -> Result<Value> {
        let limit = limit.clamp(1, MAX_LIMIT) as usize;
        let mut base = self.inbox_base(project, scope, tags, exclude).await?;
        base.sort_by(|a, b| (b.ms, &b.id).cmp(&(a.ms, &a.id)));
        let unread_test = self.unread_test(project, scope)?;
        let total = base.len();
        let unread = base.iter().filter(|p| unread_test(p)).count();
        let rows: Vec<&Post> =
            if include_read { base.iter().take(limit).collect() } else { base.iter().filter(|p| unread_test(p)).take(limit).collect() };
        let now = now_ms();
        let mut posts = Vec::new();
        for r in rows {
            let meta = r.meta();
            let line = gist(&r.body, &meta);
            let mut item = Map::new();
            item.insert("id".into(), json!(r.id));
            item.insert("author".into(), json!(author_display(&r.author)));
            item.insert("age".into(), json!(age(r.ms, now)));
            item.insert("asks".into(), json!(asks(&r.body, &line)));
            item.insert("gist".into(), json!(line));
            item.insert("chars".into(), json!(r.body.chars().count()));
            // Python's key order: id, author, age, gist, asks, chars.
            let mut ordered = Map::new();
            for k in ["id", "author", "age", "gist", "asks", "chars"] {
                ordered.insert(k.into(), item.remove(k).unwrap());
            }
            if let Some(rt) = &r.reply_to {
                ordered.insert("reply_to".into(), json!(rt));
                if (r.project.as_str(), r.scope.as_str()) != (project, scope) {
                    ordered.insert("channel".into(), json!(format!("{}/{}", r.project, r.scope).trim_start_matches('/')));
                }
            }
            if include_read {
                ordered.insert("read".into(), json!(!unread_test(r)));
            }
            posts.push(Value::Object(ordered));
        }
        let ack_through = posts.iter().find(|p| !p.get("read").is_some_and(truthy)).map(|p| p["id"].clone()).unwrap_or(Value::Null);
        let listed = posts.len();
        let mut out = Map::new();
        out.insert("project".into(), json!(project));
        out.insert("scope".into(), json!(scope));
        out.insert("tags".into(), default_tags(tags));
        out.insert("unread".into(), json!(unread));
        out.insert("total".into(), json!(total));
        out.insert("posts".into(), Value::Array(posts));
        out.insert("more".into(), json!((if include_read { total } else { unread }) as i64 - listed as i64));
        out.insert("cursor".into(), self.cursor(project, scope)?);
        out.insert("ack_through".into(), ack_through);
        if listed > 0 {
            out.insert(
                "next".into(),
                json!("Read a post in full with fetch(post_id=<id>). Mark these read with ack(project, scope, through=<ack_through>)."),
            );
        }
        Ok(Value::Object(out))
    }

    /// `inbox.ack`: move the scope's cursor to `through`, or to its newest post. It never moves back.
    pub async fn ack(&self, project: &str, scope: &str, through: Option<&str>, by: Option<&str>) -> Result<Value> {
        let mine = self.mine(project, scope).await?;
        let row = match through.filter(|t| !t.is_empty()) {
            Some(t) => Some(mine.iter().find(|p| p.id == t).ok_or_else(|| Fail::value(format!("no post {t} in {project}/{scope}")))?),
            None => mine.iter().max_by(|a, b| (a.ms, &a.id).cmp(&(b.ms, &b.id))),
        };
        let mut moved = false;
        if let Some(row) = row {
            let (p, s, ts, id, by) = (project.to_string(), scope.to_string(), row.ms, row.id.clone(), by.unwrap_or("").to_string());
            let now = now_ms();
            moved = self.db.blocking_write(move |tx| {
                let cur: Option<(i64, String)> = tx
                    .query_row(
                        "SELECT through_ts, through_id FROM scope_cursors WHERE owner_project=? AND owner_scope=?",
                        params![p, s],
                        |r| Ok((r.get(0)?, r.get(1)?)),
                    )
                    .optional()?;
                if cur.is_none_or(|c| (ts, id.clone()) > c) {
                    tx.execute(
                        "INSERT INTO scope_cursors (owner_project, owner_scope, through_ts, through_id, acked_at, acked_by) VALUES (?, ?, ?, ?, ?, ?) \
                         ON CONFLICT(owner_project, owner_scope) DO UPDATE SET through_ts=excluded.through_ts, through_id=excluded.through_id, \
                         acked_at=excluded.acked_at, acked_by=excluded.acked_by",
                        params![p, s, ts, id, now, by],
                    )?;
                    return Ok::<_, Fail>(true);
                }
                Ok(false)
            })?;
        }
        Ok(json!({
            "project": project, "scope": scope, "moved": moved,
            "cursor": self.cursor(project, scope)?,
            "unread": self.unread_count(project, scope, None, &Exclusion::Nothing).await?,
        }))
    }
}
