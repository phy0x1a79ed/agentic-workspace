//! A source's tables in LanceDB, in yearly sections, with writes that never lose an entry and
//! maintenance that the writes themselves start.
//!
//! A section holds `<table>` with one row per entry, and `<table>__chunks` with one row per
//! chunk of the entry's content: the chunk's vector and a copy of every non-content field, so a
//! vector search filters before it ranks. An entry's `_chunked` holds the content hash its chunks
//! were built from, or NULL while they wait for the model.

use crate::cells::{self, coerce, parse_ts, Cell, Row};
pub use crate::cells::Kind;
use crate::fields::{Class, Fields};
use crate::sql::id_list;
use crate::{IndexError, Result};
use awm_embed::chunk::{chunk, content_hash, header, text_hash, CHUNK_TOKENS};
use awm_embed::{Document, Embedder};
use awm_pycompat::pystr::strip;
use awm_pycompat::time::{date_iso, DateTime};
use futures::TryStreamExt;
use indexmap::IndexMap;
use lancedb::arrow::arrow_array::{RecordBatch, RecordBatchIterator, RecordBatchReader};
use lancedb::index::scalar::{BTreeIndexBuilder, FtsIndexBuilder, LabelListIndexBuilder};
use lancedb::index::vector::IvfRqIndexBuilder;
use lancedb::index::Index as LanceIndex;
use lancedb::query::{ExecutableQuery, QueryBase, Select};
use lancedb::table::{CompactionOptions, OptimizeAction, OptimizeOptions};
use lancedb::{Connection, DistanceType, ObjectStoreRegistry, Session, Table};
use serde_json::{Map, Value};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex as StdMutex, RwLock};
use tokio::sync::Mutex;
use tokio::task::JoinHandle;

pub const DIM: i32 = 384;
pub const CHUNKS: &str = "__chunks";
/// Below this many chunks a flat scan is exact and small, so no vector index is built.
pub const IVF_MIN_ROWS: usize = 5000;
/// Delta indexes that may stack up before a merge folds them.
pub const MERGE_AT: u32 = 8;
/// Rows written outside the indexes that start maintenance of a section.
pub const MAINTAIN_AT: usize = 500;
/// How long a superseded version stays readable after a compaction.
pub const KEEP_VERSIONS_S: i64 = 60;
pub const UNDATED: &str = "undated";
const CHUNKED: &str = "_chunked";
const BATCH: usize = 500;
const INDEX_CACHE_BYTES: usize = 64 << 20;
const METADATA_CACHE_BYTES: usize = 32 << 20;

/// What a source stores and how it is searched.
#[derive(Debug, Clone, PartialEq)]
pub struct Spec {
    pub name: String,
    pub fields: Fields,
    /// The fields a hit shows, after `id`.
    pub hit: Vec<String>,
    /// Columns stored and returned whole but never searched.
    pub extra: Vec<(String, Kind)>,
    /// The datetime field whose year picks an entry's section, or `None` for one table.
    pub section_by: Option<String>,
}

impl Spec {
    /// `lead`: the content field that heads each chunk.
    pub fn head(&self) -> Result<&str> {
        let content = self.fields.content();
        self.hit
            .iter()
            .map(String::as_str)
            .find(|h| content.contains(h))
            .or_else(|| content.first().copied())
            .ok_or_else(|| IndexError::Layout(format!("source {} declares no content field", self.name)))
    }

    fn kind(class: Class) -> Kind {
        match class {
            Class::Content => Kind::Text,
            Class::Categorical => Kind::Texts,
            Class::Numeric => Kind::Float,
            Class::Datetime => Kind::Time,
        }
    }

    pub fn entry_cols(&self) -> Vec<(String, Kind)> {
        let mut cols = vec![("id".to_string(), Kind::Text)];
        cols.extend(self.fields.iter().map(|f| (f.name.clone(), Self::kind(f.class))));
        cols.extend(self.extra.iter().cloned());
        cols.push((CHUNKED.into(), Kind::Text));
        cols
    }

    fn kept(&self) -> Vec<(String, Kind)> {
        self.fields.iter().filter(|f| f.class != Class::Content).map(|f| (f.name.clone(), Self::kind(f.class))).collect()
    }

