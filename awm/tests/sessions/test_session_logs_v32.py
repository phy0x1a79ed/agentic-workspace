"""Tests for the v32 session_logs INTEGER→UUID migration.

Mirrors test_room_posts_v30.py / test_scopes_v31.py — migration round-trip,
CRR registration, app-layer dedup (since the UNIQUE constraint dropped),
and @peer disambiguation via parse_id_ref.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.sessions, pytest.mark.slow]

import sqlite3
from datetime import datetime, timezone

import pytest

from awm.db import get_connection, init_db, SCHEMA_VERSION
from awm.services.replication import schema as repl_schema


def _build_v31_db(db_path):
    """v31-shape DB with pre-v32 session_logs (INTEGER PK + UNIQUE)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (31);

        CREATE TABLE session_logs (
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
        INSERT INTO session_logs (project, scope, file_path, logged_at, summary, agent_id) VALUES
            ('p', 's', '', '2026-05-01T00:00:00Z', 'first',  'a'),
            ('p', 's', '', '2026-05-02T00:00:00Z', 'second', 'a'),
            ('q', 't', '', '2026-05-03T00:00:00Z', 'third',  'b');

        CREATE TABLE peers (
            peer_id TEXT NOT NULL PRIMARY KEY,
            ssh_alias TEXT NOT NULL DEFAULT '',
            remote_port INTEGER NOT NULL DEFAULT 7820,
            friendly_name TEXT, last_seen TEXT,
            added_at TEXT NOT NULL DEFAULT '',
            endpoints TEXT, tls_fingerprint TEXT,
            peer_priority INTEGER NOT NULL DEFAULT 100,
            origin_peer TEXT
        );
        INSERT INTO peers (peer_id, ssh_alias, added_at) VALUES
            ('alpha', 'self', '2026-05-01T00:00:00Z');

        CREATE TABLE peer_sync_state (
            peer_id TEXT PRIMARY KEY,
            last_db_version INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.commit()
    conn.close()


class TestV32Migration:
    def test_migration_preserves_rows(self, tmp_path, monkeypatch):
        assert SCHEMA_VERSION >= 32, "this test guards the v31→v32 migration"
        db_path = tmp_path / "v31.db"
        monkeypatch.setattr("awm.db.AWM_DIR", tmp_path)
        _build_v31_db(db_path)

        init_db(db_path)

        conn = get_connection(db_path)
        try:
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(session_logs)"
            ).fetchall()}
            assert {"uuid", "legacy_id", "origin_peer"}.issubset(cols)
            assert "id" not in cols

            rows = conn.execute(
                "SELECT legacy_id, origin_peer, summary "
                "FROM session_logs ORDER BY legacy_id"
            ).fetchall()
            assert [r["legacy_id"] for r in rows] == [1, 2, 3]
            assert [r["summary"] for r in rows] == ["first", "second", "third"]
            assert all(r["origin_peer"] == "alpha" for r in rows)
        finally:
            conn.close()


class TestSessionLogsCRR:
    def test_session_logs_in_crr_tables(self):
        assert "session_logs" in repl_schema.CRR_TABLES

    def test_session_logs_registered(self, awm_workspace):
        if not repl_schema.crsqlite_available():
            pytest.skip("cr-sqlite extension not vendored on this host")
        conn = get_connection()
        try:
            registered = repl_schema.register_all_crrs(conn)
            assert "session_logs" in registered
        finally:
            repl_schema.finalize(conn)
            conn.close()


class TestSessionLogsDisambiguation:
    def test_get_session_resolves_local_vs_remote(self, awm_workspace, monkeypatch):
        from awm.services import sessions
        from awm.services.replication.schema import new_uuid

        monkeypatch.setattr(sessions, "_local_peer_id", lambda: "alpha")

        conn = get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            for origin, summary in (("alpha", "local body"),
                                    ("beta",  "remote body")):
                conn.execute(
                    "INSERT INTO session_logs "
                    "(uuid, legacy_id, origin_peer, project, scope, file_path, "
                    " logged_at, summary, agent_id) "
                    "VALUES (?, 42, ?, 'p', 's', '', ?, ?, 'a')",
                    (new_uuid(), origin, now, summary),
                )
            conn.commit()
        finally:
            conn.close()

        local = sessions.get_session(42)
        assert local.entry.id == 42
        assert "local body" in local.content

        remote = sessions.get_session("42@beta")
        assert remote.entry.id == 42
        assert "remote body" in remote.content


class TestSessionLogsLegacySequence:
    def test_consecutive_logs_get_monotonic_legacy_ids(self, awm_workspace, monkeypatch):
        from awm.models import SessionLogCreateRequest
        from awm.services import sessions

        monkeypatch.setattr(sessions, "_local_peer_id", lambda: "alpha")

        e1 = sessions.log_session(SessionLogCreateRequest(
            project="p", scope="s", summary="one"))
        e2 = sessions.log_session(SessionLogCreateRequest(
            project="p", scope="s", summary="two"))
        assert e2.id == e1.id + 1

    def test_remote_row_does_not_advance_local_sequence(
        self, awm_workspace, monkeypatch
    ):
        from awm.models import SessionLogCreateRequest
        from awm.services import sessions
        from awm.services.replication.schema import new_uuid

        monkeypatch.setattr(sessions, "_local_peer_id", lambda: "alpha")
        e1 = sessions.log_session(SessionLogCreateRequest(
            project="p", scope="s", summary="local"))

        conn = get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO session_logs "
                "(uuid, legacy_id, origin_peer, project, scope, file_path, "
                " logged_at, summary, agent_id) "
                "VALUES (?, 9999, 'beta', 'p', 's', '', ?, 'remote', 'a')",
                (new_uuid(), now),
            )
            conn.commit()
        finally:
            conn.close()

        e2 = sessions.log_session(SessionLogCreateRequest(
            project="p", scope="s", summary="next"))
        assert e2.id == e1.id + 1
