//! The import that builds a papers generation from a semdb build, its citation graph and the
//! Zotero library.
//!
//! It reuses semdb's bge-large vectors, so a full import embeds only the Zotero items semdb
//! lacks. semdb is read in place and never written: its directories are DVC-tracked.
//!
//! Inputs, all under `--semdb`:
//! - `registry/paper.parquet` and `registry/paper_domain.parquet`: one row per paper, and the
//!   domains each came from, which become its collections.
//! - `chunks/{titles,abstracts,chunks}.sqlite` with `vectors/<same>/index.npy`: the texts, and
//!   their vectors row-aligned by chunk id.

use crate::{spec, CURRENT};
use anyhow::{bail, Context, Result};
use awm_embed::chunk::text_hash;
use awm_embed::{Embedder, Onnx, BGE_LARGE};
use awm_index::cells::Cell;
use awm_index::{Index, Store};
use lancedb::arrow::arrow_array::{Array, ArrayRef, Int64Array, LargeStringArray, ListArray, RecordBatch, StringArray};
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};

/// The collection every item of Tony's Zotero library joins.
pub const LIBRARY: &str = "tony";
/// The chunk rows written per Lance append.
const ROWS: usize = 20_000;
/// The entries written per Lance append: each carries its paper's whole body.
const ENTRIES: usize = 2_000;

/// What one import reads and where it writes.
#[derive(Debug, Clone)]
pub struct Opts {
    pub semdb: PathBuf,
    /// semdb's citation graph (`graph.db`), or `None` for no `cites` / `cited_by`.
    pub graph: Option<PathBuf>,
    /// The Zotero items, one JSON object per line, as `zotero items` pages them.
    pub zotero: Option<PathBuf>,
    /// The bge-large files, copied into the generation.
    pub model: PathBuf,
    /// The papers root: `<AWM_DIR>/services/kb/lance/papers`.
    pub root: PathBuf,
}

/// One paper's fields, before its texts join it.
#[derive(Debug, Clone, Default)]
struct Paper {
    doi: String,
    title: String,
    authors: Vec<String>,
    year: Option<i64>,
    journal: String,
    collections: Vec<String>,
}

/// Build a generation under `opts.root` with the model in `opts.model`, then make it current.
/// Returns what it wrote.
pub async fn run(opts: &Opts) -> Result<Value> {
    let name = format!("gen-{}", chrono_stamp());
    let dir = opts.root.join(&name);
    copy_model(&opts.model, &dir.join("model"))?;
    let embedder = Onnx::load_dir(&dir.join("model"), &BGE_LARGE)?;
    let out = build(opts, &dir, &embedder).await?;
    make_current(&opts.root, &name)?;
    prune(&opts.root, &name)?;
    Ok(out)
}

/// Write a generation's store into `dir/lance`, embedding the held Zotero items with `embedder`.
pub async fn build(opts: &Opts, dir: &Path, embedder: &dyn Embedder) -> Result<Value> {
    let started = std::time::Instant::now();

    let mut papers = registry(&opts.semdb.join("registry"))?;
    let (cites, cited_by) = match &opts.graph {
        Some(g) => citations(g)?,
        None => Default::default(),
    };
    let items = match &opts.zotero {
        Some(p) => zotero_items(p)?,
        None => vec![],
    };
    let held = join_library(&mut papers, &items);

    let index = Store::open(&dir.join("lance")).await?.index(spec()).await?;
    let entries = write_entries(&index, &opts.semdb, &papers, &cites, &cited_by).await?;
    let mut chunks = 0;
    for zone in ["titles", "abstracts", "chunks"] {
        chunks += write_chunks(&index, &opts.semdb, zone, &papers).await?;
    }
    let held_n = held.len();
    if !held.is_empty() {
        let written = index.upsert(&held, Some(embedder)).await?;
        if let Some(e) = written.error {
            bail!("the held Zotero items were not embedded: {e}");
        }
    }
    index.build_indexes().await?;
    index.settle().await;
    let mut cursor = crate::sync::Cursor::default();
    cursor.take(&items);
    cursor.save(dir)?;
    Ok(json!({
        "generation": dir.file_name().map(|n| n.to_string_lossy().into_owned()),
        "papers": entries + held_n,
        "chunks": chunks,
        "zotero": {"items": items.len(), "held": held_n},
        "secs": started.elapsed().as_secs(),
    }))
}