    pub fn chunk_cols(&self) -> Vec<(String, Kind)> {
        let mut cols = vec![("id".to_string(), Kind::Text), ("chunk".into(), Kind::Int32), ("hash".into(), Kind::Text)];
        cols.extend(self.kept());
        cols.push(("vector".into(), Kind::Vector(DIM)));
        cols
    }

    /// The section an entry dated `us` belongs to.
    pub fn section_of(&self, us: Option<i64>) -> String {
        match (&self.section_by, us) {
            (None, _) => String::new(),
            (Some(_), Some(us)) => DateTime::from_utc_us(us).year.to_string(),
            (Some(_), None) => UNDATED.into(),
        }
    }

    pub fn table(&self, section: &str) -> String {
        if section.is_empty() {
            self.name.clone()
        } else {
            format!("{}_{section}", self.name)
        }
    }

    /// The section key of a table name, when the table is one of this source's entry tables.
    fn section_key(&self, table: &str) -> Option<String> {
        if table.ends_with(CHUNKS) {
            return None;
        }
        match &self.section_by {
            None => (table == self.name).then(String::new),
            Some(_) => {
                let key = table.strip_prefix(&self.name)?.strip_prefix('_')?;
                (key == UNDATED || (key.len() == 4 && key.bytes().all(|b| b.is_ascii_digit()))).then(|| key.to_string())
            }
        }
    }
}

/// The LanceDB connection a node's sources share, with capped caches.
#[derive(Clone)]
pub struct Store {
    pub path: PathBuf,
    db: Connection,
}

impl Store {
    pub async fn open(path: &Path) -> Result<Store> {
        std::fs::create_dir_all(path)?;
        let session = Arc::new(Session::new(INDEX_CACHE_BYTES, METADATA_CACHE_BYTES, Arc::new(ObjectStoreRegistry::default())));
        let db = lancedb::connect(&path.to_string_lossy())
            .read_consistency_interval(std::time::Duration::ZERO)
            .session(session)
            .execute()
            .await?;
        Ok(Store { path: path.to_path_buf(), db })
    }

    /// The connection itself, for a source's own plain tables beside its sections.
    pub fn connection(&self) -> &Connection {
        &self.db
    }

    pub async fn table_names(&self) -> Result<Vec<String>> {
        Ok(self.db.table_names().execute().await?)
    }

    /// Open a source's sections, creating none until a write needs one.
    pub async fn index(&self, spec: Spec) -> Result<Index> {
        spec.head()?;
        let names = self.table_names().await?;
        let mut sections = BTreeMap::new();
        for name in &names {
            if let Some(key) = spec.section_key(name) {
                let sec = self.open_section(&spec, &key, &names).await?;
                sections.insert(key, Arc::new(sec));
            }
        }
        Ok(Index {
            store: self.clone(),
            spec: Arc::new(spec),
            sections: RwLock::new(sections),
            write: Arc::new(Mutex::new(())),
            work: StdMutex::new(Vec::new()),
        })
    }

    async fn open_section(&self, spec: &Spec, key: &str, names: &[String]) -> Result<Section> {
        let table = spec.table(key);
        let entries = self.db.open_table(&table).execute().await?;
        check_layout(&entries, &spec.entry_cols()).await?;
        let chunk_table = format!("{table}{CHUNKS}");
        let chunks = if names.contains(&chunk_table) {
            let t = self.db.open_table(&chunk_table).execute().await?;
            check_layout(&t, &spec.chunk_cols()).await?;
            t
        } else {
            self.db.create_empty_table(&chunk_table, cells::schema(&spec.chunk_cols())).execute().await?
        };
        let sec = Section::new(key, entries, chunks);
        sec.pending.store(unindexed(&sec.entries).await?, Ordering::SeqCst);
        sec.pending_chunks.store(unindexed(&sec.chunks).await?, Ordering::SeqCst);
        if sec.entries.count_rows(None).await? > 0 {
            ensure_indexes(spec, &sec, &self.path).await?;
        }
        Ok(sec)
    }

