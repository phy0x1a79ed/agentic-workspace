//! One source's answer from its sections, a port of search's `lance_query` read half.
//!
//! A ranking fuses a vector search over chunks with any-word BM25 over entries, each min-max
//! normalised within one section, because BM25 statistics differ between sections. The
//! sections' fused lists then merge by score.

use crate::cells::{self, Cell, Row};
use crate::fields::{Class, Fields};
use crate::fts::{about_query, compile_tree, Tree};
use crate::fuse::{fuse, ranked as by_score, ALPHA, FUSION_DEPTH};
use crate::grammar::{SourceFilter, Text};
use crate::sql::{and, id_list, quote, where_sql, Veil};
use crate::store::{Index, Section};
use crate::{QueryUnserved, Result};
use awm_pycompat::repr::str_repr;
use futures::TryStreamExt;
use indexmap::IndexMap;
use lancedb::arrow::arrow_array::RecordBatch;
use lancedb::index::scalar::{FtsQuery, FullTextSearchQuery};
use lancedb::query::{ExecutableQuery, HasQuery, QueryBase, Select};
use lancedb::DistanceType;
use serde_json::{Map, Value};
use std::collections::HashSet;

/// Probes and refine factor of a vector search over an IVF index.
pub const NPROBES: usize = 20;
pub const REFINE: u32 = 0;
/// How many phrase matches an `$about` ranks, or a sort orders, at most.
pub const CANDIDATES: usize = 1000;
const BATCH: usize = 500;

/// What one source is asked: the prefilter, the compiled text, the sort, the cap and the
/// query vector, which the caller embeds once for every source.
#[derive(Debug, Clone)]
pub struct Ask {
    pub cond: String,
    pub tree: Option<Tree>,
    pub about: Option<String>,
    pub under_or: bool,
    pub sort: Option<(String, bool)>,
    pub limit: usize,
    pub vector: Option<Vec<f32>>,
}

impl Ask {
    /// The ask for a parsed text and source filter, under the caller's veil.
    pub fn new(
        fields: &Fields,
        text: Option<&Text>,
        source: &SourceFilter,
        veil: &Veil,
        limit: usize,
        vector: Option<Vec<f32>>,
    ) -> Result<Ask, QueryUnserved> {
        let content = fields.content();
        Ok(Ask {
            cond: where_sql(fields, &source.filter, veil)?,
            tree: text.and_then(|t| t.contains.as_ref()).map(|n| compile_tree(n, &content)),
            about: text.and_then(|t| t.about.clone()),
            under_or: text.is_some_and(|t| t.under_or),
            sort: source.sort.clone(),
            limit,
            vector,
        })
    }
}

/// `{exact, related?}`, each a list of hits.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Answer {
    pub exact: Vec<Map<String, Value>>,
    pub related: Option<Vec<Map<String, Value>>>,
}

impl Answer {
    pub fn to_json(&self) -> Value {
        let mut m = Map::new();
        m.insert("exact".into(), Value::Array(self.exact.iter().cloned().map(Value::Object).collect()));
        if let Some(r) = &self.related {
            m.insert("related".into(), Value::Array(r.iter().cloned().map(Value::Object).collect()));
        }
        Value::Object(m)
    }
}

async fn collect(q: impl ExecutableQuery) -> Result<Vec<Row>> {
    let batches: Vec<RecordBatch> = q.execute().await?.try_collect().await?;
    Ok(cells::rows(&batches))
}

fn score(r: &Row, name: &str) -> f64 {
    match r.get(name) {
        Some(Cell::Float(f)) => *f,
        _ => 0.0,
    }
}

impl Index {
    fn class(&self, field: &str) -> Option<Class> {
        self.spec.fields.get(field).map(|f| f.class)
    }

    async fn fts_rows(&self, sec: &Section, query: &FtsQuery, cond: &str, limit: Option<usize>, columns: &[&str]) -> Result<Vec<Row>> {
        if sec.entries.count_rows(None).await? == 0 {
            return Ok(vec![]);
        }
        let mut cols = vec!["id", "_score"];
        cols.extend_from_slice(columns);
        let mut q = sec.entries.query().full_text_search(FullTextSearchQuery::new_query(query.clone())).select(Select::columns(&cols));
        q.mut_query().limit = limit;
        if cond != "true" {
            q = q.only_if(cond);
        }
        collect(q).await
    }