fn chrono_stamp() -> String {
    let now = awm_pycompat::time::DateTime::from_utc_us(awm_pycompat::time::now_us());
    format!("{:04}{:02}{:02}T{:02}{:02}{:02}Z", now.year, now.month, now.day, now.hour, now.minute, now.second)
}

fn copy_model(from: &Path, to: &Path) -> Result<()> {
    BGE_LARGE.verify(from).with_context(|| format!("bge-large files in {}", from.display()))?;
    std::fs::create_dir_all(to)?;
    for (name, _) in BGE_LARGE.files {
        std::fs::copy(from.join(name), to.join(name))?;
    }
    Ok(())
}

/// The batches of one parquet file.
fn parquet(path: &Path) -> Result<Vec<RecordBatch>> {
    let f = std::fs::File::open(path).with_context(|| path.display().to_string())?;
    let reader = parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder::try_new(f)?.with_batch_size(8192).build()?;
    Ok(reader.collect::<std::result::Result<_, _>>()?)
}

fn column<'a>(b: &'a RecordBatch, name: &str) -> Option<&'a ArrayRef> {
    b.schema().index_of(name).ok().map(|i| b.column(i))
}

/// Row `i` of a string column as text, empty when null or absent.
fn text_at(col: Option<&ArrayRef>, i: usize) -> String {
    let Some(c) = col else { return String::new() };
    if c.is_null(i) {
        return String::new();
    }
    if let Some(s) = c.as_any().downcast_ref::<StringArray>() {
        return s.value(i).to_string();
    }
    if let Some(s) = c.as_any().downcast_ref::<LargeStringArray>() {
        return s.value(i).to_string();
    }
    lancedb::arrow::arrow_cast::display::ArrayFormatter::try_new(c.as_ref(), &Default::default())
        .map(|f| f.value(i).to_string())
        .unwrap_or_default()
}

/// Row `i` of a list-of-strings column, or of a string column holding a JSON list.
fn texts_at(col: Option<&ArrayRef>, i: usize) -> Vec<String> {
    let Some(c) = col else { return vec![] };
    if c.is_null(i) {
        return vec![];
    }
    if let Some(l) = c.as_any().downcast_ref::<ListArray>() {
        let v = l.value(i);
        return (0..v.len()).map(|j| text_at(Some(&v), j)).filter(|s| !s.is_empty()).collect();
    }
    let s = text_at(Some(c), i);
    match serde_json::from_str::<Vec<Value>>(&s) {
        Ok(v) => v.into_iter().filter_map(|x| x.as_str().map(str::to_string)).collect(),
        Err(_) => s.split(';').map(|a| a.trim().to_string()).filter(|a| !a.is_empty()).collect(),
    }
}

fn year_at(col: Option<&ArrayRef>, i: usize) -> Option<i64> {
    let c = col?;
    if c.is_null(i) {
        return None;
    }
    if let Some(a) = c.as_any().downcast_ref::<Int64Array>() {
        return Some(a.value(i));
    }
    text_at(Some(c), i).get(..4)?.parse().ok()
}