    /// Remove every table of a source.
    pub async fn drop(&self, spec: &Spec) -> Result<()> {
        for name in self.table_names().await? {
            let base = name.strip_suffix(CHUNKS).unwrap_or(&name);
            if spec.section_key(base).is_some() {
                self.db.drop_table(&name, &[]).await?;
                let _ = std::fs::remove_file(trained_path(&self.path, &name));
            }
        }
        Ok(())
    }
}

async fn check_layout(table: &Table, cols: &[(String, Kind)]) -> Result<()> {
    let schema = table.schema().await?;
    for (name, kind) in cols {
        match schema.field_with_name(name) {
            Ok(f) if f.data_type() == &kind.data_type() => {}
            _ => {
                return Err(IndexError::Layout(format!(
                    "table {} has no column {name} of {:?}; drop the source and write it again",
                    table.name(),
                    kind.data_type()
                )))
            }
        }
    }
    Ok(())
}

async fn unindexed(table: &Table) -> Result<usize> {
    let mut most = None;
    for i in table.list_indices().await? {
        if let Some(s) = table.index_stats(&i.name).await? {
            most = Some(most.unwrap_or(0).max(s.num_unindexed_rows));
        }
    }
    match most {
        Some(n) => Ok(n),
        None => Ok(table.count_rows(None).await?),
    }
}

/// One section's two tables and the rows written to them since they were last indexed.
pub struct Section {
    pub key: String,
    pub entries: Table,
    pub chunks: Table,
    pending: AtomicUsize,
    pending_chunks: AtomicUsize,
    indexed: AtomicBool,
}

impl Section {
    fn new(key: &str, entries: Table, chunks: Table) -> Section {
        Section {
            key: key.into(),
            entries,
            chunks,
            pending: AtomicUsize::new(0),
            pending_chunks: AtomicUsize::new(0),
            indexed: AtomicBool::new(false),
        }
    }

    /// Rows written outside the indexes: entries and chunks.
    pub fn pending(&self) -> (usize, usize) {
        (self.pending.load(Ordering::SeqCst), self.pending_chunks.load(Ordering::SeqCst))
    }
}

/// What one write did.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Written {
    pub entries: usize,
    /// Texts the embedder encoded; reused vectors are not counted.
    pub embedded: usize,
    /// Entries whose chunks wait for the model.
    pub waiting: usize,
    /// Why the embedder failed, leaving its entries waiting.
    pub error: Option<String>,
}

/// One source's sections.
pub struct Index {
    store: Store,
    pub spec: Arc<Spec>,
    sections: RwLock<BTreeMap<String, Arc<Section>>>,
    write: Arc<Mutex<()>>,
    work: StdMutex<Vec<JoinHandle<()>>>,
}

/// An entry as written: its cells in `entry_cols` order, its date and section.
struct Entry {
    id: String,
    cells: Vec<Cell>,
    section: String,
}

/// What the store held for the ids of a write, before it.
#[derive(Default)]
struct Held {
    section: HashMap<String, String>,
    chunked: HashMap<String, String>,
    chunks: HashMap<String, Vec<(i64, String, Vec<f32>)>>,
}

impl Held {
    fn vector(&self, hash: &str) -> Option<&Vec<f32>> {
        self.chunks.values().flatten().find(|(_, h, _)| h == hash).map(|(_, _, v)| v)
    }
}

async fn collect(q: impl ExecutableQuery) -> Result<Vec<Row>> {
    let batches: Vec<RecordBatch> = q.execute().await?.try_collect().await?;
    Ok(cells::rows(&batches))
}

fn reader(batch: RecordBatch) -> Box<dyn RecordBatchReader + Send> {
    let schema = batch.schema();
    Box::new(RecordBatchIterator::new(vec![Ok(batch)], schema))
}

impl Index {
    /// The sections, newest first, `undated` last.
    pub fn sections(&self) -> Vec<Arc<Section>> {
        let held = self.sections.read().unwrap();
        let mut out: Vec<Arc<Section>> = held.values().cloned().collect();
        out.sort_by(|a, b| match (a.key == UNDATED, b.key == UNDATED) {
            (true, false) => std::cmp::Ordering::Greater,
            (false, true) => std::cmp::Ordering::Less,
            _ => b.key.cmp(&a.key),
        });
        out
    }