    /// `_fts` over every section, best first. A single section keeps the engine's own order.
    async fn fts(&self, query: &FtsQuery, cond: &str, limit: Option<usize>) -> Result<Vec<(String, f64)>> {
        let mut out = Vec::new();
        for sec in self.sections() {
            out.extend(self.fts_rows(&sec, query, cond, limit, &[]).await?.iter().map(|r| (cells::text(r, "id"), score(r, "_score"))));
        }
        out.sort_by(|a, b| b.1.total_cmp(&a.1));
        if let Some(n) = limit {
            out.truncate(n);
        }
        Ok(out)
    }

    /// The `ids` that the tree matches.
    pub async fn matching(&self, tree: &Tree, ids: &[String]) -> Result<HashSet<String>> {
        let mut found = HashSet::new();
        for batch in ids.chunks(BATCH) {
            let hit: HashSet<String> =
                self.fts(&tree.query, &id_list(batch), Some(batch.len())).await?.into_iter().map(|(k, _)| k).collect();
            for id in batch {
                if hit.contains(id) != tree.negated {
                    found.insert(id.clone());
                }
            }
        }
        Ok(found)
    }

    async fn ids(&self, cond: &str, limit: Option<usize>) -> Result<Vec<String>> {
        let mut out = Vec::new();
        for sec in self.sections() {
            let mut q = sec.entries.query().select(Select::columns(&["id"]));
            if let Some(n) = limit {
                if out.len() >= n {
                    break;
                }
                q = q.limit(n - out.len());
            }
            if cond != "true" {
                q = q.only_if(cond);
            }
            out.extend(collect(q).await?.iter().map(|r| cells::text(r, "id")));
        }
        Ok(out)
    }

    /// The ids of `rows` ordered by `field`; a row without it goes last.
    fn by_field(&self, rows: &[Row], field: &str, descending: bool) -> Vec<String> {
        let categorical = self.class(field) == Some(Class::Categorical);
        let key = |r: &Row| -> Option<Cell> {
            match r.get(field) {
                None | Some(Cell::Null) => None,
                Some(Cell::Texts(v)) if categorical => v.first().cloned().map(Cell::Text),
                Some(c) => Some(c.clone()),
            }
        };
        let mut present: Vec<(Cell, String)> = Vec::new();
        let mut absent = Vec::new();
        for r in rows {
            match key(r) {
                Some(k) => present.push((k, cells::text(r, "id"))),
                None => absent.push(cells::text(r, "id")),
            }
        }
        present.sort_by(|a, b| {
            let o = compare(&a.0, &b.0);
            if descending {
                o.reverse()
            } else {
                o
            }
        });
        present.into_iter().map(|(_, id)| id).chain(absent).collect()
    }

    /// Every id passing `cond` (and in `within`), ordered by `field`.
    async fn sorted_ids(&self, cond: &str, field: &str, descending: bool, within: Option<&[String]>) -> Result<Vec<String>> {
        let mut rows = Vec::new();
        let conds: Vec<String> = match within {
            None => vec![cond.to_string()],
            Some(ids) => ids.chunks(BATCH).map(|b| and(&[cond, &id_list(b)])).collect(),
        };
        for sec in self.sections() {
            for c in &conds {
                let mut q = sec.entries.query().select(Select::columns(&["id", field]));
                if c != "true" {
                    q = q.only_if(c);
                }
                rows.extend(collect(q).await?);
            }
        }
        Ok(self.by_field(&rows, field, descending))
    }

