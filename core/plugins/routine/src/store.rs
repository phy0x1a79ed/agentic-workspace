//! The routine service's own SQLite store: installed routines, their runs and their timer
//! slots. A partial unique index allows one live run per routine, so the database itself
//! refuses a second one (routines.md R38). The schema is Python's, plus `timer_slots`.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Map, Value};

use crate::{new_id, now};

pub const SCHEMA: &str = "\
CREATE TABLE IF NOT EXISTS routines (
    project      TEXT NOT NULL,
    name         TEXT NOT NULL,
    root         TEXT NOT NULL,
    path         TEXT NOT NULL,
    commit_sha   TEXT NOT NULL DEFAULT '',
    definition   TEXT NOT NULL,
    scheduled    INTEGER NOT NULL DEFAULT 0,
    installed_at TEXT NOT NULL,
    PRIMARY KEY (project, name)
);
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    name        TEXT NOT NULL,
    trigger     TEXT NOT NULL,
    caller      TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,
    failed_step TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    steps       TEXT NOT NULL DEFAULT '[]',
    dispatched  TEXT NOT NULL DEFAULT '[]',
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_run
    ON runs(project, name) WHERE state = 'running';
CREATE INDEX IF NOT EXISTS runs_by_routine ON runs(project, name, started_at);
CREATE TABLE IF NOT EXISTS timer_slots (
    project    TEXT NOT NULL,
    name       TEXT NOT NULL,
    on_calendar TEXT NOT NULL,
    jitter_s   INTEGER NOT NULL,
    catchup_s  INTEGER,
    last_slot  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project, name)
);
";

const JSON_COLS: [&str; 3] = ["steps", "dispatched", "definition"];

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("{0}")]
    LiveRunExists(String),
    #[error(transparent)]
    Db(#[from] rusqlite::Error),
}

/// One routine's timer settings and the last slot it served.
#[derive(Debug, Clone, PartialEq)]
pub struct TimerSlot {
    pub on_calendar: String,
    pub jitter_s: i64,
    pub catchup_s: Option<i64>,
    pub last_slot: String,
}

pub struct Store {
    pub path: PathBuf,
    conn: Mutex<Connection>,
}

type Row = Map<String, Value>;

fn row(r: &rusqlite::Row<'_>) -> rusqlite::Result<Row> {
    let mut m = Map::new();
    let names: Vec<String> = r.as_ref().column_names().map(str::to_string).collect();
    for (i, name) in names.iter().enumerate() {
        let v = match r.get_ref(i)? {
            rusqlite::types::ValueRef::Null => Value::Null,
            rusqlite::types::ValueRef::Integer(n) => json!(n),
            rusqlite::types::ValueRef::Real(f) => json!(f),
            rusqlite::types::ValueRef::Text(t) | rusqlite::types::ValueRef::Blob(t) => {
                let s = String::from_utf8_lossy(t).into_owned();
                if JSON_COLS.contains(&name.as_str()) {
                    serde_json::from_str(if s.is_empty() { "[]" } else { &s }).unwrap_or(Value::String(s))
                } else {
                    Value::String(s)
                }
            }
        };
        m.insert(name.clone(), v);
    }
    Ok(m)
}

impl Store {
    pub fn open(path: &Path) -> rusqlite::Result<Self> {
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let conn = Connection::open(path)?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.execute_batch(SCHEMA)?;
        Ok(Self { path: path.to_path_buf(), conn: Mutex::new(conn) })
    }