    /// Wait for the maintenance that writes have started.
    pub async fn settle(&self) {
        let handles: Vec<JoinHandle<()>> = std::mem::take(&mut *self.work.lock().unwrap());
        for h in handles {
            let _ = h.await;
        }
    }

    pub async fn count(&self, cond: Option<&str>) -> Result<usize> {
        let mut n = 0;
        for sec in self.sections() {
            n += sec.entries.count_rows(cond.filter(|c| *c != "true").map(str::to_string)).await?;
        }
        Ok(n)
    }

    async fn section(&self, key: &str) -> Result<Arc<Section>> {
        if let Some(sec) = self.sections.read().unwrap().get(key) {
            return Ok(sec.clone());
        }
        let table = self.spec.table(key);
        let db = &self.store.db;
        let entries = db.create_empty_table(&table, cells::schema(&self.spec.entry_cols())).execute().await?;
        let chunks = db
            .create_empty_table(format!("{table}{CHUNKS}"), cells::schema(&self.spec.chunk_cols()))
            .execute()
            .await?;
        let sec = Arc::new(Section::new(key, entries, chunks));
        self.sections.write().unwrap().insert(key.to_string(), sec.clone());
        Ok(sec)
    }

    fn entry(&self, row: &Map<String, Value>) -> Option<Entry> {
        let id = match row.get("id")? {
            Value::String(s) => s.clone(),
            Value::Null => return None,
            v => v.to_string(),
        };
        let cols = self.spec.entry_cols();
        let mut cells = Vec::with_capacity(cols.len());
        for (name, kind) in &cols {
            let cell = match name.as_str() {
                "id" => Cell::Text(id.clone()),
                CHUNKED => Cell::Null,
                n => {
                    let c = coerce(*kind, row.get(n));
                    let content = self.spec.fields.get(n).is_some_and(|f| f.class == Class::Content);
                    if content && c == Cell::Null {
                        Cell::Text(String::new())
                    } else {
                        c
                    }
                }
            };
            cells.push(cell);
        }
        let date = self.spec.section_by.as_ref().and_then(|f| parse_ts(row.get(f).unwrap_or(&Value::Null)));
        Some(Entry { section: self.spec.section_of(date), id, cells })
    }

    fn doc(&self, cols: &[(String, Kind)], cells: &[Cell], id: &str) -> Document {
        let get = |name: &str| cols.iter().position(|(n, _)| n == name).map(|i| &cells[i]);
        let head = self.spec.head().unwrap_or_default();
        let title = get(head).and_then(Cell::as_str).unwrap_or("").to_string();
        let body = self
            .spec
            .fields
            .iter()
            .filter(|f| f.class == Class::Content && f.name != head)
            .filter_map(|f| get(&f.name).and_then(Cell::as_str).filter(|s| !s.is_empty()))
            .collect::<Vec<_>>()
            .join("\n\n");
        let date = self
            .spec
            .fields
            .iter()
            .filter(|f| f.class == Class::Datetime)
            .find_map(|f| match get(&f.name) {
                Some(Cell::Time(us)) => Some(date_iso(&DateTime::from_utc_us(*us))),
                _ => None,
            })
            .unwrap_or_default();
        Document { source_id: id.into(), body, title, context: String::new(), date }
    }

    /// `_chunk_texts`: the body's chunks, each under its header, or the title alone.
    fn texts(doc: &Document, embedder: &dyn Embedder) -> Vec<String> {
        let count = |t: &[&str]| embedder.count_tokens(t);
        let parts = chunk(&doc.body, CHUNK_TOKENS, &count);
        if parts.is_empty() {
            let title = strip(&doc.title);
            return if title.is_empty() { vec![] } else { vec![title.to_string()] };
        }
        parts.iter().map(|(sec, text)| format!("{}\n{text}", header(doc, sec))).collect()
    }