    /// `_first`: the first `limit` ids that pass `cond` and the tree, in `order` when given.
    async fn first(&self, tree: Option<&Tree>, cond: &str, order: Option<Vec<String>>, limit: usize) -> Result<Vec<String>> {
        match (tree, &order) {
            (Some(t), None) if !t.negated => return Ok(self.fts(&t.query, cond, Some(limit)).await?.into_iter().map(|(k, _)| k).collect()),
            (None, None) => return self.ids(cond, Some(limit)).await,
            _ => {}
        }
        let source = match order {
            Some(o) => o,
            None => self.ids(cond, None).await?,
        };
        let mut out = Vec::new();
        for batch in source.chunks(BATCH) {
            if out.len() >= limit {
                break;
            }
            let kept: HashSet<String> = match tree {
                Some(t) => self.matching(t, batch).await?,
                None => batch.iter().cloned().collect(),
            };
            out.extend(batch.iter().filter(|i| kept.contains(*i)).cloned());
        }
        out.truncate(limit);
        Ok(out)
    }

    /// Each entry's best chunk similarity to the query vector, for the nearest `depth` entries.
    async fn dense(&self, sec: &Section, vector: &[f32], cond: &str, depth: usize) -> Result<IndexMap<String, f64>> {
        if sec.chunks.count_rows(None).await? == 0 {
            return Ok(IndexMap::new());
        }
        let mut q = sec
            .chunks
            .query()
            .nearest_to(vector)?
            .distance_type(DistanceType::Cosine)
            .nprobes(NPROBES)?
            .select(Select::columns(&["id", "_distance"]))
            .limit(depth * 3);
        if REFINE > 0 {
            q = q.refine_factor(REFINE);
        }
        if cond != "true" {
            q = q.only_if(cond);
        }
        let mut best = IndexMap::new();
        for r in collect(q).await? {
            best.entry(cells::text(&r, "id")).or_insert(1.0 - score(&r, "_distance"));
        }
        best.truncate(depth);
        Ok(best)
    }

    /// `ranked`: ids passing `cond` (and in `within`), best first by the `$about`.
    pub async fn ranked(&self, vector: Option<&[f32]>, about: &str, cond: &str, depth: usize, within: Option<&[String]>) -> Result<Vec<String>> {
        let cond = match within {
            Some([]) => return Ok(vec![]),
            Some(ids) => and(&[cond, &id_list(ids)]),
            None => cond.to_string(),
        };
        let reach = depth.max(FUSION_DEPTH);
        let content = self.spec.fields.content();
        let keyword = about_query(about, &content);
        let mut all: IndexMap<String, f64> = IndexMap::new();
        for sec in self.sections() {
            let dense = match vector {
                Some(v) => self.dense(&sec, v, &cond, reach).await?,
                None => IndexMap::new(),
            };
            let bm25: IndexMap<String, f64> = match &keyword {
                Some(q) => self.fts_rows(&sec, q, &cond, Some(reach), &[]).await?.iter().map(|r| (cells::text(r, "id"), score(r, "_score"))).collect(),
                None => IndexMap::new(),
            };
            all.extend(fuse(&dense, &bm25, ALPHA, reach));
        }
        let mut out = by_score(&all);
        out.truncate(depth);
        Ok(out)
    }

    /// `{id, <hit fields>}` for `ids`, in their order.
    pub async fn hits(&self, ids: &[String]) -> Result<Vec<Map<String, Value>>> {
        if ids.is_empty() {
            return Ok(vec![]);
        }
        let mut cols = vec!["id"];
        cols.extend(self.spec.hit.iter().map(String::as_str));
        let mut by_id = std::collections::HashMap::new();
        for sec in self.sections() {
            for batch in ids.chunks(BATCH) {
                let q = sec.entries.query().only_if(id_list(batch)).select(Select::columns(&cols));
                for r in collect(q).await? {
                    by_id.insert(cells::text(&r, "id"), cells::to_map(&r, &[]));
                }
            }
        }
        Ok(ids.iter().filter_map(|i| by_id.remove(i)).collect())
    }

    /// One entry whole, or `None` when it is absent or `cond` excludes it.
    pub async fn document(&self, id: &str, cond: &str) -> Result<Option<Map<String, Value>>> {
        let c = and(&[&format!("id = {}", quote(id)), cond]);
        for sec in self.sections() {
            if let Some(r) = collect(sec.entries.query().only_if(&c).limit(1)).await?.first() {
                return Ok(Some(cells::to_map(r, &["_chunked"])));
            }
        }
        Ok(None)
    }