/// Every registry paper by id, with the domains it came from as its collections.
fn registry(dir: &Path) -> Result<HashMap<String, Paper>> {
    let mut out = HashMap::new();
    for b in parquet(&dir.join("paper.parquet"))? {
        let id = column(&b, "paper_id").context("registry/paper.parquet has no paper_id")?;
        let (doi, title, authors, year, journal) =
            (column(&b, "doi"), column(&b, "title"), column(&b, "authors"), column(&b, "year"), column(&b, "journal"));
        for i in 0..b.num_rows() {
            let paper = Paper {
                doi: text_at(doi, i).to_lowercase(),
                title: text_at(title, i),
                authors: texts_at(authors, i),
                year: year_at(year, i),
                journal: text_at(journal, i),
                collections: vec![],
            };
            out.insert(text_at(Some(id), i), paper);
        }
    }
    for b in parquet(&dir.join("paper_domain.parquet"))? {
        let (id, domain) = (column(&b, "paper_id").context("paper_domain.parquet has no paper_id")?, column(&b, "domain"));
        for i in 0..b.num_rows() {
            let d = text_at(domain, i);
            if let Some(p) = out.get_mut(&text_at(Some(id), i)) {
                if !d.is_empty() && !p.collections.contains(&d) {
                    p.collections.push(d);
                }
            }
        }
    }
    Ok(out)
}

/// Each paper's `cites` and `cited_by`, from the graph's `cites` edges between `paper:` nodes.
fn citations(graph: &Path) -> Result<(HashMap<String, Vec<String>>, HashMap<String, Vec<String>>)> {
    let db = open_ro(graph)?;
    let mut q = db.prepare(
        "SELECT s.key, d.key FROM edge e JOIN node s ON s.nid = e.src JOIN node d ON d.nid = e.dst
         WHERE e.kind = 'cites' AND s.kind = 'paper' AND d.kind = 'paper'",
    )?;
    let (mut cites, mut cited_by): (HashMap<String, Vec<String>>, HashMap<String, Vec<String>>) = Default::default();
    let mut rows = q.query([])?;
    while let Some(r) = rows.next()? {
        let (s, d): (String, String) = (r.get(0)?, r.get(1)?);
        let (s, d) = (s.trim_start_matches("paper:").to_string(), d.trim_start_matches("paper:").to_string());
        cited_by.entry(d.clone()).or_default().push(s.clone());
        cites.entry(s).or_default().push(d);
    }
    Ok((cites, cited_by))
}

fn zotero_items(path: &Path) -> Result<Vec<Map<String, Value>>> {
    let text = std::fs::read_to_string(path).with_context(|| path.display().to_string())?;
    text.lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| Ok(serde_json::from_str::<Map<String, Value>>(l)?))
        .collect()
}

/// Lower case, letters and digits only: the form two titles are compared in.
pub(crate) fn norm_title(t: &str) -> String {
    t.chars().filter(|c| c.is_alphanumeric()).flat_map(char::to_lowercase).collect()
}

pub(crate) fn item_str<'a>(item: &'a Map<String, Value>, key: &str) -> &'a str {
    item.get(key).and_then(Value::as_str).map(str::trim).unwrap_or("")
}

pub(crate) fn item_year(item: &Map<String, Value>) -> Option<i64> {
    match item.get("year")? {
        Value::Number(n) => n.as_i64(),
        Value::String(s) => s.get(..4)?.parse().ok(),
        _ => None,
    }
}

/// The id a Zotero item keeps across imports, so journal references to it hold.
pub fn zotero_id(item: &Map<String, Value>) -> String {
    format!("zotero:{}/{}", item_str(item, "library"), item_str(item, "key"))
}

/// What a Zotero item matches a paper by: its DOI, and its normalized title with its year when
/// the title is long enough to be distinctive.
pub(crate) struct ItemKeys {
    pub doi: Option<String>,
    pub title_year: Option<(String, i64)>,
}

/// The match keys of an item, or `None` for an item to skip: one without a key, or deleted.
pub(crate) fn item_keys(item: &Map<String, Value>) -> Option<ItemKeys> {
    if item_str(item, "key").is_empty() || item.get("deleted").and_then(Value::as_bool) == Some(true) {
        return None;
    }
    let doi = Some(item_str(item, "doi").to_lowercase()).filter(|d| !d.is_empty());
    let title = norm_title(item_str(item, "title"));
    let title_year = item_year(item).filter(|_| title.len() >= MIN_TITLE).map(|y| (title, y));
    Some(ItemKeys { doi, title_year })
}

/// The shortest normalized title that matches by title and year.
const MIN_TITLE: usize = 20;