    async fn held(&self, ids: &[String]) -> Result<Held> {
        let mut held = Held::default();
        for sec in self.sections() {
            for batch in ids.chunks(BATCH) {
                let cond = id_list(batch);
                let q = sec.entries.query().only_if(&cond).select(Select::columns(&["id", CHUNKED]));
                for r in collect(q).await? {
                    let id = cells::text(&r, "id");
                    if let Some(h) = r.get(CHUNKED).and_then(Cell::as_str) {
                        held.chunked.insert(id.clone(), h.to_string());
                    }
                    held.section.insert(id, sec.key.clone());
                }
                let q = sec.chunks.query().only_if(&cond).select(Select::columns(&["id", "chunk", "hash", "vector"]));
                for r in collect(q).await? {
                    let (Some(Cell::Int(j)), Some(Cell::Vector(v))) = (r.get("chunk"), r.get("vector")) else { continue };
                    held.chunks.entry(cells::text(&r, "id")).or_default().push((*j, cells::text(&r, "hash"), v.clone()));
                }
            }
        }
        for c in held.chunks.values_mut() {
            c.sort_by_key(|(j, _, _)| *j);
        }
        Ok(held)
    }

    async fn delete_in(table: &Table, ids: &[String]) -> Result<()> {
        for batch in ids.chunks(BATCH) {
            table.delete(id_list(batch).as_str()).await?;
        }
        Ok(())
    }

    /// Write entries `{id, <field>: value, <extra>: value}`, replacing any stored under the same id.
    ///
    /// The entry commits first, with its chunks marked waiting. Its chunks follow, reusing every
    /// stored vector of an unchanged text, and only then is it marked chunked. Without an
    /// embedder an entry whose content is unchanged keeps its vectors, and any other waits.
    pub async fn upsert(&self, rows: &[Map<String, Value>], embedder: Option<&dyn Embedder>) -> Result<Written> {
        let mut by_id: IndexMap<String, Entry> = IndexMap::new();
        for r in rows {
            if let Some(e) = self.entry(r) {
                by_id.shift_remove(&e.id);
                by_id.insert(e.id.clone(), e);
            }
        }
        if by_id.is_empty() {
            return Ok(Written::default());
        }
        let _w = self.write.lock().await;
        let ids: Vec<String> = by_id.keys().cloned().collect();
        let held = self.held(&ids).await?;
        let mut moved: HashMap<String, Vec<String>> = HashMap::new();
        for e in by_id.values() {
            if let Some(old) = held.section.get(&e.id).filter(|old| **old != e.section) {
                moved.entry(old.clone()).or_default().push(e.id.clone());
            }
        }
        for (key, ids) in &moved {
            let sec = self.section(key).await?;
            Self::delete_in(&sec.chunks, ids).await?;
            Self::delete_in(&sec.entries, ids).await?;
        }
        let mut groups: IndexMap<String, Vec<Entry>> = IndexMap::new();
        for (_, e) in by_id {
            groups.entry(e.section.clone()).or_default().push(e);
        }
        let cols = self.spec.entry_cols();
        let mut out = Written::default();
        for (key, entries) in groups {
            let sec = self.section(&key).await?;
            let rows: Vec<Vec<Cell>> = entries.iter().map(|e| e.cells.clone()).collect();
            for part in rows.chunks(BATCH) {
                let mut m = sec.entries.merge_insert(&["id"]);
                m.when_matched_update_all(None).when_not_matched_insert_all();
                m.execute(reader(cells::batch(&cols, part)?)).await?;
            }
            sec.pending.fetch_add(entries.len(), Ordering::SeqCst);
            out.entries += entries.len();
            if !sec.indexed.load(Ordering::SeqCst) {
                ensure_indexes(&self.spec, &sec, &self.store.path).await?;
            }
            let todo: Vec<(String, Vec<Cell>)> = entries.into_iter().map(|e| (e.id, e.cells)).collect();
            self.chunk_into(&sec, &todo, &held, embedder, &mut out).await?;
        }
        drop(_w);
        self.after_write();
        Ok(out)
    }

