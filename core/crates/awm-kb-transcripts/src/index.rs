//! The archive's full-text index: one LanceDB database at `<AWM_DIR>/services/kb/lance/transcripts/`,
//! with no vectors.
//!
//! The entry tables hold one row per transcript line that carries message text, in yearly
//! sections by the line's own timestamp. A plain table `sessions` beside them holds one record
//! per indexed archive session: the gzip's mtime and size, which the start compares to find
//! the sessions to index again, and the distinct `cwd`s, which the peer veil reads.
//!
//! Only this crate's sweep, prune and start change the archive, so they are the events that
//! change the index. A session's record is written before its rows, marked unfinished, so a
//! crash between the two leaves a record that the next start redoes and that a peer never sees.

use std::collections::{BTreeMap, BTreeSet, BinaryHeap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex as StdMutex, RwLock};

use awm_index::cells::{self, Cell, Kind, Row};
use awm_index::fields::{Class, Field, Fields};
use awm_index::sql::{and, id_list, quote};
use awm_index::{Index, IndexError, Spec, Store};
use awm_plugin_sdk::privacy;
use futures_util::TryStreamExt;
use lancedb::arrow::arrow_array::{RecordBatch, RecordBatchIterator, RecordBatchReader};
use lancedb::index::scalar::{FtsQuery, FullTextSearchQuery, PhraseQuery};
use lancedb::query::{ExecutableQuery, HasQuery, QueryBase, Select};
use lancedb::Table;
use serde_json::{json, Map, Value};
use tokio::sync::Mutex;

use crate::layout::{sessions, Side};
use crate::search::{Entered, Lines, Pattern, LINE_CHARS};

pub const SOURCE: &str = "transcripts";
const SESSIONS: &str = "sessions";
/// Rows gathered from several sessions before one write, so maintenance runs per batch.
const BATCH_ROWS: usize = 20_000;
const BATCH_SESSIONS: usize = 200;
const ID_BATCH: usize = 500;
/// The size an unfinished record carries, which no gzip has, so the next start redoes it.
const UNFINISHED: i64 = -1;

pub type Result<T, E = IndexError> = std::result::Result<T, E>;

/// The source's fields, as the old manifest declared them, and the columns beside them.
pub fn spec() -> Spec {
    Spec {
        name: SOURCE.into(),
        fields: Fields(vec![
            Field::new("title", Class::Content),
            Field::new("text", Class::Content),
            Field::new("ts", Class::Datetime),
            Field::new("project", Class::Categorical),
            Field::nested("scope", "/"),
        ]),
        hit: vec!["title".into(), "ts".into()],
        extra: vec![("session".into(), Kind::Text), ("line".into(), Kind::Int), ("cwd".into(), Kind::Text)],
        section_by: Some("ts".into()),
    }
}

fn record_cols() -> Vec<(String, Kind)> {
    [
        ("id", Kind::Text),
        ("project", Kind::Text),
        ("session", Kind::Text),
        ("mtime_ns", Kind::Int),
        ("size", Kind::Int),
        ("rows", Kind::Int),
        ("lines", Kind::Int),
        ("cwds", Kind::Texts),
        ("error", Kind::Text),
    ]
    .into_iter()
    .map(|(n, k)| (n.to_string(), k))
    .collect()
}

/// One indexed archive session.
#[derive(Debug, Clone, PartialEq)]
pub struct Record {
    pub project: String,
    pub session: String,
    pub mtime_ns: i64,
    pub size: i64,
    pub rows: i64,
    pub lines: i64,
    pub cwds: Vec<String>,
    /// Why the gzip did not read to its end, or that its rows are still being written. A peer
    /// sees nothing of a session with an error.
    pub error: Option<String>,
}

impl Record {
    pub fn key(project: &str, session: &str) -> String {
        format!("{project}/{session}")
    }

    fn cells(&self) -> Vec<Cell> {
        vec![
            Cell::Text(Self::key(&self.project, &self.session)),
            Cell::Text(self.project.clone()),
            Cell::Text(self.session.clone()),
            Cell::Int(self.mtime_ns),
            Cell::Int(self.size),
            Cell::Int(self.rows),
            Cell::Int(self.lines),
            Cell::Texts(self.cwds.clone()),
            self.error.clone().map_or(Cell::Null, Cell::Text),
        ]
    }

