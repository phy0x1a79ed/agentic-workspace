//! The one-shot import of `precedence.db`: each decision becomes a workspace post tagged
//! `precedent`, its notes comments, its supersedes replies. Each post's id derives from the
//! decision's, so a rerun adds nothing. The source is opened read only.

use std::collections::{BTreeSet, HashMap, HashSet};
use std::path::Path;

use awm_pycompat::time::{fromisoformat, now_ms, timestamp_ms};
use awm_store::rusqlite::{Connection, OpenFlags};
use serde_json::{json, Map, Value};

use crate::posts::Post;
use crate::schema::{CLOSED, MESSAGE, PRECEDENT, SUPERSEDED};
use crate::tags::normalize_name;
use crate::{Fail, Journals, Result};

const AUTHOR: &str = "system";
const TAG_DESCRIPTION: &str = "A topic tag on precedents, imported from precedence.";

fn namespace() -> uuid::Uuid {
    uuid::Uuid::new_v5(&uuid::Uuid::NAMESPACE_URL, b"awm:precedence-import")
}

pub fn post_id(kind: &str, source_id: &str) -> String {
    uuid::Uuid::new_v5(&namespace(), format!("{kind}:{source_id}").as_bytes()).to_string()
}

pub fn body(context: &str, question: &str, decision: &str) -> String {
    use awm_pycompat::pystr::strip;
    format!("Context: {}\n\nQuestion: {}\n\nDecision: {}", strip(context), strip(question), strip(decision))
}

/// The first of `stamps` that parses, as ms; else now.
fn ms(stamps: &[Option<&str>]) -> i64 {
    for ts in stamps.iter().flatten() {
        let s = awm_pycompat::pystr::strip(ts).replace('Z', "+00:00");
        if s.is_empty() {
            continue;
        }
        if let Some(dt) = fromisoformat(&s) {
            return timestamp_ms(dt.utc_us());
        }
        let day: String = s.chars().take(10).collect();
        if day.len() == 10 && day.as_bytes()[4] == b'-' && day.as_bytes()[7] == b'-' {
            if let Some(dt) = fromisoformat(&day) {
                return timestamp_ms(dt.utc_us());
            }
        }
    }
    now_ms()
}

fn tag_name(raw: &str) -> Option<String> {
    normalize_name(&json!(awm_pycompat::pystr::split(raw).join("-"))).ok()
}

struct Decision {
    id: String,
    context: String,
    question: String,
    decision: String,
    created: Option<String>,
    added: Option<String>,
    status: String,
    superseded_by: Option<String>,
    source: Option<String>,
    source_ref: Option<String>,
}

struct Note {
    id: String,
    decision_id: String,
    body: String,
    kind: String,
    author: Option<String>,
    created: Option<String>,
}

type Source = (Vec<Decision>, Vec<(String, String)>, Vec<Note>);

