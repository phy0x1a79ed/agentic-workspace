"""SQLite setup + migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from awm.config import DB_PATH, AWM_DIR

SCHEMA_VERSION = 5

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_path TEXT NOT NULL,
    holder_id TEXT NOT NULL,
    holder_pid INTEGER,
    lock_type TEXT NOT NULL DEFAULT 'exclusive',
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    metadata TEXT,
    UNIQUE(resource_path, holder_id)
);

CREATE TABLE IF NOT EXISTS shared_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    worktree_path TEXT NOT NULL,
    branch TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS session_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    task TEXT NOT NULL,
    file_path TEXT NOT NULL,
    git_commit TEXT,
    logged_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    agent_id TEXT DEFAULT 'unknown',
    metadata TEXT,
    UNIQUE(project, task, logged_at, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_session_logs_project_task
    ON session_logs(project, task);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    branch TEXT NOT NULL,
    worktree TEXT NOT NULL,
    repo_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_active_unique
    ON tasks(project, task) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""

# Migrations keyed by (from_version, to_version)
MIGRATIONS = {
    (1, 2): """\
CREATE TABLE IF NOT EXISTS session_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    task TEXT NOT NULL,
    file_path TEXT NOT NULL,
    git_commit TEXT,
    logged_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    agent_id TEXT DEFAULT 'unknown',
    metadata TEXT,
    UNIQUE(project, task, logged_at, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_session_logs_project_task
    ON session_logs(project, task);
""",
    (2, 3): """\
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    branch TEXT NOT NULL,
    worktree TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project, task)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
""",
    (3, 4): """\
CREATE TABLE IF NOT EXISTS tasks_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    branch TEXT NOT NULL,
    worktree TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO tasks_new SELECT * FROM tasks;
DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;
CREATE UNIQUE INDEX idx_tasks_active_unique
    ON tasks(project, task) WHERE status = 'active';
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_project ON tasks(project);
""",
    (4, 5): """\
ALTER TABLE tasks ADD COLUMN repo_path TEXT;
""",
}


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Return a new SQLite connection with WAL mode enabled."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn: sqlite3.Connection, current: int) -> None:
    """Apply migrations from current version up to SCHEMA_VERSION."""
    while current < SCHEMA_VERSION:
        next_ver = current + 1
        sql = MIGRATIONS.get((current, next_ver))
        if sql is None:
            raise RuntimeError(f"No migration path from v{current} to v{next_ver}")
        try:
            # Bundle the version bump with the migration so they commit together
            conn.executescript(
                sql + f"\nUPDATE schema_version SET version = {next_ver};"
            )
        except sqlite3.OperationalError as exc:
            # Handle partial prior migration (e.g. column already added but
            # version not bumped due to crash). Bump version and continue.
            if "duplicate column name" in str(exc):
                conn.execute(
                    "UPDATE schema_version SET version = ?", (next_ver,)
                )
                conn.commit()
            else:
                raise
        current = next_ver


def init_db(db_path: Path | None = None) -> None:
    """Create tables if they don't exist, running migrations as needed."""
    path = db_path or DB_PATH
    AWM_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    try:
        row = None
        try:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
        except sqlite3.OperationalError:
            pass  # table doesn't exist yet

        if row is None:
            # Fresh database — create everything
            conn.executescript(SCHEMA_SQL)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            conn.commit()
        else:
            current = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            if current < SCHEMA_VERSION:
                _migrate(conn, current)
    finally:
        conn.close()
