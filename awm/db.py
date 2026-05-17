"""SQLite setup + migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from awm.config import DB_PATH, AWM_DIR

SCHEMA_VERSION = 17

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
    scope TEXT NOT NULL,
    file_path TEXT NOT NULL,
    git_commit TEXT,
    logged_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    agent_id TEXT DEFAULT 'unknown',
    metadata TEXT,
    content TEXT,
    skill_path TEXT,
    outcome TEXT,
    deviations TEXT,
    suggestions TEXT,
    skill_version TEXT,
    resolved_at TEXT,
    resolution TEXT,
    title TEXT,
    UNIQUE(project, scope, logged_at, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_session_logs_project_scope
    ON session_logs(project, scope);

CREATE TABLE IF NOT EXISTS scopes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    branch TEXT NOT NULL,
    worktree TEXT NOT NULL,
    repo_path TEXT,
    session INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_scopes_active_unique
    ON scopes(project, scope) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_scopes_status ON scopes(status);
CREATE INDEX IF NOT EXISTS idx_scopes_project ON scopes(project);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    description TEXT,
    format TEXT,
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'current',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path);

CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_type, source_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    sender TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata TEXT,
    status TEXT NOT NULL DEFAULT 'unread',
    created_at TEXT NOT NULL,
    read_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages(scope);
CREATE INDEX IF NOT EXISTS idx_messages_scope_status ON messages(scope, status);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    scope TEXT NOT NULL,
    pid INTEGER NOT NULL,
    status TEXT NOT NULL,
    agent_cli TEXT NOT NULL,
    started_at TEXT NOT NULL,
    exited_at TEXT,
    exit_code INTEGER,
    log_path TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_scope ON agent_sessions(project, scope);

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
    (6, 7): """\
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    sender TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata TEXT,
    status TEXT NOT NULL DEFAULT 'unread',
    created_at TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages(scope);
CREATE INDEX IF NOT EXISTS idx_messages_scope_status ON messages(scope, status);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO config (key, value, updated_at)
    VALUES ('agent_cli', 'opencode', datetime('now'));
""",
    (7, 8): """\
ALTER TABLE tasks ADD COLUMN session INTEGER NOT NULL DEFAULT 1;
""",
    (8, 9): """\
ALTER TABLE session_logs ADD COLUMN content TEXT;
""",
    (9, 10): """\
-- Rename tasks → scopes
ALTER TABLE tasks RENAME TO scopes;
ALTER TABLE scopes RENAME COLUMN task TO scope;

-- Experiences table
CREATE TABLE IF NOT EXISTS experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_path TEXT,
    skill_version TEXT,
    project TEXT NOT NULL,
    scope TEXT NOT NULL,
    agent_id TEXT DEFAULT 'unknown',
    outcome TEXT,
    summary TEXT NOT NULL,
    deviations TEXT,
    suggestions TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiences_skill ON experiences(skill_path);
CREATE INDEX IF NOT EXISTS idx_experiences_project ON experiences(project);

-- Artifacts table
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    description TEXT,
    format TEXT,
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'current',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path);
""",
    (10, 11): """\
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_type, source_id)
);
""",
    (5, 6): """\
-- Migrate task worktree paths: main/{project}/{task} → main/{project}/tasks/{task}
UPDATE tasks SET worktree = REPLACE(worktree,
    '/' || project || '/' || task,
    '/' || project || '/tasks/' || task)
WHERE worktree LIKE '%/main/%' AND worktree NOT LIKE '%/tasks/%';

-- Migrate session file paths
UPDATE session_logs SET file_path = REPLACE(file_path,
    'main/' || project || '/' || task || '/',
    'main/' || project || '/tasks/' || task || '/')
WHERE file_path LIKE 'main/%' AND file_path NOT LIKE '%/tasks/%';
""",
    (11, 12): """\
-- Rename session_logs.task → scope
ALTER TABLE session_logs RENAME COLUMN task TO scope;
""",
    (12, 13): """\
ALTER TABLE session_logs ADD COLUMN skill_path TEXT;
""",
    (13, 14): """\
-- Fold experiences into session_logs
ALTER TABLE session_logs ADD COLUMN outcome TEXT;
ALTER TABLE session_logs ADD COLUMN deviations TEXT;
ALTER TABLE session_logs ADD COLUMN suggestions TEXT;
ALTER TABLE session_logs ADD COLUMN skill_version TEXT;

INSERT INTO session_logs
    (project, scope, file_path, git_commit, logged_at, summary, agent_id,
     metadata, content, skill_path, outcome, deviations, suggestions, skill_version)
SELECT project, scope, '', NULL, created_at, summary, agent_id,
       metadata, NULL, skill_path, outcome, deviations, suggestions, skill_version
FROM experiences;

DELETE FROM embeddings WHERE source_type = 'experience';

DROP TABLE IF EXISTS experiences;
""",
    (14, 15): """\
ALTER TABLE session_logs ADD COLUMN resolved_at TEXT;
ALTER TABLE session_logs ADD COLUMN resolution TEXT;
""",
    (15, 16): """\
ALTER TABLE session_logs ADD COLUMN title TEXT;
""",
    (16, 17): """\
CREATE TABLE IF NOT EXISTS agent_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    scope TEXT NOT NULL,
    pid INTEGER NOT NULL,
    status TEXT NOT NULL,
    agent_cli TEXT NOT NULL,
    started_at TEXT NOT NULL,
    exited_at TEXT,
    exit_code INTEGER,
    log_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_scope ON agent_sessions(project, scope);
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
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value, updated_at) VALUES ('agent_cli', 'opencode', datetime('now'))"
            )
            conn.commit()
        else:
            current = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            if current < SCHEMA_VERSION:
                _migrate(conn, current)
    finally:
        conn.close()
