//! The tag vocabulary in journal.db, and tag filters such as `!closed and complaint`.
//!
//! A post holds its tags by id, so a rename changes only the registry and shows on every post.
//! The fixed tags have fixed ids; the four type tags are set when a post is made, never after.

use std::sync::LazyLock;

use awm_pycompat::pystr::{lower, norm, strip};
use awm_pycompat::repr::str_repr;
use awm_pycompat::time::{ms_to_iso, now_ms};
use awm_store::rusqlite::{params, Connection, OptionalExtension, Row};
use regex::Regex;
use serde_json::{json, Value};

use crate::posts::Names;
use crate::{py_repr, py_str, Fail, Journals, Result};

static NAME_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9_.:+/-]{0,63}$").unwrap());
static AND_RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)\s+and\s+").unwrap());
const RESERVED: [&str; 3] = ["and", "not", "or"];

#[derive(Debug, Clone, PartialEq)]
pub struct Tag {
    pub id: String,
    pub name: String,
    pub description: String,
    pub origin: String,
    pub version: i64,
    pub created_ms: i64,
    pub updated_at: String,
}

impl Tag {
    pub fn to_dict(&self) -> Value {
        json!({"id": self.id, "name": self.name, "description": self.description, "origin": self.origin,
               "version": self.version, "created_ms": self.created_ms, "updated_at": self.updated_at})
    }

    fn from_row(r: &Row) -> awm_store::rusqlite::Result<Tag> {
        let updated: i64 = r.get("updated_at")?;
        Ok(Tag {
            id: r.get("id")?,
            name: r.get("name")?,
            description: r.get("description")?,
            origin: r.get::<_, Option<String>>("origin")?.unwrap_or_default(),
            version: r.get("version")?,
            created_ms: r.get("created_at")?,
            updated_at: ms_to_iso(updated).unwrap_or_default(),
        })
    }
}

/// `normalize_name`: the stored form of a tag name.
pub fn normalize_name(raw: &Value) -> Result<String> {
    let text = norm(&match raw {
        Value::Null => String::new(),
        v => py_str(v),
    });
    if !NAME_RE.is_match(&text) || RESERVED.contains(&text.as_str()) {
        let shown = match raw {
            Value::Null => "None".to_string(),
            v => py_repr(v),
        };
        return Err(Fail::tag(format!(
            "{shown} is not a tag name: use lowercase letters, digits and _ . : + / -, at most 64 characters, not and/or/not"
        )));
    }
    Ok(text)
}

/// A tag list argument as names: a list, or one comma-separated string.
pub fn names_arg(raw: Option<&Value>) -> Result<Vec<String>> {
    match raw {
        None | Some(Value::Null) => Ok(vec![]),
        Some(Value::String(s)) if s.is_empty() => Ok(vec![]),
        Some(Value::String(s)) => Ok(s.split(',').map(|x| strip(x).to_string()).filter(|x| !x.is_empty()).collect()),
        Some(Value::Array(a)) => Ok(a.iter().map(|x| strip(&py_str(x)).to_string()).filter(|x| !x.is_empty()).collect()),
        Some(_) => Err(Fail::tag("tags is a list of tag names")),
    }
}

/// `parse_filter`: `[(negated, name)]` from `"a and !b"`, or from a list of such terms.
pub fn parse_filter(expr: Option<&Value>) -> Result<Vec<(bool, String)>> {
    let parts: Vec<String> = match expr {
        None | Some(Value::Null) => return Ok(vec![]),
        Some(Value::String(s)) if s.is_empty() => return Ok(vec![]),
        Some(Value::Array(a)) if a.is_empty() => return Ok(vec![]),
        Some(Value::Array(a)) => a.iter().map(py_str).collect(),
        Some(v) => AND_RE.split(strip(&py_str(v))).map(str::to_string).collect(),
    };
    let mut terms = Vec::new();
    for raw in parts {
        let mut term = strip(&raw).to_string();
        let mut negated = false;
        loop {
            if let Some(rest) = term.strip_prefix('!') {
                negated = !negated;
                term = strip(rest).to_string();
            } else if lower(&term).starts_with("not ") {
                negated = !negated;
                term = strip(&term.chars().skip(4).collect::<String>()).to_string();
            } else {
                break;
            }
        }
        if term.is_empty() || term.contains(' ') {
            return Err(Fail::tag(format!(
                "cannot read the tag filter {}: join tags with 'and', negate one with '!', such as '!closed and complaint'",
                py_repr(expr.unwrap_or(&Value::Null))
            )));
        }
        terms.push((negated, term));
    }
    Ok(terms)
}

/// A filter resolved to ids: each term must hold (or, negated, must not).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Filter(pub Vec<(bool, String)>);