    /// Build and store the chunks of `entries`, all in section `sec`, then mark them chunked.
    async fn chunk_into(
        &self,
        sec: &Section,
        entries: &[(String, Vec<Cell>)],
        held: &Held,
        embedder: Option<&dyn Embedder>,
        out: &mut Written,
    ) -> Result<()> {
        let cols = self.spec.entry_cols();
        let kept = self.spec.kept();
        let pos = |name: &str| cols.iter().position(|(n, _)| n == name).unwrap();
        let mut plan: Vec<(&str, &Vec<Cell>, String, Vec<String>)> = Vec::new();
        let mut reused: Vec<(&str, &Vec<Cell>, String)> = Vec::new();
        for (id, cells) in entries {
            let doc = self.doc(&cols, cells, id);
            let key = content_hash(&doc);
            if held.chunked.get(id) == Some(&key) && held.chunks.contains_key(id) {
                reused.push((id, cells, key));
            } else if let Some(e) = embedder.filter(|_| out.error.is_none()) {
                plan.push((id, cells, key, Self::texts(&doc, e)));
            } else {
                out.waiting += 1;
            }
        }
        let mut vectors: HashMap<String, Vec<f32>> = HashMap::new();
        let mut todo: Vec<&str> = Vec::new();
        for (_, _, _, texts) in &plan {
            for t in texts {
                let h = text_hash(t);
                if vectors.contains_key(&h) {
                    continue;
                }
                match held.vector(&h) {
                    Some(v) => {
                        vectors.insert(h, v.clone());
                    }
                    None => todo.push(t),
                }
            }
        }
        todo.sort_unstable();
        todo.dedup();
        if let (false, Some(e)) = (todo.is_empty(), embedder) {
            match e.encode(&todo) {
                Ok(vs) => {
                    out.embedded += todo.len();
                    for (t, v) in todo.iter().zip(vs) {
                        vectors.insert(text_hash(t), v);
                    }
                }
                Err(err) => {
                    out.error = Some(err.to_string());
                    out.waiting += plan.len();
                    plan.clear();
                }
            }
        }
        let kept_cells = |cells: &Vec<Cell>| kept.iter().map(|(n, _)| cells[pos(n)].clone()).collect::<Vec<_>>();
        let mut rows: Vec<Vec<Cell>> = Vec::new();
        let mut done: Vec<(&Vec<Cell>, String)> = Vec::new();
        for (id, cells, key) in &reused {
            for (j, h, v) in &held.chunks[*id] {
                let mut r = vec![Cell::Text(id.to_string()), Cell::Int(*j), Cell::Text(h.clone())];
                r.extend(kept_cells(cells));
                r.push(Cell::Vector(v.clone()));
                rows.push(r);
            }
            done.push((cells, key.clone()));
        }
        for (id, cells, key, texts) in &plan {
            for (j, t) in texts.iter().enumerate() {
                let h = text_hash(t);
                let mut r = vec![Cell::Text(id.to_string()), Cell::Int(j as i64), Cell::Text(h.clone())];
                r.extend(kept_cells(cells));
                r.push(Cell::Vector(vectors[&h].clone()));
                rows.push(r);
            }
            done.push((cells, key.clone()));
        }
        let ids: Vec<String> = entries.iter().map(|(id, _)| id.clone()).collect();
        if held.chunks.keys().any(|id| ids.contains(id)) {
            Self::delete_in(&sec.chunks, &ids).await?;
        }
        let chunk_cols = self.spec.chunk_cols();
        for part in rows.chunks(BATCH * 4) {
            sec.chunks.add(cells::batch(&chunk_cols, part)?).execute().await?;
        }
        sec.pending_chunks.fetch_add(rows.len(), Ordering::SeqCst);
        // Lance SQL has no CASE, so the entries are written again whole, now marked chunked.
        let at = pos(CHUNKED);
        let marked: Vec<Vec<Cell>> = done
            .iter()
            .map(|(cells, key)| {
                let mut r = (*cells).clone();
                r[at] = Cell::Text(key.clone());
                r
            })
            .collect();
        for part in marked.chunks(BATCH) {
            let mut m = sec.entries.merge_insert(&["id"]);
            m.when_matched_update_all(None);
            m.execute(reader(cells::batch(&cols, part)?)).await?;
        }
        Ok(())
    }