/// Add [`LIBRARY`] to each semdb paper a Zotero item matches by [`item_keys`]. Returns the
/// unmatched items as rows to embed.
fn join_library(papers: &mut HashMap<String, Paper>, items: &[Map<String, Value>]) -> Vec<Map<String, Value>> {
    let by_doi: HashMap<String, String> = papers.iter().filter(|(_, p)| !p.doi.is_empty()).map(|(id, p)| (p.doi.clone(), id.clone())).collect();
    let by_title: HashMap<(String, i64), String> = papers.iter().filter_map(|(id, p)| Some(((norm_title(&p.title), p.year?), id.clone()))).collect();
    let mut held = Vec::new();
    let mut seen = HashSet::new();
    for item in items {
        let Some(keys) = item_keys(item) else { continue };
        if !seen.insert(zotero_id(item)) {
            continue;
        }
        let found = keys.doi.and_then(|d| by_doi.get(&d)).or_else(|| keys.title_year.and_then(|k| by_title.get(&k))).cloned();
        match found.and_then(|id| papers.get_mut(&id)) {
            Some(p) => {
                if !p.collections.iter().any(|c| c == LIBRARY) {
                    p.collections.push(LIBRARY.into());
                }
            }
            None => held.push(held_row(item)),
        }
    }
    held
}

/// A Zotero item semdb lacks, as an entry: its title and abstract, embedded by the import.
pub fn held_row(item: &Map<String, Value>) -> Map<String, Value> {
    let authors: Vec<String> = match item.get("creators") {
        Some(Value::Array(a)) => a.iter().map(|c| c.as_str().map_or_else(|| c.to_string(), str::to_string)).collect(),
        _ => vec![],
    };
    let mut row = Map::new();
    row.insert("id".into(), json!(zotero_id(item)));
    row.insert("title".into(), json!(item_str(item, "title")));
    row.insert("text".into(), json!(item_str(item, "abstract")));
    row.insert("body".into(), json!(""));
    if let Some(y) = item_year(item) {
        row.insert("ts".into(), json!(year_start(y)));
    }
    row.insert("collection".into(), json!([LIBRARY]));
    row.insert("author".into(), json!(authors));
    row.insert("doi".into(), json!(item_str(item, "doi").to_lowercase()));
    row.insert("journal".into(), json!(item_str(item, "publication")));
    row
}

/// The start of year `y`, UTC, as a paper's `ts`.
fn year_start(y: i64) -> String {
    format!("{y:04}-01-01T00:00:00Z")
}

fn open_ro(path: &Path) -> Result<rusqlite::Connection> {
    rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX)
        .with_context(|| path.display().to_string())
}

/// Call `f` with each `(key, text)` chunk of one semdb zone, in id order. Rowid order reads the
/// file front to back. The key index would read it at random, which takes hours on a busy disk.
fn zone_rows(semdb: &Path, zone: &str, mut f: impl FnMut(String, String) -> Result<()>) -> Result<()> {
    let db = open_ro(&semdb.join("chunks").join(format!("{zone}.sqlite")))?;
    let mut q = db.prepare("SELECT key, text FROM chunk ORDER BY id")?;
    let mut rows = q.query([])?;
    while let Some(r) = rows.next()? {
        f(r.get(0)?, r.get(1)?)?;
    }
    Ok(())
}

fn join_text(into: &mut String, text: &str) {
    if !into.is_empty() {
        into.push_str("\n\n");
    }
    into.push_str(text);
}

/// Each paper's text in one semdb zone, its chunks joined in id order.
fn zone_texts(semdb: &Path, zone: &str) -> Result<HashMap<String, String>> {
    let mut out: HashMap<String, String> = HashMap::new();
    zone_rows(semdb, zone, |key, text| {
        join_text(out.entry(key).or_default(), &text);
        Ok(())
    })?;
    Ok(out)
}

