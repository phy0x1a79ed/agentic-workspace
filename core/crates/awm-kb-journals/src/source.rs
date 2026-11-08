//! The `journals` search source: what search asks in process, and what the `journal_query`
//! and `journal_get` verbs answer over the hub.
//!
//! search ranks threads, as the Python archive did: a thread's first post with a body, tagged
//! journal, message or precedent and not folded, holds the thread's text. A filter that names a
//! kind tag, such as `brief`, lifts the kind clause. `journal_query` ranks posts: every post
//! with a body, comments included, so a caller such as scopes finds the brief in force.
//!
//! The `tag` field holds tag ids. A filter names tags, and the marks name projects, so both are
//! turned into ids here. Every answer applies the caller's veil and says so with `veiled: true`.

use std::collections::HashSet;

use awm_index::grammar::{parse_source, parse_text, SourceFilter, Text};
use awm_index::sql::Veil;
use awm_index::{Answer, Fields};
use awm_kb_source::{Ask as SourceAsk, Source, SourceError};
use awm_pycompat::pystr::norm;
use futures_util::future::BoxFuture;
use serde_json::{json, Map, Value};

use crate::posts::{and, has_tag, Names};
use crate::schema::{FOLDED, INDEXED_TAGS, KIND_TAGS, MIGRATED_PREFIX};
use crate::{Fail, Journals, Result};

/// What an answer ranks.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    /// Thread first posts, as search and `fetch` with a query rank them.
    Threads,
    /// Every post with a body, as `journal_query` ranks them.
    Posts,
}

fn tag_value(v: &Value, ids: &dyn Fn(&str) -> Option<String>, seen: &mut Vec<String>) -> Value {
    match v {
        Value::String(s) => match ids(s) {
            Some(id) => {
                seen.push(id.clone());
                Value::String(id)
            }
            None => v.clone(),
        },
        Value::Array(a) => Value::Array(a.iter().map(|x| tag_value(x, ids, seen)).collect()),
        Value::Object(m) => Value::Object(m.iter().map(|(k, x)| (k.clone(), tag_value(x, ids, seen))).collect()),
        other => other.clone(),
    }
}

/// The filter with each tag name turned into its id; returns the ids it named and whether it
/// names `tag` at all.
fn tag_ids(filter: &Map<String, Value>, ids: &dyn Fn(&str) -> Option<String>, seen: &mut Vec<String>, named: &mut bool) -> Map<String, Value> {
    filter
        .iter()
        .map(|(k, v)| {
            let v = match k.as_str() {
                "tag" => {
                    *named = true;
                    tag_value(v, ids, seen)
                }
                "$and" | "$or" | "$nor" => Value::Array(
                    v.as_array()
                        .into_iter()
                        .flatten()
                        .map(|x| match x {
                            Value::Object(m) => Value::Object(tag_ids(m, ids, seen, named)),
                            o => o.clone(),
                        })
                        .collect(),
                ),
                _ => v.clone(),
            };
            (k.clone(), v)
        })
        .collect()
}

fn kind(id: &str) -> bool {
    KIND_TAGS.contains(&id) || id.starts_with(MIGRATED_PREFIX)
}

fn any_tag(ids: &[&str]) -> String {
    format!("({})", ids.iter().map(|t| has_tag(t)).collect::<Vec<_>>().join(" OR "))
}

impl Journals {
    /// The caller's veil with the tag clause as ids: each tag named for a marked project.
    fn veil_ids(&self, veil: &Veil, names: &Names) -> Veil {
        match veil {
            Veil::Marks { marks, tags: None } => {
                let projects: HashSet<String> = marks.iter().map(|m| norm(&m.project)).collect();
                let mut ids: Vec<String> = names.iter().filter(|(_, n)| projects.contains(&norm(n))).map(|(i, _)| i.clone()).collect();
                ids.sort();
                Veil::Marks { marks: marks.clone(), tags: Some(ids) }
            }
            other => other.clone(),
        }
    }

    /// `{exact, related?}` for one ask of this source.
    pub async fn answer(&self, text: Option<&Text>, source: &SourceFilter, veil: &Veil, max: usize, vector: Option<Vec<f32>>, scope: Scope) -> Result<Answer> {
        let names = self.tag_names()?;
        let lower: std::collections::HashMap<String, String> = names.iter().map(|(i, n)| (n.to_lowercase(), i.clone())).collect();
        let lookup = |n: &str| lower.get(&norm(n)).cloned();
        let (mut seen, mut named) = (Vec::new(), false);
        let filter = tag_ids(&source.filter, &lookup, &mut seen, &mut named);
        let source = SourceFilter { filter, sort: source.sort.clone() };
        let veil = self.veil_ids(veil, &names);
        let vector = match (vector, text.and_then(|t| t.about.as_deref()), self.embedder()) {
            (Some(v), _, _) => Some(v),
            (None, Some(about), Some(e)) => e.encode(&[about]).ok().and_then(|mut v| v.pop()),
            _ => None,
        };
        let mut ask = awm_index::Ask::new(&self.index.spec.fields, text, &source, &veil, max, vector).map_err(|e| Fail::value(e.0))?;
        let mut parts = vec![ask.cond.clone(), format!("NOT {}", has_tag(FOLDED))];
        let mut entry_only = vec!["body != ''".to_string()];
        match scope {
            Scope::Threads => {
                entry_only.push("thread IS NULL".into());
                if !seen.iter().any(|t| kind(t)) {
                    parts.push(any_tag(&INDEXED_TAGS));
                }
            }
            Scope::Posts => {
                if !named {
                    parts.push(any_tag(&INDEXED_TAGS));
                }
            }
        }
        ask.chunk_cond = Some(and(&parts));
        parts.extend(entry_only);
        ask.cond = and(&parts);
        Ok(self.index.answer(&ask).await?)
    }