    /// Remove entries and their chunks: chunks first, so an entry never outlives a crash unseen.
    pub async fn delete(&self, ids: &[String]) -> Result<usize> {
        let _w = self.write.lock().await;
        let held = self.held(ids).await?;
        let mut by_section: HashMap<&str, Vec<String>> = HashMap::new();
        for (id, key) in &held.section {
            by_section.entry(key).or_default().push(id.clone());
        }
        for (key, ids) in &by_section {
            let sec = self.section(key).await?;
            Self::delete_in(&sec.chunks, ids).await?;
            Self::delete_in(&sec.entries, ids).await?;
        }
        drop(_w);
        Ok(held.section.len())
    }

    /// Remove the entries `cond` selects in every section, with their chunks, and return how
    /// many went. `cond` may name any entry column; the chunks go by the ids it selects.
    pub async fn delete_where(&self, cond: &str) -> Result<usize> {
        let _w = self.write.lock().await;
        let mut n = 0;
        for sec in self.sections() {
            let ids: Vec<String> = collect(sec.entries.query().only_if(cond).select(Select::columns(&["id"])))
                .await?
                .iter()
                .map(|r| cells::text(r, "id"))
                .collect();
            if ids.is_empty() {
                continue;
            }
            Self::delete_in(&sec.chunks, &ids).await?;
            sec.entries.delete(cond).await?;
            n += ids.len();
        }
        Ok(n)
    }

    /// Embed the entries whose chunks wait for the model. Run it when the model loads and at start.
    pub async fn embed_waiting(&self, embedder: &dyn Embedder) -> Result<Written> {
        let mut out = Written::default();
        let cols = self.spec.entry_cols();
        let names: Vec<&str> = cols.iter().map(|(n, _)| n.as_str()).collect();
        for sec in self.sections() {
            let mut skip: HashSet<String> = HashSet::new();
            loop {
                let _w = self.write.lock().await;
                let mut cond = format!("{CHUNKED} IS NULL");
                if !skip.is_empty() {
                    let ids: Vec<&String> = skip.iter().collect();
                    cond = format!("{cond} AND NOT ({})", id_list(&ids));
                }
                let q = sec.entries.query().only_if(&cond).select(Select::columns(&names)).limit(BATCH);
                let got = collect(q).await?;
                if got.is_empty() {
                    break;
                }
                let entries: Vec<(String, Vec<Cell>)> = got
                    .iter()
                    .map(|r| (cells::text(r, "id"), names.iter().map(|n| r.get(*n).cloned().unwrap_or(Cell::Null)).collect()))
                    .collect();
                let ids: Vec<String> = entries.iter().map(|(id, _)| id.clone()).collect();
                let held = self.held(&ids).await?;
                let mut step = Written::default();
                self.chunk_into(&sec, &entries, &held, Some(embedder), &mut step).await?;
                out.entries += entries.len();
                out.embedded += step.embedded;
                out.waiting += step.waiting;
                if step.error.is_some() {
                    out.error = step.error;
                    return Ok(out);
                }
                skip.extend(ids);
                drop(_w);
                self.after_write();
            }
        }
        Ok(out)
    }

    /// Start maintenance of each section whose rows outside the indexes reached [`MAINTAIN_AT`].
    fn after_write(&self) {
        for sec in self.sections() {
            let (e, c) = sec.pending();
            if e < MAINTAIN_AT && c < MAINTAIN_AT {
                continue;
            }
            sec.pending.store(0, Ordering::SeqCst);
            sec.pending_chunks.store(0, Ordering::SeqCst);
            let (spec, root, write) = (self.spec.clone(), self.store.path.clone(), self.write.clone());
            let task = tokio::spawn(async move {
                if let Err(err) = maintain(&spec, &sec, &root, &write).await {
                    tracing::warn!(section = %sec.key, "maintenance of {} failed: {err}", spec.name);
                }
            });
            let mut work = self.work.lock().unwrap();
            work.retain(|h| !h.is_finished());
            work.push(task);
        }
    }
}

fn fts_params() -> FtsIndexBuilder {
    FtsIndexBuilder::default()
        .base_tokenizer("simple".into())
        .with_position(true)
        .lower_case(true)
        .stem(false)
        .remove_stop_words(false)
        .ascii_folding(true)
}

fn trained_path(root: &Path, table: &str) -> PathBuf {
    root.join(format!("{table}.trained"))
}