    fn of(r: &Row) -> Record {
        let int = |n: &str| match r.get(n) {
            Some(Cell::Int(i)) => *i,
            _ => 0,
        };
        Record {
            project: cells::text(r, "project"),
            session: cells::text(r, "session"),
            mtime_ns: int("mtime_ns"),
            size: int("size"),
            rows: int("rows"),
            lines: int("lines"),
            cwds: match r.get("cwds") {
                Some(Cell::Texts(v)) => v.clone(),
                _ => vec![],
            },
            error: r.get("error").and_then(Cell::as_str).map(str::to_string),
        }
    }
}

/// A file's mtime in nanoseconds and its size, or `None` when it is gone.
pub fn stamp(path: &Path) -> Option<(i64, i64)> {
    let meta = std::fs::metadata(path).ok()?;
    let ns = meta.modified().ok()?.duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_nanos() as i64);
    Some((ns, meta.len() as i64))
}

fn push_strings<'a>(v: &'a Value, out: &mut Vec<&'a str>) {
    match v {
        Value::String(s) => out.push(s),
        Value::Array(a) => a.iter().for_each(|x| push_strings(x, out)),
        Value::Object(m) => m.values().for_each(|x| push_strings(x, out)),
        _ => {}
    }
}

fn push_content<'a>(content: &'a Value, out: &mut Vec<&'a str>) {
    match content {
        Value::String(s) => out.push(s),
        Value::Array(blocks) => blocks.iter().for_each(|b| push_block(b, out)),
        _ => {}
    }
}

fn push_block<'a>(block: &'a Value, out: &mut Vec<&'a str>) {
    let Value::Object(b) = block else {
        if let Value::String(s) = block {
            out.push(s);
        }
        return;
    };
    let field = |k: &str| b.get(k).and_then(Value::as_str);
    match field("type").unwrap_or("") {
        "thinking" => out.extend(field("thinking")),
        "tool_use" | "server_tool_use" => {
            if let Some(input) = b.get("input") {
                push_strings(input, out);
            }
        }
        t if t == "tool_result" || t.ends_with("_tool_result") => {
            if let Some(c) = b.get("content") {
                push_content(c, out);
            }
        }
        _ => out.extend(field("text")),
    }
}

/// A transcript line's message text: every string of `message.content` (text and thinking
/// blocks, tool_use input values, tool_result content), or `message` when it is a string, a
/// summary line's `summary`, and a system line's string `content`. `None` when it has none.
pub fn line_text(line: &Value) -> Option<String> {
    let mut parts: Vec<&str> = vec![];
    match line.get("message") {
        Some(Value::String(s)) => parts.push(s),
        Some(Value::Object(m)) => {
            if let Some(c) = m.get("content") {
                push_content(c, &mut parts);
            }
        }
        _ => {}
    }
    if let Some(Value::String(s)) = line.get("summary") {
        parts.push(s);
    }
    if let Some(Value::String(s)) = line.get("content") {
        parts.push(s);
    }
    let text = parts.into_iter().filter(|p| !awm_pycompat::pystr::strip(p).is_empty()).collect::<Vec<_>>().join("\n");
    (!text.is_empty()).then_some(text)
}

/// A hit's title: the head of its line's text, as `search` cuts a line.
pub fn title_of(text: &str) -> String {
    awm_pycompat::pystr::rstrip(awm_pycompat::pystr::head(text, LINE_CHARS)).to_string()
}

/// One archive session read for the index.
#[derive(Debug, Clone)]
pub struct Read {
    pub record: Record,
    pub path: PathBuf,
    pub rows: Vec<Map<String, Value>>,
}

