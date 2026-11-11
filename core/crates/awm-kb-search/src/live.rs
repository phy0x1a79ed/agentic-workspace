//! A source another plugin serves live, declared by its `[[service.archive]]` table: the thin
//! translation of a query into the arguments its verb takes today, and of its answer back into
//! sections. Such a source sees no caller, so search hides a peer the hits a mark covers.

use crate::hits::{self, py_str};
use crate::merge;
use crate::request::{SourceReq, Request};
use awm_index::grammar::Node;
use awm_index::Veil;
use awm_plugin_sdk::privacy;
use serde_json::{json, Map, Value};
pub use awm_pycompat::repr::truthy;

/// Whether a private mark covers a hit by its project and scope. Unreadable marks cover any hit
/// that names either.
pub fn covered(veil: &Veil, project: &str, scope: &str) -> bool {
    let marks = match veil {
        Veil::None => return false,
        Veil::Unreadable => return !project.is_empty() || !scope.is_empty(),
        Veil::Marks { marks, .. } => marks,
    };
    let (name, named) = privacy::canonical(Some(project), (!scope.is_empty()).then_some(scope));
    marks.iter().any(|m| {
        name == Some(m.project.as_str())
            && match &m.scope {
                None => true,
                Some(marked) => named.is_some_and(|s| s == marked || s.starts_with(&format!("{marked}/"))),
            }
    })
}

/// Whether a live hit falls under a private mark, by project and scope or by transcript slug.
pub fn unseen(hit: &Value, veil: &Veil) -> bool {
    let field = |k: &str| hit.get(k).map(py_str).unwrap_or_default();
    let project = field("project");
    if covered(veil, &project, &field("scope")) {
        return true;
    }
    let any_marks = matches!(veil, Veil::Marks { marks, .. } if !marks.is_empty());
    any_marks && !project.is_empty() && privacy::hidden_slug(&project)
}

fn unserved(src: &SourceReq, field: &str, served: &str) -> String {
    format!("query.{}.{field}: not yet served on {}; served: {served}", src.name, src.kind)
}

fn scalar(src: &SourceReq, field: &str, value: &Value, served: &str) -> Result<String, String> {
    let value = match value {
        Value::Object(m) if m.len() == 1 && m.contains_key("$eq") => &m["$eq"],
        other => other,
    };
    match value {
        Value::String(s) if !s.contains('*') => Ok(s.clone()),
        _ => Err(unserved(src, field, served)),
    }
}

/// The arguments a live source's `query` verb takes: one string of text and the fields its
/// service reads today. An error for what it cannot express.
pub fn live_args(src: &SourceReq, req: &Request) -> Result<Value, String> {
    let Some(text) = &req.text else {
        return Err(format!("query.{}: {} needs text; it lists nothing by its fields alone yet", src.name, src.kind));
    };
    let lit = |n: &Option<Node>| match n {
        Some(Node::Lit(s)) => Some(s.clone()),
        _ => None,
    };
    let shorthand = text.under_or && lit(&text.contains).is_some() && lit(&text.contains) == text.about;
    let words = match (&text.about, &text.contains) {
        (Some(about), None) => about.clone(),
        (Some(about), Some(_)) if shorthand => about.clone(),
        (None, Some(Node::Lit(s))) => s.clone(),
        _ => return Err(format!("query.text: {} serves a string, one $contains or one $about so far", src.kind)),
    };
    let mut args = Map::new();
    args.insert("query".into(), json!(words));
    args.insert("limit".into(), json!(req.max));
    let filter = &src.filter.filter;
    if src.kind == "transcripts" {
        for (field, value) in filter {
            if field != "project" {
                return Err(unserved(src, field, "project"));
            }
            args.insert("project".into(), json!(scalar(src, field, value, "project")?));
        }
    } else if src.kind == "kb" {
        let served = "author, ts with $gte and $lt";
        for (field, value) in filter {
            match (field.as_str(), value) {
                ("author", _) => {
                    args.insert("author".into(), json!(scalar(src, field, value, served)?));
                }
                ("ts", Value::Object(m)) if m.keys().all(|k| k == "$gte" || k == "$lt") => {
                    for (op, v) in m {
                        args.insert(if op == "$gte" { "after" } else { "before" }.into(), v.clone());
                    }
                }
                _ => return Err(unserved(src, field, served)),
            }
        }
    } else if let Some(field) = filter.keys().next() {
        return Err(unserved(src, field, "nothing"));
    }
    Ok(Value::Object(args))
}

fn kept(raw: &[Value], fields: &[String], veil: &Veil) -> Vec<Map<String, Value>> {
    raw.iter().filter(|x| !unseen(x, veil)).filter_map(|x| hits::clean(x, fields)).collect()
}

/// A live source's sections as the query's: a lone `$about` ranks everything into `exact`, the
/// string form keeps the source's own two sections, and a `$contains` keeps its exact matches.
/// A bare `hits` list is a text match, so it counts as `exact`.
pub fn live_answer(raw: &Value, req: &Request, fields: &[String]) -> Result<Value, String> {
    let Value::Object(m) = raw else {
        return Err("the answer is not an object".into());
    };
    let (exact, related) = match m.get("hits") {
        Some(Value::Array(h)) if !hits::SECTIONS.iter().any(|s| m.contains_key(*s)) => (h.clone(), Vec::new()),
        _ => (hits::flat(m.get("exact")), hits::flat(m.get("related"))),
    };
    let mut exact = kept(&exact, fields, &req.veil);
    let mut related = kept(&related, fields, &req.veil);
    for hit in exact.iter_mut().chain(related.iter_mut()) {
        if let Some(ts) = hit.get("ts").cloned() {
            hit.insert("ts".into(), json!(crate::store::normalize_ts(&ts)));
        }
    }
    let text = req.text.as_ref().expect("live_args refused a query without text");
    let list = |v: Vec<Map<String, Value>>| Value::Array(v.into_iter().map(Value::Object).collect());
    if text.about.is_some() && !text.under_or {
        return Ok(json!({"exact": list(merge::fuse(&[exact, related], req.max))}));
    }
    exact.truncate(req.max);
    if text.about.is_some() {
        let shown: std::collections::HashSet<String> = exact.iter().filter_map(|h| h.get("id").map(py_str)).collect();
        related.retain(|h| !h.get("id").map(py_str).is_some_and(|i| shown.contains(&i)));
        related.truncate(req.max);
        return Ok(json!({"exact": list(exact), "related": list(related)}));
    }
    Ok(json!({"exact": list(exact)}))
}
