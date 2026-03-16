"""SQLite setup + migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from awm.config import DB_PATH, AWM_DIR

SCHEMA_VERSION = 1

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

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Return a new SQLite connection with WAL mode enabled."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | None = None) -> None:
    """Create tables if they don't exist."""
    path = db_path or DB_PATH
    AWM_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    try:
        conn.executescript(SCHEMA_SQL)
        # Upsert schema version
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    finally:
        conn.close()