/// Read one gzipped transcript into rows, or `None` when it is gone. A line that is not JSON
/// is indexed as it stands. The distinct `cwd`s are every one a line names anywhere, as the
/// veil has always read them.
pub fn read(project: &str, session: &str, path: &Path) -> Option<Read> {
    let (mtime_ns, size) = stamp(path)?;
    let lines = Lines::open(path).ok()?;
    let fallback_ts = awm_pycompat::time::zulu(&awm_pycompat::time::DateTime::from_utc_us(mtime_ns / 1000));
    let mut cwds: BTreeSet<String> = BTreeSet::new();
    let mut rows = vec![];
    let mut error = None;
    let mut n = 0i64;
    for line in lines {
        let line = match line {
            Ok(l) => l,
            Err(e) => {
                error = Some(e.to_string());
                break;
            }
        };
        n += 1;
        Entered::cwds(&line, |c| {
            if !c.is_empty() && !cwds.contains(c) {
                cwds.insert(c.to_string());
            }
        });
        let (text, ts, cwd) = match serde_json::from_str::<Value>(&line) {
            Ok(v @ Value::Object(_)) => {
                let ts = v.get("timestamp").filter(|t| cells::parse_ts(t).is_some()).cloned();
                (line_text(&v), ts, v.get("cwd").and_then(Value::as_str).unwrap_or("").to_string())
            }
            _ => ((!awm_pycompat::pystr::strip(&line).is_empty()).then(|| line.clone()), None, String::new()),
        };
        let Some(text) = text else { continue };
        let mut row = Map::new();
        row.insert("id".into(), json!(format!("{session}:{n}")));
        row.insert("title".into(), json!(""));
        row.insert("text".into(), json!(text));
        row.insert("ts".into(), ts.unwrap_or_else(|| json!(fallback_ts)));
        row.insert("project".into(), json!(project));
        row.insert("session".into(), json!(session));
        row.insert("line".into(), json!(n));
        row.insert("cwd".into(), json!(cwd));
        rows.push(row);
    }
    let record = Record {
        project: project.into(),
        session: session.into(),
        mtime_ns,
        size,
        rows: rows.len() as i64,
        lines: n,
        cwds: cwds.into_iter().collect(),
        error,
    };
    Some(Read { record, path: path.to_path_buf(), rows })
}

/// How far the start's reconcile has come.
#[derive(Debug, Default)]
pub struct Progress {
    pub running: AtomicBool,
    pub total: AtomicUsize,
    pub done: AtomicUsize,
    pub last: StdMutex<Option<Value>>,
}

/// One line of an archive scan, ordered as the archive lists it.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Line {
    pub project: String,
    pub session: String,
    pub line: i64,
    pub text: String,
}

impl Line {
    pub fn to_json(&self) -> Value {
        json!({"project": self.project, "session": self.session, "line": self.line, "text": self.text})
    }
}

/// The archive's index: the entry sections, the session records and their in-memory copy.
pub struct TxIndex {
    pub index: Index,
    sessions: Table,
    records: RwLock<BTreeMap<String, Record>>,
    /// One writer at a time: a batch of sessions, a sweep's sessions or a prune's.
    write: Mutex<()>,
    pub progress: Progress,
}

async fn collect(q: impl ExecutableQuery) -> Result<Vec<Row>> {
    let batches: Vec<RecordBatch> = q.execute().await?.try_collect().await?;
    Ok(cells::rows(&batches))
}

fn reader(batch: RecordBatch) -> Box<dyn RecordBatchReader + Send> {
    let schema = batch.schema();
    Box::new(RecordBatchIterator::new(vec![Ok(batch)], schema))
}

/// `project` is one of `projects`.
fn in_projects(projects: &BTreeSet<String>) -> String {
    let parts: Vec<String> = projects.iter().map(|p| format!("array_has(project, {})", quote(p))).collect();
    awm_index::sql::join("OR", parts, "false")
}

fn session_in(project: &str, ids: &[&str]) -> String {
    format!("array_has(project, {}) AND session IN ({})", quote(project), ids.iter().map(|s| quote(s)).collect::<Vec<_>>().join(", "))
}

impl TxIndex {
    /// Open the database at `dir`, creating it empty.
    pub async fn open(dir: &Path) -> Result<TxIndex> {
        let store = Store::open(dir).await?;
        let index = store.index(spec()).await?;
        let db = store.connection();
        let sessions = if store.table_names().await?.iter().any(|t| t == SESSIONS) {
            db.open_table(SESSIONS).execute().await?
        } else {
            db.create_empty_table(SESSIONS, cells::schema(&record_cols())).execute().await?
        };
        let mut records = BTreeMap::new();
        for r in collect(sessions.query()).await? {
            records.insert(cells::text(&r, "id"), Record::of(&r));
        }
        Ok(TxIndex { index, sessions, records: RwLock::new(records), write: Mutex::new(()), progress: Progress::default() })
    }

    pub fn records(&self) -> Vec<Record> {
        self.records.read().unwrap().values().cloned().collect()
    }

    pub fn record(&self, project: &str, session: &str) -> Option<Record> {
        self.records.read().unwrap().get(&Record::key(project, session)).cloned()
    }

