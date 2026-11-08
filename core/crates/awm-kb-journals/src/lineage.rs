//! Who wrote a journal, what it cites, what each session read, and the lineage graph.
//!
//! A journal post carries `meta.session`, the R9 id of the session it covers, which defaults to
//! the posting session. A debrief journal's `reply_to` lists the journals that shaped the work.
//! Every journal that `fetch` hands a session lands in `session_reads`, the candidates the agent
//! picks its citations from. A parent link is a hard edge, "started by"; a citation is a soft
//! one, "built on". Both point one way, and the walk goes only forward.

use std::collections::{HashMap, HashSet};
use std::path::Path;

use awm_plugin_sdk::ids::to_r9;
use awm_pycompat::repr::str_repr;
use awm_pycompat::time::now_ms;
use awm_store::rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use serde_json::{json, Map, Value};

use crate::posts::{and, eq, has_tag, Post};
use crate::schema::JOURNAL;
use crate::{py_repr, truthy, Fail, Journals, Result};

pub const MAX_REASON: usize = 200;
pub const PARENT_KINDS: [&str; 2] = ["start", "clear"];
pub const MAX_EDGES: usize = 200;

/// `sessions.stamp`: `meta` with `session` in R9 form, defaulting to the posting session. A
/// caller names another session only to journal for it; the poster is kept as `posted_by`.
pub fn stamp(meta: &Value, caller: Option<&str>) -> Result<Value> {
    let Value::Object(m) = meta else {
        return Ok(match caller {
            Some(c) => json!({"session": c}),
            None => meta.clone(),
        });
    };
    let mut m = m.clone();
    let named = m.shift_remove("session");
    match named {
        None | Some(Value::Null) => {}
        Some(Value::String(s)) if s.is_empty() => {}
        Some(named) => {
            let session = named.as_str().and_then(|s| to_r9(Some(s)));
            let Some(session) = session else {
                return Err(Fail::value(format!("meta.session {} is not a session id", py_repr(&named))));
            };
            m.insert("session".into(), json!(session));
            if let Some(c) = caller.filter(|c| *c != session) {
                m.insert("posted_by".into(), json!(c));
            }
            return Ok(Value::Object(m));
        }
    }
    if let Some(c) = caller {
        m.insert("session".into(), json!(c));
    }
    Ok(Value::Object(m))
}

/// `sessions.fold_cites`: an old-style `meta.cites` as citation ids for `reply_to`, its reasons
/// kept as `meta.cite_reasons`.
pub fn fold_cites(meta: &Value, tag_ids: &[String]) -> Result<(Value, Vec<String>)> {
    let Value::Object(m) = meta else { return Ok((meta.clone(), vec![])) };
    if !m.contains_key("cites") {
        return Ok((meta.clone(), vec![]));
    }
    let mut m = m.clone();
    let cites = m.shift_remove("cites").unwrap_or(Value::Null);
    if cites.is_null() || cites.as_array().is_some_and(Vec::is_empty) {
        return Ok((Value::Object(m), vec![]));
    }
    if !tag_ids.iter().any(|t| t == JOURNAL) {
        return Err(Fail::value("only a debrief journal cites (sessions.md R14)"));
    }
    let Value::Array(cites) = cites else {
        return Err(Fail::value("meta.cites is a list of {journal}; pass the ids in reply_to instead"));
    };
    let (mut ids, mut reasons) = (Vec::<String>::new(), Map::new());
    for c in &cites {
        let Some(j) = c.get("journal").and_then(Value::as_str).filter(|_| c.is_object()) else {
            return Err(Fail::value("each meta.cites entry is {journal: <post id>}; use reply_to"));
        };
        if !ids.iter().any(|i| i == j) {
            ids.push(j.to_string());
        }
        if let Some(Value::String(r)) = c.get("reason") {
            if !awm_pycompat::pystr::strip(r).is_empty() {
                let folded = awm_pycompat::pystr::split(r).join(" ");
                reasons.insert(j.to_string(), json!(awm_pycompat::pystr::head(&folded, MAX_REASON)));
            }
        }
    }
    if !reasons.is_empty() {
        let mut merged = match m.get("cite_reasons") {
            Some(Value::Object(o)) => o.clone(),
            _ => Map::new(),
        };
        for (k, v) in reasons {
            merged.insert(k, v);
        }
        m.insert("cite_reasons".into(), Value::Object(merged));
    }
    Ok((Value::Object(m), ids))
}