impl Filter {
    pub fn keeps(&self, tag_ids: &[String]) -> bool {
        self.0.iter().all(|(neg, id)| tag_ids.contains(id) != *neg)
    }
}

pub fn vocabulary(c: &Connection) -> Result<Vec<Tag>> {
    let mut stmt = c.prepare("SELECT * FROM tags ORDER BY name")?;
    let rows = stmt.query_map([], Tag::from_row)?.collect::<std::result::Result<Vec<_>, _>>()?;
    Ok(rows)
}

pub fn get(c: &Connection, id: &str) -> Result<Option<Tag>> {
    Ok(c.query_row("SELECT * FROM tags WHERE id=?", [id], Tag::from_row).optional()?)
}

pub fn by_name(c: &Connection, name: &str) -> Result<Option<Tag>> {
    Ok(c.query_row("SELECT * FROM tags WHERE name=? COLLATE NOCASE", [norm(name)], Tag::from_row).optional()?)
}

fn unknown(c: &Connection, names: &[String]) -> Fail {
    let listed: Vec<String> = names.iter().map(|n| str_repr(n)).collect();
    let vocab: Vec<String> = vocabulary(c).map(|v| v.into_iter().map(|t| t.name).collect()).unwrap_or_default();
    Fail::tag(format!(
        "unknown tag {}. The vocabulary is: {}. Add a tag with tag_add(name, description).",
        listed.join(", "),
        vocab.join(", ")
    ))
}

/// The ids of the named tags, in order, or a `TagError` naming each unknown one.
pub fn resolve(c: &Connection, names: &[String]) -> Result<Vec<String>> {
    let (mut ids, mut missing) = (Vec::new(), Vec::new());
    for n in names {
        match by_name(c, n)? {
            None => missing.push(n.clone()),
            Some(t) if !ids.contains(&t.id) => ids.push(t.id),
            Some(_) => {}
        }
    }
    if !missing.is_empty() {
        return Err(unknown(c, &missing));
    }
    Ok(ids)
}

/// A parsed filter's names as ids, refusing an unknown name.
pub fn resolve_filter(c: &Connection, terms: &[(bool, String)]) -> Result<Filter> {
    let mut missing = Vec::new();
    let mut out = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for (neg, name) in terms {
        match by_name(c, name)? {
            Some(t) => out.push((*neg, t.id)),
            None => {
                if seen.insert(name.clone()) {
                    missing.push(name.clone());
                }
            }
        }
    }
    if !missing.is_empty() {
        return Err(unknown(c, &missing));
    }
    Ok(Filter(out))
}

pub fn names(c: &Connection) -> Result<Names> {
    let mut stmt = c.prepare("SELECT id, name FROM tags")?;
    let out = stmt
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?
        .collect::<std::result::Result<Names, _>>()?;
    Ok(out)
}

/// `names_of`: the names of these ids, in their order, leaving out an id with no tag.
pub fn names_of(names: &Names, ids: &[String]) -> Vec<String> {
    ids.iter().filter_map(|i| names.get(i).cloned()).collect()
}

impl Journals {
    pub fn tag_names(&self) -> Result<Names> {
        self.db.blocking_read(names)
    }

    pub fn vocabulary(&self) -> Result<Vec<Tag>> {
        self.db.blocking_read(vocabulary)
    }

    pub fn tag(&self, id: &str) -> Result<Option<Tag>> {
        let id = id.to_string();
        self.db.blocking_read(move |c| get(c, &id))
    }

    pub fn tag_by_name(&self, name: &str) -> Result<Option<Tag>> {
        let name = name.to_string();
        self.db.blocking_read(move |c| by_name(c, &name))
    }

    /// A tag by its id or its current name.
    pub fn tag_ref(&self, r: &str) -> Result<Option<Tag>> {
        Ok(match self.tag(r)? {
            Some(t) => Some(t),
            None => self.tag_by_name(r)?,
        })
    }

    pub fn resolve_tags(&self, names: &[String]) -> Result<Vec<String>> {
        let names = names.to_vec();
        self.db.blocking_read(move |c| resolve(c, &names))
    }

    pub fn tag_filter(&self, expr: Option<&Value>) -> Result<Filter> {
        let terms = parse_filter(expr)?;
        if terms.is_empty() {
            return Ok(Filter::default());
        }
        self.db.blocking_read(move |c| resolve_filter(c, &terms))
    }

