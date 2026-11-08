//! search's own database, in place at `<AWM_DIR>/services/search/search.db`: the judgements and the
//! usage log. The `sources` table and its feed columns stay on disk, unread, so the schema is the
//! Python's v9 and a rollback reads the file as it is.

use awm_pycompat::time::{fromisoformat, zulu, DateTime};
use awm_store::rusqlite::{params, Connection, OptionalExtension};
use awm_store::{Migration, Store, StoreError};
use serde_json::{json, Map, Value};
use std::path::Path;

pub const SCHEMA_VERSION: i64 = 9;
pub const VERDICTS: [&str; 2] = ["relevant", "irrelevant"];
pub const SECTIONS: [&str; 2] = ["exact", "related"];

const SOURCES: &str = "
CREATE TABLE IF NOT EXISTS sources (
    name        TEXT PRIMARY KEY,
    service     TEXT NOT NULL,
    mode        TEXT NOT NULL,
    spec        TEXT NOT NULL,       -- the Source as JSON, kept so a frozen source stays described
    present     INTEGER NOT NULL DEFAULT 1,
    synced_at   REAL,                -- end of the last feed read that succeeded
    attempt_at  REAL,
    last_error  TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    cursor      TEXT,                -- the feed's `next` after the last page applied
    epoch       TEXT,                -- the feed's epoch that cursor belongs to
    layout      TEXT                 -- the layout the source's tables were built with
);
";

const JUDGEMENTS: &str = "
CREATE TABLE IF NOT EXISTS judgements (
    query       TEXT NOT NULL,
    source      TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    verdict     TEXT NOT NULL CHECK (verdict IN ('relevant', 'irrelevant')),
    section     TEXT NOT NULL DEFAULT '',  -- the section a relevant hit must answer in; '' for any
    collection  TEXT NOT NULL DEFAULT '',  -- the set it was seeded from, or '' for a mark by hand
    note        TEXT NOT NULL DEFAULT '',
    judge       TEXT NOT NULL DEFAULT '',  -- the caller who recorded it
    ts          REAL NOT NULL,
    PRIMARY KEY (query, source, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_judgements_collection ON judgements(collection);
";

const USAGE: &str = "
CREATE TABLE IF NOT EXISTS usage (
    ts      REAL NOT NULL,
    scope   TEXT NOT NULL DEFAULT '',  -- the caller's scope, within its project
    query   TEXT NOT NULL DEFAULT '',
    source  TEXT NOT NULL,
    doc_id  TEXT NOT NULL,
    node    TEXT NOT NULL DEFAULT '',  -- the node holding the document; 'local' for this one
    project TEXT NOT NULL DEFAULT ''   -- the caller's project
);
CREATE INDEX IF NOT EXISTS idx_usage_scope ON usage(scope, ts);
";

const USAGE_SCORE: &str = "
CREATE TABLE IF NOT EXISTS usage_score (
    source TEXT NOT NULL, doc_id TEXT NOT NULL, node TEXT NOT NULL DEFAULT '', s REAL NOT NULL,
    PRIMARY KEY (source, doc_id, node));
CREATE INDEX IF NOT EXISTS idx_usage_score_rank ON usage_score(source, s DESC);
";

/// v9 moved the documents to LanceDB: the SQLite copies, their text indexes and the embeddings went.
const TO_LANCE: &str = "
DROP TRIGGER IF EXISTS documents_exact_ai;
DROP TRIGGER IF EXISTS documents_exact_ad;
DROP TRIGGER IF EXISTS documents_exact_au;
DROP TRIGGER IF EXISTS passages_words_ai;
DROP TRIGGER IF EXISTS passages_words_ad;
DROP TRIGGER IF EXISTS doc_values_ad;
DROP TABLE IF EXISTS documents_exact;
DROP TABLE IF EXISTS passages_words;
DROP TABLE IF EXISTS passages;
DROP TABLE IF EXISTS doc_values;
DROP TABLE IF EXISTS documents;
DROP TABLE IF EXISTS embeddings_fts;
DROP TABLE IF EXISTS embeddings;
ALTER TABLE sources ADD COLUMN cursor TEXT;
ALTER TABLE sources ADD COLUMN epoch TEXT;
ALTER TABLE sources ADD COLUMN layout TEXT;
UPDATE sources SET synced_at = NULL;
";

pub const MIGRATIONS: [Migration; 1] = [Migration { from: 8, sql: TO_LANCE }];

pub fn schema() -> String {
    [SOURCES, JUDGEMENTS, USAGE, USAGE_SCORE].concat()
}

/// Open and create or migrate `<state_dir>/search.db`.
pub fn open(state_dir: &Path) -> Result<Store, StoreError> {
    let store = Store::open(state_dir.join("search.db"))?;
    store.init(&schema(), SCHEMA_VERSION, &MIGRATIONS)?;
    Ok(store)
}

/// `2026-03-01T12:00:00Z` for ISO 8601 text, or `""`. A naive value is UTC, a date is midnight.
pub fn normalize_ts(value: &Value) -> String {
    value.as_str().and_then(awm_index::grammar::normalize_ts).unwrap_or_default()
}

/// Seconds since the epoch as `2026-03-01T12:00:00Z`.
pub fn iso(epoch: Option<f64>) -> Value {
    match epoch {
        None => Value::Null,
        Some(t) => Value::String(zulu(&DateTime::from_utc_us((t * 1e6).floor() as i64))),
    }
}

pub fn now() -> f64 {
    awm_pycompat::time::now_us() as f64 / 1e6
}

/// usage_score counts a get as exp(LAMBDA * t), t in days since EPOCH, kept as a log so a score
/// never overflows: a get 30 days later weighs twice as much.
pub fn epoch() -> f64 {
    fromisoformat("2026-01-01T00:00:00+00:00").map(|d| d.utc_us() as f64 / 1e6).unwrap_or(1_767_225_600.0)
}

pub fn lambda() -> f64 {
    std::f64::consts::LN_2 / 30.0
}

pub fn logaddexp(a: f64, b: f64) -> f64 {
    let (hi, lo) = if a >= b { (a, b) } else { (b, a) };
    hi + (lo - hi).exp().ln_1p()
}

/// One get, as the usage log keeps it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Use {
    pub ts: Option<f64>,
    pub project: String,
    pub scope: String,
    pub query: String,
    pub source: String,
    pub doc_id: String,
    pub node: String,
}

/// Append one get to the usage log and fold it into the document's `usage_score`.
pub fn log_use(conn: &Connection, row: &Use) -> Result<(), StoreError> {
    let ts = row.ts.unwrap_or_else(now);
    conn.execute(
        "INSERT INTO usage (ts, project, scope, query, source, doc_id, node) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![ts, row.project, row.scope, row.query, row.source, row.doc_id, row.node],
    )?;
    let gain = lambda() * (ts - epoch()) / 86400.0;
    let held: Option<f64> = conn
        .query_row(
            "SELECT s FROM usage_score WHERE source=?1 AND doc_id=?2 AND node=?3",
            params![row.source, row.doc_id, row.node],
            |r| r.get(0),
        )
        .optional()?;
    conn.execute(
        "INSERT INTO usage_score (source, doc_id, node, s) VALUES (?1, ?2, ?3, ?4) \
         ON CONFLICT (source, doc_id, node) DO UPDATE SET s = excluded.s",
        params![row.source, row.doc_id, row.node, held.map_or(gain, |h| logaddexp(h, gain))],
    )?;
    Ok(())
}

/// One verdict, as `judge` records it.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Verdict {
    pub query: String,
    pub source: String,
    pub doc_id: String,
    pub verdict: String,
    pub section: String,
    pub collection: String,
    pub note: String,
    pub judge: String,
    pub ts: Option<f64>,
}