fn first_line_title(p: &Post) -> Value {
    let meta = p.meta();
    match meta.get("title").filter(|v| truthy(v)) {
        Some(t) => t.clone(),
        None => {
            let lines = awm_pycompat::pystr::splitlines(awm_pycompat::pystr::strip(&p.body), false);
            json!(lines.first().map(|l| awm_pycompat::pystr::head(l, 120)).unwrap_or(""))
        }
    }
}

impl Journals {
    /// Note each journal in `posts` as read by `session`. Its own journals are skipped.
    pub fn record_reads(&self, session: Option<&str>, posts: &[Post]) -> Result<i64> {
        let Some(session) = session.filter(|s| !s.is_empty()) else { return Ok(0) };
        let ids: Vec<String> = posts
            .iter()
            .filter(|p| p.has(JOURNAL) && p.meta().get("session").and_then(Value::as_str) != Some(session))
            .map(|p| p.id.clone())
            .collect();
        if ids.is_empty() {
            return Ok(0);
        }
        let n = ids.len() as i64;
        let mut unique = ids;
        let mut seen = HashSet::new();
        unique.retain(|i| seen.insert(i.clone()));
        let (s, now) = (session.to_string(), now_ms());
        self.db.blocking_write(move |tx| {
            for pid in &unique {
                tx.execute(
                    "INSERT INTO session_reads (session, post_id, first_read, last_read) VALUES (?,?,?,?) \
                     ON CONFLICT (session, post_id) DO UPDATE SET last_read=excluded.last_read, times=times+1",
                    params![s, pid, now, now],
                )?;
            }
            Ok::<_, Fail>(())
        })?;
        Ok(n)
    }

