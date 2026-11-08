//! Schedule state (`dvc_jobs`) and run history (`dvc_runs`) on `services/dvc/dvc.db`, the
//! schema Python wrote, read in place.
//!
//! The single-flight guard is the partial unique index `dvc_runs_single_flight`: the insert
//! is the acquire, so the service and `dvc run-job` cannot stack a second whole-tree scan.
//! SQLite matches a partial index by its expression text, so `LIVE_SQL` must say exactly
//! what the index says.

use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use rusqlite::{params, params_from_iter, Connection, ErrorCode, OptionalExtension, ToSql};
use serde_json::{Map, Value};

use crate::{new_uuid, now, now_iso};

pub const SCHEMA_VERSION: i64 = 1;
pub const LIVE: [&str; 2] = ["submitting", "running"];
const LIVE_SQL: &str = "('submitting', 'running')";

pub const SCHEMA: &str = "\
CREATE TABLE IF NOT EXISTS dvc_jobs (
    name             TEXT    NOT NULL PRIMARY KEY,
    cron             TEXT    NOT NULL DEFAULT '',
    enabled          INTEGER NOT NULL DEFAULT 1,
    catchup_window_s INTEGER NOT NULL DEFAULT 0,
    last_fire_at     REAL    NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT '',
    updated_at       TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dvc_runs (
    id                TEXT    NOT NULL PRIMARY KEY,
    job               TEXT    NOT NULL,
    trigger           TEXT    NOT NULL DEFAULT 'manual',
    task_id           TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL DEFAULT 'submitting',
    label             TEXT    NOT NULL DEFAULT '',
    started_at        REAL    NOT NULL DEFAULT 0,
    submitted_at      REAL    NOT NULL DEFAULT 0,
    polled_at         REAL    NOT NULL DEFAULT 0,
    finished_at       REAL    NOT NULL DEFAULT 0,
    files             INTEGER NOT NULL DEFAULT 0,
    files_transferred INTEGER NOT NULL DEFAULT 0,
    files_skipped     INTEGER NOT NULL DEFAULT 0,
    bytes_transferred INTEGER NOT NULL DEFAULT 0,
    faults            INTEGER NOT NULL DEFAULT 0,
    nice_status       TEXT    NOT NULL DEFAULT '',
    error             TEXT    NOT NULL DEFAULT '',
    note              TEXT    NOT NULL DEFAULT '',
    alerted_at        REAL    NOT NULL DEFAULT 0
);

-- Load-bearing: this is the single-flight guard. See the module docstring.
CREATE UNIQUE INDEX IF NOT EXISTS dvc_runs_single_flight
    ON dvc_runs (job) WHERE status IN ('submitting', 'running');
-- Adoption idempotence: one row per Globus task, so a task can never be
-- watched by two rows that then disagree about its verdict.
CREATE UNIQUE INDEX IF NOT EXISTS dvc_runs_one_row_per_task
    ON dvc_runs (task_id) WHERE task_id != '';
-- The adopt sweep's scan, run every tick.
CREATE INDEX IF NOT EXISTS dvc_runs_live
    ON dvc_runs (status) WHERE status IN ('submitting', 'running');
CREATE INDEX IF NOT EXISTS dvc_runs_history
    ON dvc_runs (job, started_at DESC);
";

const RUN_COLS: &str = "id, job, trigger, task_id, status, label, started_at, submitted_at, \
    polled_at, finished_at, files, files_transferred, files_skipped, \
    bytes_transferred, faults, nice_status, error, note, alerted_at";

/// The Globus counters copied from a task document onto a run row.
pub const COUNTERS: [&str; 5] = ["files", "files_transferred", "files_skipped", "bytes_transferred", "faults"];

pub type Row = Map<String, Value>;