    async fn put_records(&self, records: &[Record]) -> Result<()> {
        if records.is_empty() {
            return Ok(());
        }
        let cols = record_cols();
        for part in records.chunks(ID_BATCH) {
            let rows: Vec<Vec<Cell>> = part.iter().map(Record::cells).collect();
            let mut m = self.sessions.merge_insert(&["id"]);
            m.when_matched_update_all(None).when_not_matched_insert_all();
            m.execute(reader(cells::batch(&cols, &rows)?)).await?;
        }
        let mut held = self.records.write().unwrap();
        for r in records {
            held.insert(Record::key(&r.project, &r.session), r.clone());
        }
        Ok(())
    }

    async fn drop_records(&self, keys: &[String]) -> Result<()> {
        for part in keys.chunks(ID_BATCH) {
            self.sessions.delete(&id_list(part)).await?;
        }
        let mut held = self.records.write().unwrap();
        for k in keys {
            held.remove(k);
        }
        Ok(())
    }

    /// Delete the rows of these sessions, grouped by project.
    async fn drop_rows(&self, gone: &[(String, String)]) -> Result<usize> {
        let mut by_project: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
        for (p, s) in gone {
            by_project.entry(p).or_default().push(s);
        }
        let mut n = 0;
        for (project, ids) in by_project {
            for part in ids.chunks(ID_BATCH) {
                n += self.index.delete_where(&session_in(project, part)).await?;
            }
        }
        Ok(n)
    }

    /// Index sessions read from the archive, replacing what they held. A session whose gzip
    /// changed or went since it was read is left for the event that changed it.
    pub async fn put(&self, reads: Vec<Read>) -> Result<usize> {
        let _w = self.write.lock().await;
        let reads: Vec<Read> = reads.into_iter().filter(|r| stamp(&r.path) == Some((r.record.mtime_ns, r.record.size))).collect();
        if reads.is_empty() {
            return Ok(0);
        }
        let had: Vec<(String, String)> = {
            let held = self.records.read().unwrap();
            reads
                .iter()
                .filter(|r| held.contains_key(&Record::key(&r.record.project, &r.record.session)))
                .map(|r| (r.record.project.clone(), r.record.session.clone()))
                .collect()
        };
        let unfinished: Vec<Record> = reads
            .iter()
            .map(|r| Record { size: UNFINISHED, error: Some("indexing".into()), ..r.record.clone() })
            .collect();
        self.put_records(&unfinished).await?;
        self.drop_rows(&had).await?;
        let rows: Vec<Map<String, Value>> = reads.iter().flat_map(|r| r.rows.iter().cloned()).collect();
        if !rows.is_empty() {
            self.index.upsert(&rows, None).await?;
        }
        let done: Vec<Record> = reads.into_iter().map(|r| r.record).collect();
        self.put_records(&done).await?;
        Ok(rows.len())
    }

    /// Read and index these archive sessions, in batches.
    pub async fn index_sessions(&self, todo: Vec<(String, String, PathBuf)>) -> Result<usize> {
        let mut n = 0;
        let mut batch: Vec<Read> = vec![];
        let mut rows = 0;
        for (project, session, path) in todo {
            let got = tokio::task::spawn_blocking(move || read(&project, &session, &path)).await.map_err(|e| IndexError::Layout(e.to_string()))?;
            if let Some(r) = got {
                rows += r.rows.len();
                batch.push(r);
            }
            if rows >= BATCH_ROWS || batch.len() >= BATCH_SESSIONS {
                n += self.put(std::mem::take(&mut batch)).await?;
                rows = 0;
            }
        }
        n += self.put(batch).await?;
        Ok(n)
    }

    /// Forget sessions that left the archive: their rows, then their records.
    pub async fn forget(&self, gone: &[(String, String)]) -> Result<usize> {
        let _w = self.write.lock().await;
        let n = self.drop_rows(gone).await?;
        let keys: Vec<String> = gone.iter().map(|(p, s)| Record::key(p, s)).collect();
        self.drop_records(&keys).await?;
        Ok(n)
    }

