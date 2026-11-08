//! `scopes.db` at schema v5, unchanged from the Python, and the identity rows: projects,
//! users and agents (a scope's row).
//!
//! The `embeddings` tables are created on a fresh database, as the Python did, and never
//! read: scopes keeps no search index.

use awm_store::rusqlite::{params, Connection, OptionalExtension, Row};
use awm_store::{Migration, Store};

use crate::Fail;

pub const SCHEMA_VERSION: i64 = 5;

const SCHEMA_SQL: &str = "\
-- Identity (kept relational — all in the scopes DB) -------------------
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT,
    repo_path   TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id        TEXT PRIMARY KEY,
    username  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS agents (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects(id),
    scope         TEXT NOT NULL,
    status        TEXT NOT NULL,
    agent_cli     TEXT NOT NULL,
    branch        TEXT NOT NULL,
    worktree      TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    is_vagrant    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    retired_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agents_project_status ON agents(project_id, status);

-- Tags per project (scope '') or per scope or scope group path ----------
-- Keyed by name, not agents row: a project, a main checkout or a group such
-- as 'metasmith/lab' can carry tags with no agents row behind it.
--   tags : JSON list of normalised tags, never empty (an empty set deletes the row).
CREATE TABLE IF NOT EXISTS tags (
    project     TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (project, scope)
);
";

const CLAIMS_SQL: &str = "\
CREATE TABLE IF NOT EXISTS scope_claims (
    project     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    session     TEXT NOT NULL,
    pid         INTEGER,
    proc_start  TEXT,
    job         TEXT,
    claimed_at  INTEGER NOT NULL,
    PRIMARY KEY (project, scope)
);
CREATE TABLE IF NOT EXISTS scope_waiters (
    project     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    session     TEXT NOT NULL,
    pid         INTEGER,
    proc_start  TEXT,
    job         TEXT,
    asked_at    INTEGER NOT NULL,
    notified_at INTEGER,
    PRIMARY KEY (project, scope, session)
);
CREATE TABLE IF NOT EXISTS scope_use (
    project     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    last_used   INTEGER NOT NULL,
    PRIMARY KEY (project, scope)
);
CREATE TABLE IF NOT EXISTS scope_counters (
    name   TEXT PRIMARY KEY,
    value  INTEGER NOT NULL
);
";

/// The Python's `EMBEDDINGS_DDL`, for a fresh database only.
const EMBEDDINGS_SQL: &str = "\
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk INTEGER NOT NULL DEFAULT 0,
    header TEXT NOT NULL DEFAULT '',
    chunk_text TEXT NOT NULL,
    embedding BLOB,
    model TEXT,
    content_hash TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(source_type, source_id, chunk)
);
CREATE VIRTUAL TABLE IF NOT EXISTS embeddings_fts USING fts5(text, tokenize='porter unicode61');
";

const MIGRATIONS: [Migration; 3] = [
    Migration { from: 2, sql: "DROP INDEX IF EXISTS idx_agents_parent;\nALTER TABLE agents DROP COLUMN parent_id;\n" },
    Migration {
        from: 3,
        sql: "CREATE TABLE IF NOT EXISTS tags (\n    project TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',\n    tags TEXT NOT NULL, updated_at INTEGER NOT NULL,\n    PRIMARY KEY (project, scope));\n",
    },
    Migration { from: 4, sql: CLAIMS_SQL },
];

/// Open `scopes.db` and bring it to v5.
pub fn open(path: &std::path::Path) -> Result<Store, Fail> {
    let store = Store::open(path)?;
    let schema = format!("{SCHEMA_SQL}{CLAIMS_SQL}{EMBEDDINGS_SQL}");
    store.init(&schema, SCHEMA_VERSION, &MIGRATIONS)?;
    Ok(store)
}

pub fn now_ms() -> i64 {
    awm_pycompat::time::now_ms()
}

/// One `agents` row joined to its project's name.
#[derive(Debug, Clone, PartialEq)]
pub struct Agent {
    pub id: String,
    pub project: String,
    pub scope: String,
    pub status: String,
    pub branch: String,
    pub worktree: String,
    pub created_at: i64,
}

fn agent_row(r: &Row<'_>) -> awm_store::rusqlite::Result<Agent> {
    Ok(Agent {
        id: r.get("id")?,
        project: r.get("project_name")?,
        scope: r.get("scope")?,
        status: r.get("status")?,
        branch: r.get("branch")?,
        worktree: r.get("worktree")?,
        created_at: r.get("created_at")?,
    })
}

pub fn project_id(c: &Connection, name: &str) -> Result<Option<String>, Fail> {
    Ok(c.query_row("SELECT id FROM projects WHERE name=?", [name], |r| r.get(0)).optional()?)
}