    fn conn(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn tx<T>(&self, f: impl FnOnce(&rusqlite::Transaction<'_>) -> rusqlite::Result<T>) -> rusqlite::Result<T> {
        let mut c = self.conn();
        let tx = c.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let out = f(&tx)?;
        tx.commit()?;
        Ok(out)
    }

    fn all(&self, sql: &str, args: &[&dyn rusqlite::ToSql]) -> rusqlite::Result<Vec<Row>> {
        let c = self.conn();
        let mut st = c.prepare(sql)?;
        let rows = st.query_map(args, row)?;
        rows.collect()
    }

    fn one(&self, sql: &str, args: &[&dyn rusqlite::ToSql]) -> rusqlite::Result<Option<Row>> {
        self.conn().query_row(sql, args, row).optional()
    }

    // -- routines -------------------------------------------------------------

    pub fn put_routine(&self, project: &str, name: &str, root: &str, path: &str, commit_sha: &str, definition: &Value) -> rusqlite::Result<Row> {
        self.tx(|c| {
            c.execute(
                "INSERT INTO routines(project, name, root, path, commit_sha, definition, installed_at) VALUES (?,?,?,?,?,?,?) \
                 ON CONFLICT(project, name) DO UPDATE SET root=excluded.root, path=excluded.path, commit_sha=excluded.commit_sha, \
                 definition=excluded.definition, installed_at=excluded.installed_at",
                params![project, name, root, path, commit_sha, definition.to_string(), now()],
            )
        })?;
        Ok(self.get_routine(project, name)?.unwrap_or_default())
    }

    pub fn get_routine(&self, project: &str, name: &str) -> rusqlite::Result<Option<Row>> {
        self.one("SELECT * FROM routines WHERE project=? AND name=?", &[&project, &name])
    }

    pub fn list_routines(&self, project: Option<&str>) -> rusqlite::Result<Vec<Row>> {
        match project.filter(|p| !p.is_empty()) {
            Some(p) => self.all("SELECT * FROM routines WHERE project=? ORDER BY name", &[&p]),
            None => self.all("SELECT * FROM routines ORDER BY project, name", &[]),
        }
    }

    pub fn delete_routine(&self, project: &str, name: &str) -> rusqlite::Result<bool> {
        self.tx(|c| {
            c.execute("DELETE FROM timer_slots WHERE project=? AND name=?", params![project, name])?;
            Ok(c.execute("DELETE FROM routines WHERE project=? AND name=?", params![project, name])? > 0)
        })
    }

    pub fn set_scheduled(&self, project: &str, name: &str, scheduled: bool) -> rusqlite::Result<()> {
        self.tx(|c| c.execute("UPDATE routines SET scheduled=? WHERE project=? AND name=?", params![scheduled as i64, project, name]).map(|_| ()))
    }

    // -- timer slots ----------------------------------------------------------

    /// Set a routine's timer settings, keeping the last slot it served.
    pub fn put_timer(&self, project: &str, name: &str, on_calendar: &str, jitter_s: i64, catchup_s: Option<i64>) -> rusqlite::Result<()> {
        self.tx(|c| {
            c.execute(
                "INSERT INTO timer_slots(project, name, on_calendar, jitter_s, catchup_s) VALUES (?,?,?,?,?) \
                 ON CONFLICT(project, name) DO UPDATE SET on_calendar=excluded.on_calendar, jitter_s=excluded.jitter_s, catchup_s=excluded.catchup_s",
                params![project, name, on_calendar, jitter_s, catchup_s],
            )
            .map(|_| ())
        })
    }

    pub fn timer(&self, project: &str, name: &str) -> rusqlite::Result<Option<TimerSlot>> {
        self.conn()
            .query_row("SELECT on_calendar, jitter_s, catchup_s, last_slot FROM timer_slots WHERE project=? AND name=?", params![project, name], |r| {
                Ok(TimerSlot { on_calendar: r.get(0)?, jitter_s: r.get(1)?, catchup_s: r.get(2)?, last_slot: r.get(3)? })
            })
            .optional()
    }

    /// Move the last slot served forward to `slot`, never back. Slots compare as text: both are
    /// `iso` UTC strings.
    pub fn raise_last_slot(&self, project: &str, name: &str, slot: &str) -> rusqlite::Result<()> {
        self.tx(|c| c.execute("UPDATE timer_slots SET last_slot=? WHERE project=? AND name=? AND last_slot < ?", params![slot, project, name, slot]).map(|_| ()))
    }

    pub fn set_last_slot(&self, project: &str, name: &str, slot: &str) -> rusqlite::Result<()> {
        self.tx(|c| c.execute("UPDATE timer_slots SET last_slot=? WHERE project=? AND name=?", params![slot, project, name]).map(|_| ()))
    }

    // -- runs -----------------------------------------------------------------

    pub fn start_run(&self, project: &str, name: &str, trigger: &str, caller: &str) -> Result<Row, StoreError> {
        let rid = new_id();
        let res = self.tx(|c| {
            c.execute(
                "INSERT INTO runs(id, project, name, trigger, caller, state, started_at) VALUES (?,?,?,?,?, 'running', ?)",
                params![rid, project, name, trigger, caller, now()],
            )
        });
        match res {
            Ok(_) => Ok(self.get_run(&rid)?.unwrap_or_default()),
            Err(rusqlite::Error::SqliteFailure(e, _)) if e.code == rusqlite::ErrorCode::ConstraintViolation => {
                let live = self.live_run(project, name)?;
                let id = live.as_ref().and_then(|l| l.get("id")).and_then(Value::as_str).map(|i| format!(" ({i})")).unwrap_or_default();
                Err(StoreError::LiveRunExists(format!("{project}/{name} already has a live run{id}")))
            }
            Err(e) => Err(e.into()),
        }
    }

    /// Record a timer fire that did not run, already ended.
    pub fn skipped_run(&self, project: &str, name: &str, caller: &str, why: &str) -> rusqlite::Result<Row> {
        let rid = new_id();
        let t = now();
        self.tx(|c| {
            c.execute(
                "INSERT INTO runs(id, project, name, trigger, caller, state, error, started_at, ended_at) VALUES (?,?,?, 'timer', ?, 'skipped', ?, ?, ?)",
                params![rid, project, name, caller, why, t, t],
            )
        })?;
        Ok(self.get_run(&rid)?.unwrap_or_default())
    }

    pub fn live_run(&self, project: &str, name: &str) -> rusqlite::Result<Option<Row>> {
        self.one("SELECT * FROM runs WHERE project=? AND name=? AND state='running'", &[&project, &name])
    }

    pub fn get_run(&self, rid: &str) -> rusqlite::Result<Option<Row>> {
        self.one("SELECT * FROM runs WHERE id=?", &[&rid])
    }

    pub fn list_runs(&self, project: Option<&str>, name: Option<&str>, limit: i64) -> rusqlite::Result<Vec<Row>> {
        let mut sql = String::from("SELECT * FROM runs");
        let mut args: Vec<&dyn rusqlite::ToSql> = vec![];
        let mut wh = vec![];
        let (project, name) = (project.filter(|p| !p.is_empty()), name.filter(|n| !n.is_empty()));
        if let Some(p) = &project {
            wh.push("project=?");
            args.push(p);
        }
        if let Some(n) = &name {
            wh.push("name=?");
            args.push(n);
        }
        if !wh.is_empty() {
            sql += &format!(" WHERE {}", wh.join(" AND "));
        }
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?";
        args.push(&limit);
        self.all(&sql, &args)
    }

    fn append(&self, rid: &str, col: &str, entry: Value) -> rusqlite::Result<()> {
        self.tx(|c| {
            let cur: Option<String> = c.query_row(&format!("SELECT {col} FROM runs WHERE id=?"), [rid], |r| r.get(0)).optional()?;
            let mut items: Vec<Value> = cur.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
            items.push(entry);
            c.execute(&format!("UPDATE runs SET {col}=? WHERE id=?"), params![Value::Array(items).to_string(), rid]).map(|_| ())
        })
    }

    pub fn record_step(&self, rid: &str, outcome: Value) -> rusqlite::Result<()> {
        self.append(rid, "steps", outcome)
    }

    pub fn record_dispatch(&self, rid: &str, entry: Value) -> rusqlite::Result<()> {
        self.append(rid, "dispatched", entry)
    }

    /// End a live run. A run already ended keeps its first verdict.
    pub fn finish_run(&self, rid: &str, state: &str, failed_step: &str, error: &str) -> rusqlite::Result<Row> {
        let chars: Vec<char> = error.chars().collect();
        let error: String = chars[chars.len().saturating_sub(4000)..].iter().collect();
        self.tx(|c| {
            c.execute(
                "UPDATE runs SET state=?, failed_step=?, error=?, ended_at=? WHERE id=? AND state='running'",
                params![state, failed_step, error, now(), rid],
            )
        })?;
        Ok(self.get_run(rid)?.unwrap_or_default())
    }

    /// Close runs a previous process left live. Returns the count.
    pub fn orphan_live(&self) -> rusqlite::Result<usize> {
        self.tx(|c| {
            c.execute(
                "UPDATE runs SET state='failed', error='the routine service restarted mid-run', ended_at=? WHERE state='running'",
                params![now()],
            )
        })
    }
}