/// Record a verdict on one hit for one query, replacing any earlier one on the same pair. The
/// row comes back with its keys in the Python's order.
pub fn judge(conn: &Connection, v: &Verdict) -> Result<Value, StoreError> {
    let ts = v.ts.unwrap_or_else(now);
    conn.execute(
        "INSERT INTO judgements (query, source, doc_id, verdict, section, collection, note, judge, ts) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9) \
         ON CONFLICT (query, source, doc_id) DO UPDATE SET verdict=excluded.verdict, \
         section=excluded.section, collection=excluded.collection, note=excluded.note, \
         judge=excluded.judge, ts=excluded.ts",
        params![v.query, v.source, v.doc_id, v.verdict, v.section, v.collection, v.note, v.judge, ts],
    )?;
    Ok(json!({
        "section": v.section, "collection": v.collection, "note": v.note, "judge": v.judge, "ts": ts,
        "query": v.query, "source": v.source, "doc_id": v.doc_id, "verdict": v.verdict,
    }))
}

pub fn unjudge(conn: &Connection, query: &str, source: &str, doc_id: &str) -> Result<usize, StoreError> {
    Ok(conn.execute("DELETE FROM judgements WHERE query=?1 AND source=?2 AND doc_id=?3", params![query, source, doc_id])?)
}

pub fn judgements(conn: &Connection, collection: &str, query: &str) -> Result<Vec<Value>, StoreError> {
    let mut sql = String::from("SELECT * FROM judgements WHERE 1=1");
    let mut args: Vec<&str> = Vec::new();
    if !collection.is_empty() {
        sql.push_str(" AND collection=?");
        args.push(collection);
    }
    if !query.is_empty() {
        sql.push_str(" AND query=?");
        args.push(query);
    }
    sql.push_str(" ORDER BY collection, query, source, doc_id");
    let mut stmt = conn.prepare(&sql)?;
    awm_store::rows_json(&mut stmt, awm_store::rusqlite::params_from_iter(args))
}

/// The usage rows, oldest first, for tests and the replay.
pub fn usage(conn: &Connection) -> Result<Vec<Map<String, Value>>, StoreError> {
    let mut stmt = conn.prepare("SELECT project, scope, query, source, doc_id, node FROM usage ORDER BY rowid")?;
    Ok(awm_store::rows_json(&mut stmt, [])?.into_iter().filter_map(|v| v.as_object().cloned()).collect())
}