/// Get or create a projects row. `url` is set on insert only.
pub fn ensure_project(c: &Connection, name: &str, repo_path: &str, url: Option<&str>) -> Result<String, Fail> {
    if let Some(id) = project_id(c, name)? {
        return Ok(id);
    }
    let id = uuid::Uuid::new_v4().to_string();
    c.execute("INSERT INTO projects (id, name, url, repo_path, created_at) VALUES (?, ?, ?, ?, ?)", params![id, name, url, repo_path, now_ms()])?;
    Ok(id)
}

/// The newest agent id for a scope: only a live one (allocated, active) unless `active_only`
/// is false, which falls through to the newest retired one.
pub fn agent_id(c: &Connection, project: &str, scope: &str, active_only: bool) -> Result<Option<String>, Fail> {
    let statuses = if active_only { "'allocated', 'active'" } else { "'allocated', 'active', 'retired'" };
    let sql = format!(
        "SELECT a.id FROM agents a JOIN projects p ON p.id = a.project_id \
         WHERE p.name=? AND a.scope=? AND a.status IN ({statuses}) ORDER BY a.created_at DESC LIMIT 1"
    );
    Ok(c.query_row(&sql, [project, scope], |r| r.get(0)).optional()?)
}

pub fn agent_by_id(c: &Connection, id: &str) -> Result<Option<Agent>, Fail> {
    Ok(c
        .query_row("SELECT a.*, p.name AS project_name FROM agents a JOIN projects p ON p.id = a.project_id WHERE a.id = ?", [id], agent_row)
        .optional()?)
}

pub fn agent_record(c: &Connection, project: &str, scope: &str, active_only: bool) -> Result<Option<Agent>, Fail> {
    match agent_id(c, project, scope, active_only)? {
        Some(id) => agent_by_id(c, &id),
        None => Ok(None),
    }
}

/// The optional fields of a new agents row.
#[derive(Debug, Clone)]
pub struct NewAgent<'a> {
    pub branch: Option<&'a str>,
    pub worktree: &'a str,
    pub agent_cli: &'a str,
    pub status: &'a str,
    pub is_vagrant: bool,
    pub display_name: Option<&'a str>,
}

impl Default for NewAgent<'_> {
    fn default() -> Self {
        Self { branch: None, worktree: "", agent_cli: "claude", status: "allocated", is_vagrant: false, display_name: None }
    }
}

/// Get or create the agents row of a scope under an existing project.
pub fn ensure_agent(c: &Connection, project: &str, scope: &str, a: &NewAgent<'_>) -> Result<String, Fail> {
    if let Some(id) = agent_id(c, project, scope, true)? {
        return Ok(id);
    }
    let Some(pid) = project_id(c, project)? else {
        return Err(Fail::key(&format!("project {} not found — call ensure_project first", awm_pycompat::repr::str_repr(project))));
    };
    let id = uuid::Uuid::new_v4().to_string();
    let branch = a.branch.filter(|b| !b.is_empty()).map(str::to_string).unwrap_or_else(|| format!("feat/{scope}"));
    let display = a.display_name.filter(|d| !d.is_empty()).unwrap_or(scope);
    c.execute(
        "INSERT INTO agents (id, project_id, scope, status, agent_cli, branch,  worktree, display_name, is_vagrant, created_at, retired_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        params![id, pid, scope, a.status, a.agent_cli, branch, a.worktree, display, a.is_vagrant as i64, now_ms()],
    )?;
    Ok(id)
}

pub fn retire_agent(c: &Connection, id: &str) -> Result<(), Fail> {
    c.execute("UPDATE agents SET status='retired', retired_at = COALESCE(retired_at, ?) WHERE id=?", params![now_ms(), id])?;
    Ok(())
}

pub fn user_id(c: &Connection, username: &str) -> Result<Option<String>, Fail> {
    Ok(c.query_row("SELECT id FROM users WHERE username=?", [username], |r| r.get(0)).optional()?)
}

pub fn username(c: &Connection, id: &str) -> Result<Option<String>, Fail> {
    Ok(c.query_row("SELECT username FROM users WHERE id=?", [id], |r| r.get(0)).optional()?)
}

impl crate::App {
    /// Run `f` on a pooled connection.
    pub fn read<T>(&self, f: impl FnOnce(&Connection) -> Result<T, Fail>) -> Result<T, Fail> {
        self.store.blocking_read(f)
    }

    /// Run `f` in one `BEGIN IMMEDIATE` transaction.
    pub fn write<T>(&self, f: impl FnOnce(&Connection) -> Result<T, Fail>) -> Result<T, Fail> {
        self.store.blocking_write(|tx| f(tx))
    }

    pub fn agent_record(&self, project: &str, scope: &str, active_only: bool) -> Result<Option<Agent>, Fail> {
        self.read(|c| agent_record(c, project, scope, active_only))
    }
}