    /// The `journal_query` verb: the search grammar's `query` text and the journals filter.
    pub async fn query_verb(&self, args: &Value, veil: &Veil) -> Result<Value> {
        let text = match args.get("query") {
            None | Some(Value::Null) => None,
            Some(v) => Some(parse_text(v).map_err(|e| Fail::value(e.0))?),
        };
        let filter = args.get("filter").cloned().unwrap_or(json!({}));
        let source = parse_source(&filter, &self.index.spec.fields, "journals", "query.journals").map_err(|e| Fail::value(e.0))?;
        let max = match args.get("max") {
            None | Some(Value::Null) => awm_index::grammar::DEFAULT_MAX,
            Some(v) => awm_index::grammar::parse_max(v).map_err(|e| Fail::value(e.0))?,
        };
        let answer = self.answer(text.as_ref(), &source, veil, max, None, Scope::Posts).await?;
        let mut out = answer.to_json();
        let ids: Vec<String> = answer.exact.iter().chain(answer.related.iter().flatten()).filter_map(|h| h["id"].as_str().map(str::to_string)).collect();
        let posts = self.posts_by_id(&ids).await?;
        for section in ["exact", "related"] {
            if let Some(hits) = out.get_mut(section).and_then(Value::as_array_mut) {
                for h in hits {
                    if let Some(p) = h["id"].as_str().and_then(|i| posts.get(i)) {
                        h["project"] = json!(p.project);
                        h["scope"] = json!(p.scope);
                    }
                }
            }
        }
        out["veiled"] = json!(true);
        Ok(out)
    }

    /// One post as a search document, or None when it is absent or the veil covers it.
    pub async fn document(&self, id: &str, veil: &Veil, session: Option<&str>) -> Result<Option<Map<String, Value>>> {
        let names = self.tag_names()?;
        let cond = awm_index::sql::veil_sql(&self.index.spec.fields, &self.veil_ids(veil, &names));
        let Some(doc) = self.index.document(id, &cond).await? else { return Ok(None) };
        let Some(post) = self.get_post(id).await? else { return Ok(None) };
        if session.is_some() {
            self.record_reads(session, std::slice::from_ref(&post))?;
        }
        let mut out = Map::new();
        out.insert("id".into(), json!(id));
        for k in ["title", "text", "ts", "project", "scope", "replies_to"] {
            if let Some(v) = doc.get(k).filter(|v| !(v.is_null() || v.as_str() == Some("") || v.as_array().is_some_and(Vec::is_empty))) {
                out.insert(k.into(), v.clone());
            }
        }
        let tags = post.tag_names(&names);
        if !tags.is_empty() {
            out.insert("tag".into(), json!(tags));
        }
        Ok(Some(out))
    }

    /// Counts for `search status`.
    pub async fn source_status(&self) -> Value {
        let posts = self.index.count(None).await.unwrap_or(0);
        let waiting = self.index.count(Some("_chunked IS NULL")).await.unwrap_or(0);
        let sections: Vec<String> = self.index.sections().iter().map(|s| s.key.clone()).collect();
        json!({"posts": posts, "waiting": waiting, "sections": sections, "cutover": self.cutover.to_json()})
    }
}

impl Source for Journals {
    fn name(&self) -> &str {
        crate::posts::SOURCE
    }

    fn default(&self) -> bool {
        true
    }

    fn fields(&self) -> (&Fields, &[String]) {
        (&self.index.spec.fields, &self.index.spec.hit)
    }

    fn takes_vectors(&self) -> bool {
        true
    }

    fn query(&self, ask: SourceAsk) -> BoxFuture<'_, Result<Value, SourceError>> {
        Box::pin(async move {
            let answer = self
                .answer(ask.text.as_ref(), &ask.source, &ask.veil, ask.max, ask.vector, Scope::Threads)
                .await
                .map_err(|e| SourceError(e.message))?;
            let mut out = answer.to_json();
            out["veiled"] = json!(true);
            Ok(out)
        })
    }

    fn get<'a>(&'a self, id: &'a str, veil: &'a Veil, session: Option<&'a str>) -> BoxFuture<'a, Result<Option<Map<String, Value>>, SourceError>> {
        Box::pin(async move { self.document(id, veil, session).await.map_err(|e| SourceError(e.message)) })
    }

    fn status(&self) -> BoxFuture<'_, Value> {
        Box::pin(self.source_status())
    }
}