#[derive(Debug, thiserror::Error)]
pub enum RunsError {
    #[error(transparent)]
    Db(#[from] rusqlite::Error),
    #[error("{0}")]
    Io(#[from] std::io::Error),
}

pub type Result<T> = std::result::Result<T, RunsError>;

fn row(r: &rusqlite::Row<'_>) -> rusqlite::Result<Row> {
    let mut m = Map::new();
    for (i, name) in r.as_ref().column_names().enumerate() {
        let v = match r.get_ref(i)? {
            rusqlite::types::ValueRef::Null => Value::Null,
            rusqlite::types::ValueRef::Integer(n) => Value::from(n),
            rusqlite::types::ValueRef::Real(f) => serde_json::Number::from_f64(f).map(Value::Number).unwrap_or(Value::Null),
            rusqlite::types::ValueRef::Text(t) => Value::String(String::from_utf8_lossy(t).into_owned()),
            rusqlite::types::ValueRef::Blob(b) => Value::String(String::from_utf8_lossy(b).into_owned()),
        };
        m.insert(name.to_string(), v);
    }
    Ok(m)
}

fn counter(doc: &Value, k: &str) -> i64 {
    match doc.get(k) {
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)).unwrap_or(0),
        Some(Value::String(s)) => s.trim().parse().unwrap_or(0),
        Some(Value::Bool(b)) => i64::from(*b),
        _ => 0,
    }
}

fn text(doc: &Value, k: &str) -> String {
    match doc.get(k) {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(v) => v.to_string(),
    }
}