    /// Bring the index level with the archive: index each session whose gzip is new or changed
    /// by mtime and size, and forget each that went. Reads run on `reader`, which the caller
    /// may run at idle priority.
    pub async fn reconcile(&self, archive: &Path, reader: impl Fn(Box<dyn FnOnce() + Send>) + Send + Sync) -> Result<Value> {
        let started = std::time::Instant::now();
        let root = archive.to_path_buf();
        let listed = tokio::task::spawn_blocking(move || {
            sessions(&root, Side::Archive, None)
                .into_iter()
                .filter_map(|s| {
                    let t = s.transcript?;
                    let st = stamp(&t)?;
                    Some((s.project, s.session_id, t, st))
                })
                .collect::<Vec<_>>()
        })
        .await
        .map_err(|e| IndexError::Layout(e.to_string()))?;
        let held = self.records.read().unwrap().clone();
        let present: BTreeSet<String> = listed.iter().map(|(p, s, _, _)| Record::key(p, s)).collect();
        let gone: Vec<(String, String)> =
            held.values().filter(|r| !present.contains(&Record::key(&r.project, &r.session))).map(|r| (r.project.clone(), r.session.clone())).collect();
        let todo: Vec<(String, String, PathBuf)> = listed
            .into_iter()
            .filter(|(p, s, _, st)| held.get(&Record::key(p, s)).is_none_or(|r| (r.mtime_ns, r.size) != *st))
            .map(|(p, s, t, _)| (p, s, t))
            .collect();
        self.progress.running.store(true, Ordering::SeqCst);
        self.progress.total.store(todo.len(), Ordering::SeqCst);
        self.progress.done.store(0, Ordering::SeqCst);
        let forgot = if gone.is_empty() { 0 } else { self.forget(&gone).await? };
        let (tx, mut rx) = tokio::sync::mpsc::channel::<Option<Read>>(4);
        let count = todo.len();
        reader(Box::new(move || {
            for (p, s, t) in todo {
                if tx.blocking_send(read(&p, &s, &t)).is_err() {
                    return;
                }
            }
        }));
        let mut indexed = 0;
        let mut rows = 0;
        let mut batch: Vec<Read> = vec![];
        let mut pending = 0;
        let mut received = 0;
        let outcome: Result<()> = async {
            while received < count {
                let Some(got) = rx.recv().await else { break };
                received += 1;
                if let Some(r) = got {
                    pending += r.rows.len();
                    batch.push(r);
                }
                if pending >= BATCH_ROWS || batch.len() >= BATCH_SESSIONS || received == count {
                    let n = batch.len();
                    rows += self.put(std::mem::take(&mut batch)).await?;
                    indexed += n;
                    pending = 0;
                    self.progress.done.store(received, Ordering::SeqCst);
                }
            }
            Ok(())
        }
        .await;
        self.progress.done.store(received, Ordering::SeqCst);
        self.progress.running.store(false, Ordering::SeqCst);
        let mut last = json!({"sessions": indexed, "rows": rows, "forgot": gone.len(), "rows_forgot": forgot, "seconds": (started.elapsed().as_secs_f64() * 100.0).round() / 100.0});
        if let Err(e) = &outcome {
            last["error"] = json!(e.to_string());
        }
        *self.progress.last.lock().unwrap() = Some(last.clone());
        outcome.map(|_| last)
    }

    /// The condition that keeps a caller to what it may see: the projects containing
    /// `project`, and for a peer no marked project nor any session that entered a marked path
    /// or did not read whole. `None` when nothing is left.
    pub fn narrow(&self, project: Option<&str>, peer: bool) -> Option<String> {
        let project = project.filter(|p| !p.is_empty());
        if project.is_none() && !peer {
            return Some("true".into());
        }
        let held = self.records.read().unwrap();
        let mut shown: BTreeSet<String> = BTreeSet::new();
        let mut cut = false;
        let mut hidden_project: HashMap<&str, bool> = HashMap::new();
        let mut hidden_cwd: HashMap<&str, bool> = HashMap::new();
        let mut excluded: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
        for r in held.values() {
            if project.is_some_and(|p| !r.project.contains(p)) || (peer && *hidden_project.entry(&r.project).or_insert_with(|| privacy::hidden_slug(&r.project))) {
                cut = true;
                continue;
            }
            if peer && (r.error.is_some() || r.cwds.iter().any(|c| *hidden_cwd.entry(c).or_insert_with(|| privacy::hidden_path(c)))) {
                excluded.entry(&r.project).or_default().push(&r.session);
                continue;
            }
            shown.insert(r.project.clone());
        }
        if shown.is_empty() {
            return None;
        }
        let mut parts = vec![];
        if cut {
            parts.push(in_projects(&shown));
        }
        for (p, ids) in excluded {
            for part in ids.chunks(ID_BATCH) {
                parts.push(format!("NOT ({})", session_in(p, part)));
            }
        }
        Some(and(&parts.iter().map(String::as_str).collect::<Vec<_>>()))
    }