/// Write one entry per registry paper: title, abstract and body from semdb's chunks.
async fn write_entries(
    index: &Index,
    semdb: &Path,
    papers: &HashMap<String, Paper>,
    cites: &HashMap<String, Vec<String>>,
    cited_by: &HashMap<String, Vec<String>>,
) -> Result<usize> {
    let titles = zone_texts(semdb, "titles")?;
    let abstracts = zone_texts(semdb, "abstracts")?;
    let entry = |id: &str, body: String| -> Option<Map<String, Value>> {
        let p = papers.get(id)?;
        let title = titles.get(id).cloned().unwrap_or_else(|| p.title.clone());
        let mut m = Map::new();
        m.insert("id".into(), json!(id));
        m.insert("title".into(), json!(title));
        m.insert("text".into(), json!(abstracts.get(id).cloned().unwrap_or_default()));
        m.insert("body".into(), json!(body));
        m.insert("ts".into(), p.year.map_or(Value::Null, |y| json!(year_start(y))));
        m.insert("collection".into(), json!(p.collections));
        m.insert("author".into(), json!(p.authors));
        m.insert("cites".into(), json!(cites.get(id).cloned().unwrap_or_default()));
        m.insert("cited_by".into(), json!(cited_by.get(id).cloned().unwrap_or_default()));
        m.insert("doi".into(), json!(p.doi));
        m.insert("journal".into(), json!(p.journal));
        Some(m)
    };
    // semdb writes each paper's chunks together, so a paper's body is one run of rows. The
    // reader thread sends each run, and a key seen twice means the runs are not contiguous.
    let (tx, mut rx) = tokio::sync::mpsc::channel::<(String, String)>(ENTRIES);
    let dir = semdb.to_path_buf();
    let reader = tokio::task::spawn_blocking(move || -> Result<()> {
        let (mut key, mut body) = (String::new(), String::new());
        zone_rows(&dir, "chunks", |k, text| {
            if k != key {
                if !key.is_empty() {
                    tx.blocking_send((std::mem::take(&mut key), std::mem::take(&mut body)))?;
                }
                key = k;
            }
            join_text(&mut body, &text);
            Ok(())
        })?;
        if !key.is_empty() {
            tx.blocking_send((key, body))?;
        }
        Ok(())
    });
    let (mut batch, mut n, mut bodied) = (Vec::new(), 0, HashSet::new());
    while let Some((key, body)) = rx.recv().await {
        batch.extend(entry(&key, body));
        anyhow::ensure!(bodied.insert(key.clone()), "the chunks of {key} are not contiguous in chunks.sqlite");
        if batch.len() >= ENTRIES {
            n += index.load_entries(&std::mem::take(&mut batch)).await?;
        }
    }
    reader.await??;
    for id in papers.keys().filter(|id| !bodied.contains(*id)) {
        batch.extend(entry(id, String::new()));
        if batch.len() >= ENTRIES {
            n += index.load_entries(&std::mem::take(&mut batch)).await?;
        }
    }
    n += index.load_entries(&batch).await?;
    Ok(n)
}

/// The `.npy` header of a row-major `<f4` matrix: its rows, columns and data offset.
fn npy_header(r: &mut impl Read) -> Result<(usize, usize, u64)> {
    let mut magic = [0u8; 10];
    r.read_exact(&mut magic)?;
    if &magic[..6] != b"\x93NUMPY" {
        bail!("not a .npy file");
    }
    let (len, skip) = match magic[6] {
        1 => (u16::from_le_bytes([magic[8], magic[9]]) as usize, 10u64),
        _ => {
            let mut more = [0u8; 2];
            r.read_exact(&mut more)?;
            (u32::from_le_bytes([magic[8], magic[9], more[0], more[1]]) as usize, 12u64)
        }
    };
    let mut head = vec![0u8; len];
    r.read_exact(&mut head)?;
    let head = String::from_utf8_lossy(&head);
    if !head.contains("'<f4'") || head.contains("'fortran_order': True") {
        bail!("expected a C-order <f4 matrix, got {head}");
    }
    let shape = head.split("'shape': (").nth(1).and_then(|s| s.split(')').next()).context("no shape")?;
    let dims: Vec<usize> = shape.split(',').filter_map(|d| d.trim().parse().ok()).collect();
    match dims[..] {
        [rows, cols] => Ok((rows, cols, skip + len as u64)),
        _ => bail!("expected two dimensions, got {shape}"),
    }
}