    /// `tags.add`: a new tag. The description is required.
    pub async fn tag_add(&self, name: &Value, description: &str, by: &str, origin: Option<&str>) -> Result<Tag> {
        let name = normalize_name(name)?;
        let description = strip(description).to_string();
        if description.is_empty() {
            return Err(Fail::tag("a tag needs a description: say what a post with this tag is"));
        }
        if self.tag_by_name(&name)?.is_some() {
            return Err(Fail::tag(format!("tag {} exists already; change it with tag_edit", str_repr(&name))));
        }
        if crate::tagsync::provisional(&name) {
            return Err(Fail::tag(format!("{} has the shape of a clash rename; choose another name", str_repr(&name))));
        }
        let id = uuid::Uuid::new_v4().simple().to_string();
        let now = now_ms();
        let node = origin.map(str::to_string).unwrap_or_else(|| self.node.clone());
        let (tid, by) = (id.clone(), by.to_string());
        self.db.blocking_write(move |tx| {
            tx.execute(
                "INSERT INTO tags (id, name, description, origin, version, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                params![tid, name, description, node, now, now],
            )?;
            tx.execute(
                "INSERT INTO tag_versions (tag_id, version, name, description, edited_at, edited_by) VALUES (?,1,?,?,?,?)",
                params![tid, name, description, now, by],
            )?;
            Ok::<_, Fail>(())
        })?;
        let tag = self.tag(&id)?.ok_or_else(|| Fail::runtime("the new tag vanished"))?;
        self.push_tag(&tag).await;
        Ok(tag)
    }

    /// `tags.edit`: a new name or description. The earlier version stays in `tag_versions`.
    pub async fn tag_edit(&self, tag_ref: &str, name: Option<&Value>, description: Option<&str>, by: &str) -> Result<Tag> {
        let tag = self.tag_ref(tag_ref)?.ok_or_else(|| self.unknown_tag(tag_ref))?;
        let new_name = match name {
            Some(v) if !(v.is_null() || v.as_str() == Some("")) => normalize_name(v)?,
            _ => tag.name.clone(),
        };
        let new_desc = match description {
            Some(d) if !d.is_empty() => strip(d).to_string(),
            _ => tag.description.clone(),
        };
        if new_name == tag.name && new_desc == tag.description {
            return Ok(tag);
        }
        if let Some(clash) = self.tag_by_name(&new_name)? {
            if clash.id != tag.id {
                return Err(Fail::tag(format!("tag {} exists already", str_repr(&new_name))));
            }
        }
        let now = now_ms();
        let (t, by) = (tag.clone(), by.to_string());
        self.db.blocking_write(move |tx| {
            let has: Option<i64> = tx.query_row("SELECT 1 FROM tag_versions WHERE tag_id=?", [&t.id], |r| r.get(0)).optional()?;
            if has.is_none() {
                tx.execute(
                    "INSERT INTO tag_versions (tag_id, version, name, description, edited_at, edited_by) VALUES (?,?,?,?,?,?)",
                    params![t.id, t.version, t.name, t.description, 0, ""],
                )?;
            }
            let version = t.version + 1;
            tx.execute(
                "UPDATE tags SET name=?, description=?, version=?, updated_at=? WHERE id=?",
                params![new_name, new_desc, version, now, t.id],
            )?;
            tx.execute(
                "INSERT INTO tag_versions (tag_id, version, name, description, edited_at, edited_by) VALUES (?,?,?,?,?,?)",
                params![t.id, version, new_name, new_desc, now, by],
            )?;
            Ok::<_, Fail>(())
        })?;
        let out = self.tag(&tag.id)?.ok_or_else(|| Fail::runtime("the tag vanished"))?;
        self.resolved_by_edit(&out.id)?;
        self.push_tag(&out).await;
        Ok(out)
    }

    /// `tags.versions`: each version of a tag, oldest first.
    pub fn tag_versions(&self, tag_ref: &str) -> Result<Vec<Value>> {
        let tag = match self.tag_ref(tag_ref)? {
            Some(t) => t,
            None => return Err(self.unknown_tag(tag_ref)),
        };
        self.db.blocking_read(move |c| {
            let mut stmt = c.prepare(
                "SELECT version, name, description, edited_at, edited_by FROM tag_versions WHERE tag_id=? ORDER BY version",
            )?;
            let rows = stmt
                .query_map([&tag.id], |r| {
                    let at: i64 = r.get(3)?;
                    let by: String = r.get(4)?;
                    Ok(json!({
                        "version": r.get::<_, i64>(0)?,
                        "name": r.get::<_, String>(1)?,
                        "description": r.get::<_, String>(2)?,
                        "edited_at": if at != 0 { ms_to_iso(at).map(Value::String).unwrap_or(Value::Null) } else { Value::Null },
                        "edited_by": if by.is_empty() { Value::Null } else { Value::String(by) },
                    }))
                })?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            Ok(rows)
        })
    }

    fn unknown_tag(&self, name: &str) -> Fail {
        self.db.blocking_read(|c| Ok::<_, Fail>(unknown(c, &[name.to_string()]))).unwrap_or_else(|e| e)
    }
}