    /// The archive lines whose text `rx` matches, in archive order: at most `limit`, and
    /// whether the limit was reached.
    pub async fn scan(&self, rx: Arc<Pattern>, cond: &str, limit: usize) -> Result<(Vec<Line>, bool)> {
        let mut best: BinaryHeap<Line> = BinaryHeap::new();
        let mut matched = 0usize;
        for sec in self.index.sections() {
            let mut q = sec.entries.query().select(Select::columns(&["project", "session", "line", "text"]));
            if cond != "true" {
                q = q.only_if(cond);
            }
            let mut stream = q.execute().await?;
            while let Some(batch) = stream.try_next().await? {
                let rx = rx.clone();
                let found = tokio::task::spawn_blocking(move || {
                    cells::rows(&[batch])
                        .into_iter()
                        .filter(|r| rx.is_match(r.get("text").and_then(Cell::as_str).unwrap_or("")).unwrap_or(false))
                        .map(|r| Line {
                            project: r.get("project").and_then(|c| if let Cell::Texts(v) = c { v.first().cloned() } else { None }).unwrap_or_default(),
                            session: cells::text(&r, "session"),
                            line: if let Some(Cell::Int(i)) = r.get("line") { *i } else { 0 },
                            text: title_of(r.get("text").and_then(Cell::as_str).unwrap_or("")),
                        })
                        .collect::<Vec<_>>()
                })
                .await
                .map_err(|e| IndexError::Layout(e.to_string()))?;
                for l in found {
                    matched += 1;
                    best.push(l);
                    if best.len() > limit {
                        best.pop();
                    }
                }
            }
        }
        Ok((best.into_sorted_vec(), limit == 0 || matched >= limit))
    }

    /// The archive lines that hold the words of `phrase`, in archive order, as
    /// `(project, session, line, id)`.
    pub async fn phrase(&self, phrase: &str, cond: &str) -> Result<Vec<(String, String, i64, String)>> {
        let query: FtsQuery = PhraseQuery::new(phrase.to_string()).with_column(Some("text".into())).into();
        let mut out = vec![];
        for sec in self.index.sections() {
            if sec.entries.count_rows(None).await? == 0 {
                continue;
            }
            let mut q = sec.entries.query().full_text_search(FullTextSearchQuery::new_query(query.clone())).select(Select::columns(&["id", "project", "session", "line"]));
            q.mut_query().limit = None;
            if cond != "true" {
                q = q.only_if(cond);
            }
            for r in collect(q).await? {
                let project = match r.get("project") {
                    Some(Cell::Texts(v)) => v.first().cloned().unwrap_or_default(),
                    _ => String::new(),
                };
                let line = if let Some(Cell::Int(i)) = r.get("line") { *i } else { 0 };
                out.push((project, cells::text(&r, "session"), line, cells::text(&r, "id")));
            }
        }
        out.sort();
        Ok(out)
    }

    /// The text of each of `ids`.
    pub async fn texts(&self, ids: &[String]) -> Result<HashMap<String, String>> {
        let mut out = HashMap::new();
        for sec in self.index.sections() {
            for part in ids.chunks(ID_BATCH) {
                for r in collect(sec.entries.query().only_if(id_list(part)).select(Select::columns(&["id", "text"]))).await? {
                    out.insert(cells::text(&r, "id"), cells::text(&r, "text"));
                }
            }
        }
        Ok(out)
    }

    /// The source's counts, and how far the reconcile has come.
    pub async fn status(&self) -> Value {
        let rows = self.index.count(None).await.map_err(|e| e.to_string());
        let held = self.records.read().unwrap();
        let p = &self.progress;
        json!({
            "rows": match rows { Ok(n) => json!(n), Err(e) => json!(e) },
            "sessions": held.len(),
            "unfinished": held.values().filter(|r| r.size == UNFINISHED).count(),
            "sections": self.index.sections().iter().map(|s| s.key.clone()).collect::<Vec<_>>(),
            "reconcile": {
                "running": p.running.load(Ordering::SeqCst),
                "indexed": p.done.load(Ordering::SeqCst),
                "total": p.total.load(Ordering::SeqCst),
                "last": p.last.lock().unwrap().clone(),
            },
        })
    }
}