fn read_source(path: &Path) -> Result<Source> {
    let src = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let mut stmt = src.prepare("SELECT * FROM decisions ORDER BY created, added, id")?;
    let decisions = stmt
        .query_map([], |r| {
            Ok(Decision {
                id: r.get("id")?,
                context: r.get::<_, Option<String>>("context")?.unwrap_or_default(),
                question: r.get::<_, Option<String>>("question")?.unwrap_or_default(),
                decision: r.get::<_, Option<String>>("decision")?.unwrap_or_default(),
                created: r.get("created")?,
                added: r.get("added")?,
                status: r.get::<_, Option<String>>("status")?.unwrap_or_default(),
                superseded_by: r.get("superseded_by")?,
                source: r.get("source")?,
                source_ref: r.get("source_ref")?,
            })
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let mut stmt = src.prepare("SELECT decision_id, tag FROM tags")?;
    let tags = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<std::result::Result<Vec<_>, _>>()?;
    let mut stmt = src.prepare("SELECT * FROM notes ORDER BY created, id")?;
    let notes = stmt
        .query_map([], |r| {
            Ok(Note {
                id: r.get("id")?,
                decision_id: r.get("decision_id")?,
                body: r.get("body")?,
                kind: r.get::<_, Option<String>>("kind")?.unwrap_or_default(),
                author: r.get("author")?,
                created: r.get("created")?,
            })
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    Ok((decisions, tags, notes))
}

impl Journals {
    /// Import every decision, tag and note of `source`; returns the counts.
    pub async fn import_precedents(&self, source: &Path, dry_run: bool) -> Result<Value> {
        let shown = source.to_string_lossy().to_string();
        if !source.exists() {
            return Ok(json!({"status": "no-source", "source": shown}));
        }
        let (decisions, dtags, notes) = read_source(source)?;
        let mut names: HashMap<String, Vec<String>> = HashMap::new();
        let mut skipped = BTreeSet::new();
        for (did, raw) in &dtags {
            match tag_name(raw) {
                None => {
                    skipped.insert(raw.clone());
                }
                Some(n) => names.entry(did.clone()).or_default().push(n),
            }
        }
        let mut new_tags = BTreeSet::new();
        for n in names.values().flatten() {
            if self.tag_by_name(n)?.is_none() {
                new_tags.insert(n.clone());
            }
        }
        let by_id: HashMap<&str, &Decision> = decisions.iter().map(|d| (d.id.as_str(), d)).collect();
        let pid: HashMap<&str, String> = decisions.iter().map(|d| (d.id.as_str(), post_id("decision", &d.id))).collect();
        // A decision replaces the oldest one that names it; any other it replaces gets a comment.
        let mut replaces: Vec<(String, Vec<String>)> = Vec::new();
        for d in &decisions {
            if let Some(new) = d.superseded_by.as_deref().filter(|n| !n.is_empty() && by_id.contains_key(n) && *n != d.id) {
                match replaces.iter_mut().find(|(k, _)| k == new) {
                    Some((_, v)) => v.push(d.id.clone()),
                    None => replaces.push((new.to_string(), vec![d.id.clone()])),
                }
            }
        }
        let replaces_of = |id: &str| replaces.iter().find(|(k, _)| k == id).map(|(_, v)| v.clone()).unwrap_or_default();
        let reply: HashMap<String, String> = replaces.iter().map(|(new, olds)| (new.clone(), olds[0].clone())).collect();
        let replaced: HashSet<String> = replaces.iter().flat_map(|(_, v)| v.clone()).collect();
        let root = |did: &str| -> String {
            let mut did = did.to_string();
            let mut seen = HashSet::new();
            while let Some(r) = reply.get(&did) {
                if !seen.insert(did.clone()) {
                    break;
                }
                did = r.clone();
            }
            did
        };
        let ids: Vec<String> = pid.values().cloned().collect();
        let existing: HashSet<String> = self.posts_by_id(&ids).await?.into_keys().collect();
        let out = json!({
            "status": if dry_run { "dry-run" } else { "imported" },
            "source": shown,
            "decisions": decisions.len() - existing.len(),
            "notes": notes.len(),
            "tags_added": new_tags.iter().collect::<Vec<_>>(),
            "tags_skipped": skipped.iter().collect::<Vec<_>>(),
            "already": existing.len(),
        });
        if dry_run {
            return Ok(out);
        }
        for n in &new_tags {
            self.tag_add(&json!(n), TAG_DESCRIPTION, "precedence-import", None).await?;
        }
        let mut rows: Vec<Post> = Vec::new();
        let mut notes_out: Vec<(String, String, String, String, Option<String>)> = Vec::new();
        for d in &decisions {
            if existing.contains(&pid[d.id.as_str()]) {
                continue;
            }
            let top = root(&d.id);
            let mut meta = Map::new();
            let title: String = d.question.chars().take(120).collect();
            for (k, v) in [
                ("title", Some(title)),
                ("precedence_id", Some(d.id.clone())),
                ("created", d.created.clone()),
                ("source", d.source.clone()),
                ("source_ref", d.source_ref.clone()),
            ] {
                if let Some(v) = v.filter(|v| !v.is_empty()) {
                    meta.insert(k.into(), json!(v));
                }
            }
            let mut tag_ids = vec![PRECEDENT.to_string()];
            for n in names.get(&d.id).into_iter().flatten() {
                if let Some(t) = self.tag_by_name(n)? {
                    tag_ids.push(t.id);
                }
            }
            if d.status == "superseded" || replaced.contains(&d.id) {
                tag_ids.push(SUPERSEDED.into());
            }
            if d.status == "retired" {
                tag_ids.push(CLOSED.into());
            }
            let mut uniq = Vec::new();
            for t in tag_ids {
                if !uniq.contains(&t) {
                    uniq.push(t);
                }
            }
            rows.push(Post {
                id: pid[d.id.as_str()].clone(),
                project: String::new(),
                scope: "workspace".into(),
                author: AUTHOR.into(),
                body: body(&d.context, &d.question, &d.decision),
                meta_raw: awm_pycompat::json::dumps(&Value::Object(meta)),
                ms: ms(&[d.created.as_deref(), d.added.as_deref()]),
                seq: 0,
                reply_to: reply.get(&d.id).map(|r| pid[r.as_str()].clone()),
                thread: (top != d.id).then(|| pid[top.as_str()].clone()),
                cites: vec![],
                tag_ids: uniq,
                version: 1,
                comments: None,
                matched: None,
            });
            for extra in replaces_of(&d.id).iter().skip(1) {
                notes_out.push((
                    post_id("supersede", &format!("{extra}>{}", d.id)),
                    extra.clone(),
                    format!("Superseded by {}.", pid[d.id.as_str()]),
                    AUTHOR.into(),
                    d.created.clone().or(d.added.clone()),
                ));
            }
        }
        for n in &notes {
            if by_id.contains_key(n.decision_id.as_str()) {
                let label = match n.kind.as_str() {
                    "amendment" => Some("Amendment"),
                    "context-change" => Some("Context change"),
                    "supersede" => Some("Supersede"),
                    _ => None,
                };
                let text = match label {
                    Some(l) => format!("{l}: {}", n.body),
                    None => n.body.clone(),
                };
                let author = n.author.clone().filter(|a| !a.is_empty()).unwrap_or_else(|| AUTHOR.into());
                notes_out.push((post_id("note", &n.id), n.decision_id.clone(), text, author, n.created.clone()));
            }
        }
        for (nid, did, text, author, created) in notes_out {
            let author = if author.contains(':') || author == "system" { author } else { format!("user:{author}") };
            rows.push(Post {
                id: nid,
                project: String::new(),
                scope: "workspace".into(),
                author,
                body: text,
                meta_raw: "{}".into(),
                ms: ms(&[created.as_deref()]),
                seq: 0,
                reply_to: Some(pid[did.as_str()].clone()),
                thread: Some(pid[root(&did).as_str()].clone()),
                cites: vec![],
                tag_ids: vec![MESSAGE.into()],
                version: 1,
                comments: None,
                matched: None,
            });
        }
        let all: Vec<String> = rows.iter().map(|r| r.id.clone()).collect();
        let held: HashSet<String> = self.posts_by_id(&all).await?.into_keys().collect();
        let mut seen = HashSet::new();
        rows.retain(|r| !held.contains(&r.id) && seen.insert(r.id.clone()));
        for r in &mut rows {
            r.seq = self.next_seq();
        }
        let _w = self.write.lock().await;
        if !rows.is_empty() {
            self.write_posts(&rows).await?;
        }
        Ok(out)
    }

    /// The node's own `precedence.db`.
    pub fn precedence_path(&self) -> std::path::PathBuf {
        self.awm_dir.join("services").join("precedence").join("precedence.db")
    }
}

impl From<std::io::Error> for Fail {
    fn from(e: std::io::Error) -> Self {
        Fail::new("OSError", e.to_string())
    }
}