fn clip(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

fn is_unique_violation(e: &rusqlite::Error) -> bool {
    matches!(e, rusqlite::Error::SqliteFailure(f, _) if f.code == ErrorCode::ConstraintViolation)
}

pub struct RunsDao {
    pub path: PathBuf,
    conn: Mutex<Connection>,
}

impl RunsDao {
    /// Open the store, creating its tables on a fresh database as Python's `init_service_db` did.
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let conn = Connection::open(path)?;
        conn.busy_timeout(Duration::from_secs(30))?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        let version: Option<i64> = conn.query_row("SELECT version FROM schema_version", [], |r| r.get(0)).optional().unwrap_or(None);
        if version.is_none() {
            conn.execute_batch(SCHEMA)?;
            conn.execute_batch("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")?;
            let existing: Option<i64> = conn.query_row("SELECT version FROM schema_version", [], |r| r.get(0)).optional()?;
            match existing {
                None => conn.execute("INSERT INTO schema_version (version) VALUES (?)", [SCHEMA_VERSION])?,
                Some(_) => conn.execute("UPDATE schema_version SET version = ?", [SCHEMA_VERSION])?,
            };
        }
        Ok(Self { path: path.to_path_buf(), conn: Mutex::new(conn) })
    }

    fn conn(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().unwrap_or_else(|p| p.into_inner())
    }

    pub fn execute(&self, sql: &str, args: &[&dyn ToSql]) -> Result<usize> {
        Ok(self.conn().execute(sql, args)?)
    }

    fn one(&self, sql: &str, args: &[&dyn ToSql]) -> Result<Option<Row>> {
        Ok(self.conn().query_row(sql, args, row).optional()?)
    }

    fn all(&self, sql: &str, args: &[&dyn ToSql]) -> Result<Vec<Row>> {
        let c = self.conn();
        let mut st = c.prepare(sql)?;
        let rows = st.query_map(args, row)?.collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }

    // -- jobs ---------------------------------------------------------------------------

    /// Insert the job's row if absent. An existing row is state an operator may have
    /// changed, so a restart never reverts it.
    pub fn seed_job(&self, name: &str, cron: &str, catchup_window_s: i64, enabled: bool) -> Result<Option<Row>> {
        let now = now_iso();
        self.execute(
            "INSERT INTO dvc_jobs (name, cron, enabled, catchup_window_s, created_at, updated_at) \
             VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO NOTHING",
            &[&name, &cron, &i64::from(enabled), &catchup_window_s, &now, &now],
        )?;
        self.get_job(name)
    }

    pub fn get_job(&self, name: &str) -> Result<Option<Row>> {
        self.one("SELECT * FROM dvc_jobs WHERE name = ?", &[&name])
    }

    pub fn list_jobs(&self) -> Result<Vec<Row>> {
        self.all("SELECT * FROM dvc_jobs ORDER BY name", &[])
    }

    pub fn set_schedule(&self, name: &str, cron: Option<&str>, enabled: Option<bool>) -> Result<Option<Row>> {
        let mut sets: Vec<&str> = vec![];
        let mut vals: Vec<Box<dyn ToSql>> = vec![];
        if let Some(c) = cron {
            sets.push("cron = ?");
            vals.push(Box::new(c.to_string()));
        }
        if let Some(e) = enabled {
            sets.push("enabled = ?");
            vals.push(Box::new(i64::from(e)));
        }
        if !sets.is_empty() {
            sets.push("updated_at = ?");
            vals.push(Box::new(now_iso()));
            vals.push(Box::new(name.to_string()));
            let sql = format!("UPDATE dvc_jobs SET {} WHERE name = ?", sets.join(", "));
            self.conn().execute(&sql, params_from_iter(vals.iter().map(|b| b.as_ref())))?;
        }
        self.get_job(name)
    }

    pub fn set_last_fire(&self, name: &str, when: f64) -> Result<()> {
        self.execute("UPDATE dvc_jobs SET last_fire_at = ?, updated_at = ? WHERE name = ?", &[&when, &now_iso(), &name])?;
        Ok(())
    }

    // -- runs: creation -----------------------------------------------------------------

    /// Claim the job's single-flight slot with a fresh `submitting` row, or None when another
    /// run holds it. The unique-index refusal is the answer, so there is no check-then-act gap.
    pub fn begin_run(&self, job: &str, trigger: &str, note: &str) -> Result<Option<Row>> {
        let id = new_uuid();
        match self.conn().execute(
            "INSERT INTO dvc_runs (id, job, trigger, status, started_at, note) VALUES (?, ?, ?, 'submitting', ?, ?)",
            params![id, job, trigger, now(), note],
        ) {
            Ok(_) => {}
            Err(e) if is_unique_violation(&e) => return Ok(None),
            Err(e) => return Err(e.into()),
        }
        self.get_run(&id)
    }

    /// A declined run, as a row. The in-flight task's id goes in `note`: `task_id` is unique
    /// per task and belongs to the run that owns the transfer.
    pub fn record_skip(&self, job: &str, trigger: &str, note: &str) -> Result<Option<Row>> {
        let id = new_uuid();
        let t = now();
        self.execute(
            "INSERT INTO dvc_runs (id, job, trigger, status, started_at, finished_at, note) VALUES (?, ?, ?, 'skipped', ?, ?, ?)",
            &[&id, &job, &trigger, &t, &t, &note],
        )?;
        self.get_run(&id)
    }

    /// Seed a live row for a task a previous record knew about (the adopt sweep resolves it).
    pub fn adopt_task(&self, job: &str, task_id: &str, label: &str, note: &str) -> Result<bool> {
        let t = now();
        match self.conn().execute(
            "INSERT INTO dvc_runs (id, job, trigger, task_id, status, label, started_at, submitted_at, note) \
             VALUES (?, ?, 'adopted', ?, 'running', ?, ?, ?, ?)",
            params![new_uuid(), job, task_id, label, t, t, note],
        ) {
            Ok(_) => Ok(true),
            Err(e) if is_unique_violation(&e) => Ok(false),
            Err(e) => Err(e.into()),
        }
    }

    // -- runs: transitions --------------------------------------------------------------

    pub fn mark_submitted(&self, run_id: &str, task_id: &str, label: &str) -> Result<()> {
        self.execute(
            "UPDATE dvc_runs SET status = 'running', task_id = ?, label = ?, submitted_at = ?, error = '', finished_at = 0 WHERE id = ?",
            &[&task_id, &label, &now(), &run_id],
        )?;
        Ok(())
    }

    /// Persist a Globus status document. Always before the event that reports it.
    pub fn record_poll(&self, run_id: &str, doc: &Value) -> Result<()> {
        let c: Vec<i64> = COUNTERS.iter().map(|k| counter(doc, k)).collect();
        self.execute(
            "UPDATE dvc_runs SET polled_at = ?, nice_status = ?, files = ?, files_transferred = ?, files_skipped = ?, \
             bytes_transferred = ?, faults = ? WHERE id = ?",
            &[&now(), &text(doc, "nice_status"), &c[0], &c[1], &c[2], &c[3], &c[4], &run_id],
        )?;
        Ok(())
    }

    /// Stamp a terminal verdict, folding in the final counters.
    pub fn finish(&self, run_id: &str, status: &str, doc: &Value) -> Result<()> {
        let c: Vec<i64> = COUNTERS.iter().map(|k| counter(doc, k)).collect();
        let t = now();
        self.execute(
            "UPDATE dvc_runs SET status = ?, finished_at = ?, polled_at = ?, nice_status = ?, files = ?, \
             files_transferred = ?, files_skipped = ?, bytes_transferred = ?, faults = ? WHERE id = ?",
            &[&status, &t, &t, &text(doc, "nice_status"), &c[0], &c[1], &c[2], &c[3], &c[4], &run_id],
        )?;
        Ok(())
    }

    /// A run that never reached Globus: config, auth, or a build that failed.
    pub fn fail(&self, run_id: &str, error: &str) -> Result<()> {
        self.execute("UPDATE dvc_runs SET status = 'error', error = ?, finished_at = ? WHERE id = ?", &[&clip(error, 2000), &now(), &run_id])?;
        Ok(())
    }

    pub fn set_note(&self, run_id: &str, note: &str) -> Result<()> {
        self.execute("UPDATE dvc_runs SET note = ? WHERE id = ?", &[&clip(note, 2000), &run_id])?;
        Ok(())
    }

    pub fn mark_alerted(&self, run_id: &str) -> Result<()> {
        self.execute("UPDATE dvc_runs SET alerted_at = ? WHERE id = ?", &[&now(), &run_id])?;
        Ok(())
    }

    /// Retire whatever holds `job`'s slot, so a forced run can claim it.
    pub fn close_live(&self, job: &str, note: &str) -> Result<usize> {
        let sql = format!("UPDATE dvc_runs SET status = 'skipped', finished_at = ?, note = ? WHERE job = ? AND status IN {LIVE_SQL}");
        self.execute(&sql, &[&now(), &note, &job])
    }

    // -- runs: reads --------------------------------------------------------------------

    pub fn get_run(&self, run_id: &str) -> Result<Option<Row>> {
        self.one(&format!("SELECT {RUN_COLS} FROM dvc_runs WHERE id = ?"), &[&run_id])
    }

    pub fn run_for_task(&self, task_id: &str) -> Result<Option<Row>> {
        self.one(&format!("SELECT {RUN_COLS} FROM dvc_runs WHERE task_id = ?"), &[&task_id])
    }

    pub fn live_runs(&self) -> Result<Vec<Row>> {
        self.all(&format!("SELECT {RUN_COLS} FROM dvc_runs WHERE status IN {LIVE_SQL} ORDER BY started_at"), &[])
    }

    pub fn live_run(&self, job: &str) -> Result<Option<Row>> {
        self.one(&format!("SELECT {RUN_COLS} FROM dvc_runs WHERE job = ? AND status IN {LIVE_SQL}"), &[&job])
    }

    pub fn last_run(&self, job: &str) -> Result<Option<Row>> {
        self.one(&format!("SELECT {RUN_COLS} FROM dvc_runs WHERE job = ? ORDER BY started_at DESC LIMIT 1"), &[&job])
    }

    pub fn history(&self, job: Option<&str>, limit: i64, status: Option<&str>, since: Option<f64>) -> Result<Vec<Row>> {
        let mut wh: Vec<&str> = vec![];
        let mut vals: Vec<Box<dyn ToSql>> = vec![];
        if let Some(j) = job.filter(|j| !j.is_empty()) {
            wh.push("job = ?");
            vals.push(Box::new(j.to_string()));
        }
        if let Some(s) = status.filter(|s| !s.is_empty()) {
            wh.push("status = ?");
            vals.push(Box::new(s.to_string()));
        }
        if let Some(t) = since.filter(|t| *t != 0.0) {
            wh.push("started_at >= ?");
            vals.push(Box::new(t));
        }
        let clause = if wh.is_empty() { String::new() } else { format!("WHERE {}", wh.join(" AND ")) };
        vals.push(Box::new(limit.clamp(1, 500)));
        let sql = format!("SELECT {RUN_COLS} FROM dvc_runs {clause} ORDER BY started_at DESC LIMIT ?");
        let c = self.conn();
        let mut st = c.prepare(&sql)?;
        let rows = st.query_map(params_from_iter(vals.iter().map(|b| b.as_ref())), row)?.collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(rows)
    }
}
