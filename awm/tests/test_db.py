"""Tests for awm.db — init, migrations, WAL mode, row factory."""

from __future__ import annotations

import sqlite3

import pytest

from awm.db import init_db, get_connection, SCHEMA_VERSION, _migrate


class TestInitDB:
    def test_fresh_init_creates_tables(self, awm_workspace):
        """Fresh DB should have all tables at current schema version."""
        conn = get_connection(awm_workspace["db_path"])
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "locks" in tables
        assert "shared_edits" in tables
        assert "session_logs" in tables
        assert "tasks" in tables
        assert "schema_version" in tables

    def test_schema_version_is_current(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        conn.close()
        assert version == SCHEMA_VERSION

    def test_idempotent_init(self, awm_workspace):
        """Calling init_db twice should not error."""
        init_db(awm_workspace["db_path"])
        conn = get_connection(awm_workspace["db_path"])
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        conn.close()
        assert version == SCHEMA_VERSION

    def test_wal_mode(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_row_factory(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        row = conn.execute("SELECT 1 AS val").fetchone()
        assert row["val"] == 1
        conn.close()

    def test_migration_v1_to_v3(self, tmp_path, monkeypatch):
        """Simulate a v1 database and migrate to v3."""
        db_path = tmp_path / "migrate.db"
        awm_dir = tmp_path
        monkeypatch.setattr("awm.db.AWM_DIR", awm_dir)

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        # Create only v1 tables (locks + shared_edits + schema_version)
        conn.executescript("""
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
        """)
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.commit()
        conn.close()

        # Now init should migrate
        init_db(db_path)

        conn = get_connection(db_path)
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        # session_logs and tasks should exist
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "session_logs" in tables
        assert "tasks" in tables
        conn.close()

    def test_missing_migration_raises(self, tmp_path, monkeypatch):
        """_migrate should raise if no migration path exists."""
        from awm.db import MIGRATIONS
        # Temporarily remove a known migration to trigger the error
        saved = MIGRATIONS.pop((1, 2))
        try:
            db_path = tmp_path / "bad.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            with pytest.raises(RuntimeError, match="No migration path"):
                _migrate(conn, 1)
            conn.close()
        finally:
            MIGRATIONS[(1, 2)] = saved
