//! The run history in `transcripts.db`, schema v2, as the Python wrote it. Rows are
//! append-only: a sweep that failed partway still writes its row, with its failures.

use awm_store::{rows_json, Migration, Store, StoreError};
use serde_json::Value;

pub const SCHEMA_VERSION: i64 = 2;

pub const SCHEMA_SQL: &str = "\
CREATE TABLE IF NOT EXISTS transcripts_runs (
    id             TEXT    NOT NULL PRIMARY KEY,
    kind           TEXT    NOT NULL,
    trigger        TEXT    NOT NULL DEFAULT 'manual',
    dry_run        INTEGER NOT NULL DEFAULT 0,
    retention_days REAL    NOT NULL DEFAULT 0,
    sessions       INTEGER NOT NULL DEFAULT 0,
    orphans        INTEGER NOT NULL DEFAULT 0,
    files          INTEGER NOT NULL DEFAULT 0,
    vanished       INTEGER NOT NULL DEFAULT 0,
    bytes_before   INTEGER NOT NULL DEFAULT 0,
    bytes_after    INTEGER NOT NULL DEFAULT 0,
    failures       INTEGER NOT NULL DEFAULT 0,
    detail         TEXT    NOT NULL DEFAULT '',
    started_at     TEXT    NOT NULL DEFAULT '',
    seconds        REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS transcripts_runs_started
    ON transcripts_runs (started_at DESC);
";

/// v2 added `vanished`, the files that went between the listing and the copy.
pub const MIGRATIONS: &[Migration] = &[Migration { from: 1, sql: "ALTER TABLE transcripts_runs ADD COLUMN vanished INTEGER NOT NULL DEFAULT 0;" }];

pub fn open(store: &Store) -> Result<(), StoreError> {
    store.init(SCHEMA_SQL, SCHEMA_VERSION, MIGRATIONS).map(|_| ())
}

fn int(v: &Value) -> i64 {
    v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)).unwrap_or(0)
}

/// `datetime.now(timezone.utc).isoformat(timespec="seconds")`.
fn now_iso() -> String {
    let secs = crate::sweep::now().floor() as i64;
    awm_pycompat::time::DateTime::from_utc_us(secs * 1_000_000).isoformat()
}

/// Record a sweep or prune result, and return the run id.
pub async fn record(store: &Store, kind: &str, result: &Value, trigger: &str) -> Result<String, StoreError> {
    let id = awm_store::new_uuid();
    let failures = result["failed"].as_array().map_or(0, Vec::len) as i64;
    let bytes_after = match int(&result["bytes_after"]) {
        0 => int(&result["bytes_freed"]),
        n => n,
    };
    let row = (
        id.clone(),
        kind.to_string(),
        trigger.to_string(),
        result["dry_run"].as_bool().unwrap_or(false) as i64,
        result["retention_days"].as_f64().unwrap_or(0.0),
        [int(&result["sessions"]), int(&result["orphans"]), int(&result["files"]), int(&result["vanished"]), int(&result["bytes_before"]), bytes_after, failures],
        now_iso(),
        result["seconds"].as_f64().unwrap_or(0.0),
    );
    store
        .write(move |tx| {
            let (id, kind, trigger, dry_run, days, n, started, seconds) = row;
            tx.execute(
                "INSERT INTO transcripts_runs (id, kind, trigger, dry_run, retention_days, sessions, orphans, files, vanished, \
                 bytes_before, bytes_after, failures, detail, started_at, seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                awm_store::rusqlite::params![id, kind, trigger, dry_run, days, n[0], n[1], n[2], n[3], n[4], n[5], n[6], "", started, seconds],
            )?;
            Ok::<_, StoreError>(())
        })
        .await?;
    Ok(id)
}

/// The newest runs first.
pub async fn recent(store: &Store, limit: i64) -> Result<Vec<Value>, StoreError> {
    store
        .read(move |conn| {
            let mut stmt = conn.prepare("SELECT * FROM transcripts_runs ORDER BY started_at DESC LIMIT ?")?;
            rows_json(&mut stmt, [limit])
        })
        .await
}

/// When the last timer sweep started, as Unix seconds.
pub async fn last_timer(store: &Store) -> Result<Option<f64>, StoreError> {
    let started: Option<String> = store
        .read(|conn| {
            conn.query_row("SELECT max(started_at) FROM transcripts_runs WHERE kind = 'sweep' AND trigger = 'timer'", [], |r| r.get(0))
                .map_err(StoreError::from)
        })
        .await?;
    Ok(started.and_then(|s| awm_pycompat::time::fromisoformat(&s)).map(|dt| dt.utc_us() as f64 / 1e6))
}