    async fn candidates(&self, ask: &Ask) -> Result<Vec<String>> {
        match &ask.tree {
            Some(t) => self.first(Some(t), &ask.cond, None, CANDIDATES).await,
            None => Ok(vec![]),
        }
    }

    /// `_ordered`: without `$about`, the matches, ordered by `$sort` when given.
    async fn ordered(&self, ask: &Ask) -> Result<Vec<String>> {
        let Some((field, descending)) = &ask.sort else {
            return self.first(ask.tree.as_ref(), &ask.cond, None, ask.limit).await;
        };
        let positive = ask.tree.as_ref().filter(|t| !t.negated);
        if self.class(field) == Some(Class::Content) {
            let Some(t) = positive else {
                return Err(QueryUnserved(format!(
                    "$sort on the content field {} needs a $contains or $about to score by",
                    str_repr(field)
                ))
                .into());
            };
            let ids: Vec<String> =
                self.fts(&t.query, &ask.cond, descending.then_some(ask.limit)).await?.into_iter().map(|(k, _)| k).collect();
            return Ok(if *descending { ids.into_iter().take(ask.limit).collect() } else { ids.into_iter().rev().take(ask.limit).collect() });
        }
        if let Some(t) = positive {
            let mut rows = Vec::new();
            for sec in self.sections() {
                rows.extend(self.fts_rows(&sec, &t.query, &ask.cond, None, &[field.as_str()]).await?);
            }
            rows.sort_by(|a, b| score(b, "_score").total_cmp(&score(a, "_score")));
            let mut ids = self.by_field(&rows, field, *descending);
            ids.truncate(ask.limit);
            return Ok(ids);
        }
        let order = self.sorted_ids(&ask.cond, field, *descending, None).await?;
        self.first(ask.tree.as_ref(), &ask.cond, Some(order), ask.limit).await
    }

    /// `_resorted`: with `$about`, a `$sort` on a non-content field reorders the top hits; on
    /// content it keeps relevance.
    async fn resorted(&self, ids: Vec<String>, sort: &Option<(String, bool)>) -> Result<Vec<String>> {
        match sort {
            None => Ok(ids),
            Some((field, desc)) if ids.is_empty() || self.class(field) == Some(Class::Content) => {
                Ok(if *desc { ids } else { ids.into_iter().rev().collect() })
            }
            Some((field, desc)) => self.sorted_ids("true", field, *desc, Some(&ids)).await,
        }
    }

    /// `answer`: `{exact, related?}` for one source.
    pub async fn answer(&self, ask: &Ask) -> Result<Answer> {
        let Some(about) = &ask.about else {
            return Ok(Answer { exact: self.hits(&self.ordered(ask).await?).await?, related: None });
        };
        let vector = ask.vector.as_deref();
        let exact = match &ask.tree {
            None => self.ranked(vector, about, &ask.cond, ask.limit, None).await?,
            Some(_) => {
                let within = self.candidates(ask).await?;
                self.ranked(vector, about, &ask.cond, ask.limit, Some(&within)).await?
            }
        };
        let mut out = Answer { exact: self.hits(&self.resorted(exact, &ask.sort).await?).await?, related: None };
        if ask.under_or {
            let near = self.ranked(vector, about, &ask.cond, ask.limit * 3, None).await?;
            let inside: HashSet<String> = match &ask.tree {
                Some(t) => self.matching(t, &near).await?,
                None => near.iter().cloned().collect(),
            };
            let related: Vec<String> = near.into_iter().filter(|i| !inside.contains(i)).take(ask.limit).collect();
            out.related = Some(self.hits(&self.resorted(related, &ask.sort).await?).await?);
        }
        Ok(out)
    }
}

fn compare(a: &Cell, b: &Cell) -> std::cmp::Ordering {
    use std::cmp::Ordering::Equal;
    match (a, b) {
        (Cell::Text(x), Cell::Text(y)) => x.cmp(y),
        (Cell::Float(x), Cell::Float(y)) => x.partial_cmp(y).unwrap_or(Equal),
        (Cell::Int(x), Cell::Int(y)) | (Cell::Time(x), Cell::Time(y)) => x.cmp(y),
        _ => Equal,
    }
}