/// Write one zone's chunk rows: its texts' ids in order, each with its row of the zone's vectors.
async fn write_chunks(index: &Index, semdb: &Path, zone: &str, papers: &HashMap<String, Paper>) -> Result<usize> {
    let npy = semdb.join("vectors").join(zone).join("index.npy");
    let mut vectors = BufReader::with_capacity(1 << 24, std::fs::File::open(&npy).with_context(|| npy.display().to_string())?);
    let (rows_n, dim, _) = npy_header(&mut vectors)?;
    if dim != BGE_LARGE.dim {
        bail!("{} holds {dim}-wide vectors, not {}", npy.display(), BGE_LARGE.dim);
    }
    let cols = index.spec.chunk_cols();
    let db = open_ro(&semdb.join("chunks").join(format!("{zone}.sqlite")))?;
    let mut q = db.prepare("SELECT key, text FROM chunk ORDER BY id")?;
    let mut rows = q.query([])?;
    let base = match zone {
        "titles" => 0,
        "abstracts" => 1,
        _ => 2,
    };
    let mut ordinal: HashMap<String, i64> = HashMap::new();
    let (mut batch, mut n) = (Vec::with_capacity(ROWS), 0usize);
    let mut buf = vec![0u8; dim * 4];
    while let Some(r) = rows.next()? {
        let (key, text): (String, String) = (r.get(0)?, r.get(1)?);
        if n == rows_n {
            bail!("{zone}: more chunks than the {rows_n} vectors in {}", npy.display());
        }
        vectors.read_exact(&mut buf)?;
        n += 1;
        let Some(p) = papers.get(&key) else { continue };
        let j = ordinal.entry(key.clone()).or_insert(base);
        let vector: Vec<f32> = buf.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
        let row = cols
            .iter()
            .map(|(name, _)| match name.as_str() {
                "id" => Cell::Text(key.clone()),
                "chunk" => Cell::Int(*j),
                "hash" => Cell::Text(text_hash(&text)),
                "ts" => p.year.map_or(Cell::Null, |y| Cell::Time(year_us(y))),
                "collection" => Cell::Texts(p.collections.clone()),
                "vector" => Cell::Vector(vector.clone()),
                _ => Cell::Null,
            })
            .collect();
        if base == 2 {
            *j += 1;
        }
        batch.push(row);
        if batch.len() >= ROWS {
            index.load_chunks("", &batch).await?;
            batch.clear();
        }
    }
    if n != rows_n {
        bail!("{zone}: {n} chunks but {rows_n} vectors in {}", npy.display());
    }
    index.load_chunks("", &batch).await?;
    Ok(n)
}

/// Microseconds since the epoch at the start of year `y`, UTC.
fn year_us(y: i64) -> i64 {
    awm_index::cells::parse_ts(&json!(year_start(y))).unwrap_or(0)
}

/// Point `CURRENT` at generation `name`, atomically.
pub fn make_current(root: &Path, name: &str) -> Result<()> {
    write_atomic(&root.join(CURRENT), &format!("{name}\n"))
}

/// Replace `path` with `text` through a sibling temp file, so a reader never sees half of it.
pub(crate) fn write_atomic(path: &Path, text: &str) -> Result<()> {
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".tmp");
    std::fs::write(&tmp, text)?;
    std::fs::rename(&tmp, path)?;
    Ok(())
}

/// Delete every generation but `keep` and the one before it, which kb may still be serving.
fn prune(root: &Path, keep: &str) -> Result<()> {
    let mut gens: Vec<String> = std::fs::read_dir(root)?
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_dir())
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with("gen-") && n.as_str() != keep)
        .collect();
    gens.sort();
    gens.pop();
    for g in gens {
        std::fs::remove_dir_all(root.join(g))?;
    }
    Ok(())
}