fn ivf(rows: usize) -> LanceIndex {
    let parts = 16.max((rows as f64).sqrt() as u32);
    LanceIndex::IvfRq(IvfRqIndexBuilder::default().distance_type(DistanceType::Cosine).num_bits(1).num_partitions(parts))
}

async fn index_names(table: &Table) -> Result<HashSet<String>> {
    Ok(table.list_indices().await?.into_iter().map(|i| i.name).collect())
}

/// `_ensure_indexes`: every scalar and full-text index once a table has rows, and the vector
/// index once the chunks reach [`IVF_MIN_ROWS`].
async fn ensure_indexes(spec: &Spec, sec: &Section, root: &Path) -> Result<()> {
    let have = index_names(&sec.entries).await?;
    if sec.entries.count_rows(None).await? > 0 {
        if !have.contains("id_idx") {
            sec.entries.create_index(&["id"], LanceIndex::BTree(BTreeIndexBuilder::default())).execute().await?;
        }
        for f in spec.fields.iter() {
            if have.contains(&format!("{}_idx", f.name)) {
                continue;
            }
            let index = match f.class {
                Class::Content => LanceIndex::FTS(fts_params()),
                Class::Categorical => LanceIndex::LabelList(LabelListIndexBuilder::default()),
                _ => LanceIndex::BTree(BTreeIndexBuilder::default()),
            };
            sec.entries.create_index(&[f.name.as_str()], index).execute().await?;
        }
        sec.indexed.store(true, Ordering::SeqCst);
    }
    let rows = sec.chunks.count_rows(None).await?;
    let have = index_names(&sec.chunks).await?;
    if rows > 0 && !have.contains("id_idx") {
        sec.chunks.create_index(&["id"], LanceIndex::BTree(BTreeIndexBuilder::default())).execute().await?;
    }
    let trained = trained_path(root, sec.chunks.name());
    if rows >= IVF_MIN_ROWS && !have.contains("vector_idx") {
        sec.chunks.create_index(&["vector"], ivf(rows)).execute().await?;
        std::fs::write(&trained, rows.to_string())?;
    } else if have.contains("vector_idx") && !trained.exists() {
        let stats = sec.chunks.index_stats("vector_idx").await?;
        std::fs::write(&trained, stats.map_or(rows, |s| s.num_indexed_rows).to_string())?;
    }
    Ok(())
}

/// Fold new rows into delta indexes, merge stacked deltas, compact small files, drop the
/// versions the compaction superseded, and retrain the vector index once the chunks double.
async fn maintain(spec: &Spec, sec: &Section, root: &Path, write: &Mutex<()>) -> Result<()> {
    {
        let _w = write.lock().await;
        ensure_indexes(spec, sec, root).await?;
    }
    for table in [&sec.entries, &sec.chunks] {
        let _w = write.lock().await;
        let mut stacked = 0;
        for i in table.list_indices().await? {
            if let Some(s) = table.index_stats(&i.name).await? {
                stacked = stacked.max(s.num_indices.unwrap_or(0));
            }
        }
        let options = if stacked > MERGE_AT { OptimizeOptions::merge(stacked as usize) } else { OptimizeOptions::append() };
        table.optimize(OptimizeAction::Index(options)).await?;
        let compaction = CompactionOptions { materialize_deletions_threshold: 0.5, ..Default::default() };
        table.optimize(OptimizeAction::Compact { options: compaction, remap_options: None }).await?;
        table
            .optimize(OptimizeAction::Prune {
                older_than: lancedb::table::Duration::try_seconds(KEEP_VERSIONS_S),
                delete_unverified: None,
                error_if_tagged_old_versions: None,
            })
            .await?;
    }
    let trained = trained_path(root, sec.chunks.name());
    let at: Option<usize> = std::fs::read_to_string(&trained).ok().and_then(|s| s.trim().parse().ok());
    if let Some(at) = at {
        let rows = sec.chunks.count_rows(None).await?;
        if rows >= 2 * at.max(1) {
            let _w = write.lock().await;
            sec.chunks.create_index(&["vector"], ivf(rows)).replace(true).execute().await?;
            std::fs::write(&trained, rows.to_string())?;
        }
    }
    Ok(())
}
