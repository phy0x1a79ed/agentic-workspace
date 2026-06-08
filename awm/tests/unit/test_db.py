"""Tests for awm.db — init, migrations, WAL mode, row factory."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.unit, pytest.mark.smoke]

import sqlite3

import pytest

from awm.db import init_db, get_connection, SCHEMA_VERSION, _migrate


class TestInitDB:
    def test_fresh_init_creates_tables(self, awm_workspace):
        """Fresh DB should have all current tables at current schema version."""
        conn = get_connection(awm_workspace["db_path"])
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "projects" in tables
        assert "users" in tables
        assert "agents" in tables
        assert "agent_instances" in tables
        assert "rooms" in tables
        assert "guest_list" in tables
        assert "room_transcripts" in tables
        assert "session_logs" in tables
        assert "artifacts" in tables
        assert "messages" in tables
        assert "schema_version" in tables
        # Federation retired in v38 — peers/peer_sync_state must be gone.
        assert "peers" not in tables
        assert "peer_sync_state" not in tables
        # Locks retired in v39 — table must be gone.
        assert "locks" not in tables

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

    def test_migration_v1_step_to_v2(self, tmp_path, monkeypatch):
        """Drive just the v1→v2 migration step in isolation.

        The full v1→v37 chain runs through v27→v28 and v32→v33 rebuild dances
        that assume rooms/etc exist; a from-scratch v1 fixture can't supply
        them without becoming a full DB-replica fixture. This test instead
        validates the registered (1, 2) migration SQL drives forward cleanly.
        """
        from awm.db import MIGRATIONS

        db_path = tmp_path / "migrate.db"
        monkeypatch.setattr("awm.db.AWM_DIR", tmp_path)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (1);
        """)
        conn.commit()

        sql = MIGRATIONS[(1, 2)]
        conn.executescript(sql + "\nUPDATE schema_version SET version = 2;")
        version = conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        conn.close()
        assert version == 2

    def test_migration_v26_to_v27_adds_peer_priority(self, tmp_path, monkeypatch):
        """The (26, 27) migration step adds peer_priority and preserves rows.

        Drives the registered (26, 27) SQL directly rather than running the
        full v26→v37 chain — the chain's intermediate rebuild dances
        (v27→v28, v32→v33) require room/scope-shaped tables this fixture
        doesn't provide.
        """
        from awm.db import MIGRATIONS

        db_path = tmp_path / "v26.db"
        monkeypatch.setattr("awm.db.AWM_DIR", tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE peers (
                peer_id TEXT PRIMARY KEY,
                ssh_alias TEXT NOT NULL,
                remote_port INTEGER NOT NULL DEFAULT 7820,
                friendly_name TEXT,
                last_seen TEXT,
                added_at TEXT NOT NULL,
                endpoints TEXT,
                tls_fingerprint TEXT
            );
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (26);
            INSERT INTO peers (peer_id, ssh_alias, added_at) VALUES
                ('capella', 'capella-host', '2026-01-01T00:00:00Z'),
                ('xps', 'self', '2026-01-01T00:00:00Z');
        """)
        conn.commit()

        sql = MIGRATIONS[(26, 27)]
        conn.executescript(sql + "\nUPDATE schema_version SET version = 27;")
        rows = {r["peer_id"]: dict(r) for r in conn.execute(
            "SELECT peer_id, peer_priority FROM peers ORDER BY peer_id"
        ).fetchall()}
        v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        conn.close()
        assert v == 27
        assert rows["capella"]["peer_priority"] == 100
        assert rows["xps"]["peer_priority"] == 100

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