    /// The journals `session` read, newest read first.
    pub async fn reads(&self, session: &str, limit: i64) -> Result<Vec<Value>> {
        let s = session.to_string();
        let rows: Vec<(String, i64, i64, i64)> = self.db.blocking_read(move |c| {
            let mut stmt = c.prepare("SELECT post_id, first_read, last_read, times FROM session_reads WHERE session=? ORDER BY last_read DESC")?;
            let rows = stmt.query_map([s], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?.collect::<std::result::Result<Vec<_>, _>>()?;
            Ok::<_, Fail>(rows)
        })?;
        let ids: Vec<String> = rows.iter().map(|r| r.0.clone()).collect();
        let posts = self.posts_by_id(&ids).await?;
        Ok(rows
            .into_iter()
            .filter_map(|(id, first, last, times)| {
                let p = posts.get(&id)?;
                Some(json!({
                    "journal": id, "project": p.project, "scope": p.scope, "title": first_line_title(p),
                    "session": p.meta().get("session").cloned().unwrap_or(Value::Null),
                    "first_read": first, "last_read": last, "times": times,
                }))
            })
            .take(limit.max(0) as usize)
            .collect())
    }

    /// The journals that `session`'s own journals cite, with the session each covers.
    pub async fn cited_by_session(&self, session: &str) -> Result<Vec<Value>> {
        let mut own = self.scan(&and(&[eq("session", session), has_tag(JOURNAL)])).await?;
        own.sort_by_key(|p| (p.ms, p.seq));
        let cited: Vec<String> = own.iter().flat_map(|p| p.cites.clone()).collect();
        let targets = self.posts_by_id(&cited).await?;
        let mut out = Vec::new();
        for p in &own {
            for c in &p.cites {
                if let Some(t) = targets.get(c) {
                    out.push(json!({"journal": c, "session": t.meta().get("session").cloned().unwrap_or(Value::Null), "from": p.id}));
                }
            }
        }
        Ok(out)
    }

    /// The union of the citations of every journal `session` wrote.
    pub async fn cites_of(&self, session: &str) -> Result<Vec<Value>> {
        let mut seen = HashSet::new();
        Ok(self.cited_by_session(session).await?.into_iter().filter(|c| seen.insert(c["journal"].to_string())).collect())
    }

    /// Record who started a session. The first link wins; false when nothing changed.
    pub fn set_parent(&self, session_id: &str, parent_id: &str, how: &str, node: &str, at: Option<f64>) -> Result<bool> {
        if !PARENT_KINDS.contains(&how) {
            return Err(Fail::value(format!("how is one of ('start', 'clear'), not {}", str_repr(how))));
        }
        let (Some(session), Some(parent)) = (to_r9(Some(session_id)), to_r9(Some(parent_id))) else {
            return Err(Fail::value("session_id and parent are session ids"));
        };
        if session == parent {
            return Ok(false);
        }
        let at = at.unwrap_or_else(|| awm_pycompat::time::now_us() as f64 / 1e6);
        let (how, node) = (how.to_string(), node.to_string());
        self.db.blocking_write(move |tx| {
            let n = tx.execute(
                "INSERT OR IGNORE INTO session_parents (session, parent, how, node, at) VALUES (?,?,?,?,?)",
                params![session, parent, how, node, at],
            )?;
            Ok::<_, Fail>(n > 0)
        })
    }

    pub fn parent_of(&self, session: &str) -> Result<Option<Value>> {
        let s = session.to_string();
        self.db.blocking_read(move |c| {
            Ok::<_, Fail>(
                c.query_row("SELECT session, parent, how, node, at FROM session_parents WHERE session=?", [s], |r| {
                    Ok(json!({"session": r.get::<_, String>(0)?, "parent": r.get::<_, String>(1)?, "how": r.get::<_, String>(2)?,
                              "node": r.get::<_, String>(3)?, "at": r.get::<_, f64>(4)?}))
                })
                .optional()?,
            )
        })
    }

    /// Every edge reachable forward from `session_id`: parents, then what each debrief cites.
    pub async fn walk(&self, session_id: &str, limit: usize) -> Result<Value> {
        let start = to_r9(Some(session_id)).ok_or_else(|| Fail::value(format!("{} is not a session id", str_repr(session_id))))?;
        let mut edges = Vec::new();
        let mut seen = HashSet::from([start.clone()]);
        let mut queue = std::collections::VecDeque::from([start.clone()]);
        while let Some(s) = queue.pop_front() {
            if edges.len() >= limit {
                break;
            }
            if let Some(p) = self.parent_of(&s)? {
                let parent = p["parent"].as_str().unwrap_or("").to_string();
                edges.push(json!({"from": s, "to": parent, "kind": "parent", "how": p["how"], "node": p["node"], "at": p["at"]}));
                if seen.insert(parent.clone()) {
                    queue.push_back(parent);
                }
            }
            for c in self.cited_by_session(&s).await? {
                edges.push(json!({"from": s, "to": c["journal"], "kind": "cite", "via": c["from"], "session": c["session"]}));
                if let Some(cs) = c["session"].as_str().filter(|x| !x.is_empty()) {
                    if seen.insert(cs.to_string()) {
                        queue.push_back(cs.to_string());
                    }
                }
            }
        }
        let truncated = edges.len() >= limit;
        edges.truncate(limit);
        Ok(json!({"session": start, "edges": edges, "truncated": truncated}))
    }

    /// Copy reporting's `session_parents` rows once. The first link still wins.
    pub fn import_parents(&self, source: &Path) -> Result<Value> {
        let shown = source.to_string_lossy().to_string();
        if !source.exists() {
            return Ok(json!({"status": "no-source", "source": shown}));
        }
        let src = Connection::open_with_flags(source, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        let rows: std::result::Result<Vec<(String, String, String, Option<String>, f64)>, _> = src
            .prepare("SELECT session, parent, how, node, at FROM session_parents")
            .and_then(|mut s| s.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?.collect());
        let rows = match rows {
            Ok(r) => r,
            Err(e) if e.to_string().contains("no such table") => return Ok(json!({"status": "no-table", "source": shown})),
            Err(e) => return Err(e.into()),
        };
        let mut made = 0;
        for (s, p, how, node, at) in &rows {
            let how = if PARENT_KINDS.contains(&how.as_str()) { how.as_str() } else { "start" };
            if self.set_parent(s, p, how, node.as_deref().unwrap_or(""), Some(*at))? {
                made += 1;
            }
        }
        Ok(json!({"status": "imported", "source": shown, "rows": rows.len(), "recorded": made}))
    }

    /// Each post's own read state, by id, for tests and the export-back.
    pub fn read_counts(&self, session: &str) -> Result<HashMap<String, i64>> {
        let s = session.to_string();
        self.db.blocking_read(move |c| {
            let mut stmt = c.prepare("SELECT post_id, times FROM session_reads WHERE session=?")?;
            let out = stmt.query_map([s], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<std::result::Result<HashMap<_, _>, _>>()?;
            Ok::<_, Fail>(out)
        })
    }
}
