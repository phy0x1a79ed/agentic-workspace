"""Tests for the v33 messages INTEGER→UUID migration."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.messaging, pytest.mark.slow]

import sqlite3
from datetime import datetime, timezone

import pytest

from awm.db import get_connection, init_db, SCHEMA_VERSION
from awm.services.replication import schema as repl_schema


def _build_v32_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (32);

        CREATE TABLE messages (
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
        INSERT INTO messages (scope, sender, msg_type, subject, body, status, created_at)
        VALUES
            ('workspace', 'a', 'note', 's1', 'b1', 'unread', '2026-05-01T00:00:00Z'),
            ('scope:p/q', 'b', 'note', 's2', 'b2', 'read',   '2026-05-02T00:00:00Z');

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


class TestV33Migration:
    def test_migration_preserves_rows(self, tmp_path, monkeypatch):
        assert SCHEMA_VERSION >= 33
        db_path = tmp_path / "v32.db"
        monkeypatch.setattr("awm.db.AWM_DIR", tmp_path)
        _build_v32_db(db_path)

        init_db(db_path)

        conn = get_connection(db_path)
        try:
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(messages)"
            ).fetchall()}
            assert {"uuid", "legacy_id", "origin_peer"}.issubset(cols)
            assert "id" not in cols

            rows = conn.execute(
                "SELECT legacy_id, origin_peer, subject "
                "FROM messages ORDER BY legacy_id"
            ).fetchall()
            assert [r["legacy_id"] for r in rows] == [1, 2]
            assert all(r["origin_peer"] == "alpha" for r in rows)
        finally:
            conn.close()


class TestMessagesCRR:
    def test_messages_in_crr_tables(self):
        assert "messages" in repl_schema.CRR_TABLES

    def test_messages_registered(self, awm_workspace):
        if not repl_schema.crsqlite_available():
            pytest.skip("cr-sqlite extension not vendored on this host")
        conn = get_connection()
        try:
            registered = repl_schema.register_all_crrs(conn)
            assert "messages" in registered
        finally:
            repl_schema.finalize(conn)
            conn.close()


class TestMessagesDisambiguation:
    def test_fetch_at_peer_returns_only_remote_rows(self, awm_workspace, monkeypatch):
        """inbox_fetch('scope:p/q@beta') returns rows where origin_peer='beta',
        not local rows. This is the v33 unlock: previously the @peer suffix
        on a read was rejected outright."""
        from awm.services import messaging
        from awm.services.replication.schema import new_uuid

        monkeypatch.setattr(messaging, "_local_peer_id", lambda: "alpha")

        conn = get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            for legacy, origin, subj in (
                (1, "alpha", "local note"),
                (1, "beta",  "remote note"),
            ):
                conn.execute(
                    "INSERT INTO messages "
                    "(uuid, legacy_id, origin_peer, scope, sender, msg_type, "
                    " subject, body, status, created_at) "
                    "VALUES (?, ?, ?, 'scope:p/q', 's', 't', ?, 'b', 'unread', ?)",
                    (new_uuid(), legacy, origin, subj, now),
                )
            conn.commit()
        finally:
            conn.close()

        local = messaging.fetch_messages(scope="scope:p/q")
        local_subjects = {m.subject for m in local.messages}
        assert local_subjects == {"local note", "remote note"}  # no peer filter → both

        remote = messaging.fetch_messages(scope="scope:p/q@beta")
        assert [m.subject for m in remote.messages] == ["remote note"]

    def test_mark_read_targets_specific_origin(self, awm_workspace, monkeypatch):
        from awm.services import messaging
        from awm.services.replication.schema import new_uuid

        monkeypatch.setattr(messaging, "_local_peer_id", lambda: "alpha")

        conn = get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            for legacy, origin in ((7, "alpha"), (7, "beta")):
                conn.execute(
                    "INSERT INTO messages "
                    "(uuid, legacy_id, origin_peer, scope, sender, msg_type, "
                    " subject, body, status, created_at) "
                    "VALUES (?, ?, ?, 'workspace', 's', 't', 'subj', 'b', 'unread', ?)",
                    (new_uuid(), legacy, origin, now),
                )
            conn.commit()
        finally:
            conn.close()

        # Bare 7 → local row only.
        messaging.mark_read(7)
        conn = get_connection()
        try:
            local = conn.execute(
                "SELECT status FROM messages WHERE legacy_id=7 AND origin_peer='alpha'"
            ).fetchone()
            remote = conn.execute(
                "SELECT status FROM messages WHERE legacy_id=7 AND origin_peer='beta'"
            ).fetchone()
        finally:
            conn.close()
        assert local["status"] == "read"
        assert remote["status"] == "unread"

        # "7@beta" → flips the remote-origin row.
        messaging.mark_read("7@beta")
        conn = get_connection()
        try:
            remote = conn.execute(
                "SELECT status FROM messages WHERE legacy_id=7 AND origin_peer='beta'"
            ).fetchone()
        finally:
            conn.close()
        assert remote["status"] == "read"


class TestMessagesLegacySequence:
    def test_consecutive_sends_get_monotonic_legacy_ids(self, awm_workspace, monkeypatch):
        from awm.models import MessageSendRequest
        from awm.services import messaging

        monkeypatch.setattr(messaging, "_local_peer_id", lambda: "alpha")

        r1 = messaging.send_message(MessageSendRequest(
            scope="workspace", sender="t", msg_type="notification",
            subject="one", body="b"))
        r2 = messaging.send_message(MessageSendRequest(
            scope="workspace", sender="t", msg_type="notification",
            subject="two", body="b"))
        assert r2.msg.id == r1.msg.id + 1
