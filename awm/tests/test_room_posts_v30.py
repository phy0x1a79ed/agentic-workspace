"""Tests for the v30 room_posts INTEGER→UUID migration.

Covers:
- parse_id_ref helper: legacy_id + optional peer parsing.
- Migration round-trip: a v29 DB with fixture rows comes through v30 with
  uuids + per-row legacy_id preserved + origin_peer stamped.
- crsql_as_crr accepts the new table (extension must be loaded for this).
- Post-replication disambiguation: two rows with the same legacy_id but
  different origin_peer resolve via the @peer suffix.
- Per-peer legacy_id sequence: consecutive posts get monotonic ids and
  do not gap when a row from another peer arrives via replication.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from awm.db import get_connection, init_db, SCHEMA_VERSION
from awm.services.network import peers as peer_svc
from awm.services.replication import schema as repl_schema


# ---------------------------------------------------------------------------
# parse_id_ref
# ---------------------------------------------------------------------------

class TestParseIdRef:
    def test_bare_int(self):
        assert peer_svc.parse_id_ref(42) == (42, None)

    def test_numeric_string(self):
        assert peer_svc.parse_id_ref("42") == (42, None)

    def test_peer_scoped(self):
        assert peer_svc.parse_id_ref("42@capella") == (42, "capella")

    def test_peer_scoped_with_dash(self):
        assert peer_svc.parse_id_ref("7@xps-dev") == (7, "xps-dev")

    def test_zero(self):
        assert peer_svc.parse_id_ref(0) == (0, None)

    def test_rejects_negative_int(self):
        with pytest.raises(ValueError, match="non-negative"):
            peer_svc.parse_id_ref(-1)

    def test_rejects_garbage_string(self):
        with pytest.raises(ValueError, match="invalid id ref"):
            peer_svc.parse_id_ref("not-a-number")

    def test_rejects_empty_peer(self):
        with pytest.raises(ValueError, match="invalid id ref"):
            peer_svc.parse_id_ref("42@")

    def test_rejects_bad_peer_shape(self):
        with pytest.raises(ValueError, match="invalid id ref"):
            peer_svc.parse_id_ref("42@-leading-dash")

    def test_rejects_unexpected_type(self):
        with pytest.raises(ValueError, match="must be int or str"):
            peer_svc.parse_id_ref(3.14)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Migration round-trip
# ---------------------------------------------------------------------------

def _build_v29_db(db_path):
    """Construct a partial v29-shape DB with rooms (already TEXT PK from v28)
    and the pre-v30 INTEGER-PK room_posts. We only seed the columns the v30
    migration actually reads, so the rest of the schema stays minimal."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (29);

        CREATE TABLE rooms (
            id TEXT NOT NULL PRIMARY KEY,
            host_peer_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            closed_at TEXT,
            topic TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            close_on_exit INTEGER NOT NULL DEFAULT 0,
            origin_peer TEXT
        );
        INSERT INTO rooms (id, host_peer_id, created_at, status) VALUES
            ('room-alpha', 'capella', '2026-05-01T00:00:00Z', 'active'),
            ('room-beta',  'xps',     '2026-05-02T00:00:00Z', 'active');

        CREATE TABLE room_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id TEXT NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'text',
            ts TEXT NOT NULL,
            FOREIGN KEY (room_id) REFERENCES rooms(id)
        );
        INSERT INTO room_posts (id, room_id, author, body, kind, ts) VALUES
            (1, 'room-alpha', 'system',        'opened',        'system', '2026-05-01T00:00:01Z'),
            (2, 'room-alpha', 'user:operator', 'hello there',   'text',   '2026-05-01T00:01:00Z'),
            (3, 'room-beta',  'system',        'opened',        'system', '2026-05-02T00:00:01Z'),
            (4, 'room-beta',  'user:operator', 'over here',     'text',   '2026-05-02T00:01:00Z');

        -- The other tables init_db expects to operate on; minimal shape.
        CREATE TABLE peer_sync_state (
            peer_id TEXT PRIMARY KEY,
            last_db_version INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE peers (
            peer_id TEXT NOT NULL PRIMARY KEY,
            ssh_alias TEXT NOT NULL DEFAULT '',
            remote_port INTEGER NOT NULL DEFAULT 7820,
            friendly_name TEXT,
            last_seen TEXT,
            added_at TEXT NOT NULL DEFAULT '',
            endpoints TEXT,
            tls_fingerprint TEXT,
            peer_priority INTEGER NOT NULL DEFAULT 100,
            origin_peer TEXT
        );
    """)
    conn.commit()
    conn.close()


class TestV30Migration:
    def test_migration_preserves_rows(self, tmp_path, monkeypatch):
        """v29 → v30: all four posts survive, uuids minted, legacy_id matches
        old id, origin_peer takes the room's host_peer_id."""
        assert SCHEMA_VERSION >= 30, "this test guards the v29→v30 migration"
        db_path = tmp_path / "v29.db"
        monkeypatch.setattr("awm.db.AWM_DIR", tmp_path)
        _build_v29_db(db_path)

        init_db(db_path)

        conn = get_connection(db_path)
        try:
            version = conn.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            assert version == SCHEMA_VERSION

            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(room_posts)"
            ).fetchall()}
            assert {"uuid", "legacy_id", "origin_peer"}.issubset(cols)
            assert "id" not in cols

            rows = conn.execute(
                "SELECT uuid, legacy_id, origin_peer, room_id, body "
                "FROM room_posts ORDER BY legacy_id"
            ).fetchall()
            assert len(rows) == 4
            assert [r["legacy_id"] for r in rows] == [1, 2, 3, 4]
            assert [r["body"] for r in rows] == [
                "opened", "hello there", "opened", "over here",
            ]
            # origin_peer follows the room's host
            assert rows[0]["origin_peer"] == "capella"
            assert rows[1]["origin_peer"] == "capella"
            assert rows[2]["origin_peer"] == "xps"
            assert rows[3]["origin_peer"] == "xps"
            # uuids minted, unique, hex
            uuids = [r["uuid"] for r in rows]
            assert len(set(uuids)) == 4
            for u in uuids:
                assert len(u) == 32
                int(u, 16)  # valid hex
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# CRR registration sanity
# ---------------------------------------------------------------------------

class TestRoomPostsCRR:
    def test_room_posts_in_crr_tables(self):
        assert "room_posts" in repl_schema.CRR_TABLES

    def test_room_posts_registered(self, awm_workspace):
        if not repl_schema.crsqlite_available():
            pytest.skip("cr-sqlite extension not vendored on this host")
        conn = get_connection()
        try:
            registered = repl_schema.register_all_crrs(conn)
            assert "room_posts" in registered
        finally:
            repl_schema.finalize(conn)
            conn.close()

    def test_room_posts_insert_produces_changes(self, awm_workspace):
        """Inserting into the new room_posts shape populates crsql_changes."""
        if not repl_schema.crsqlite_available():
            pytest.skip("cr-sqlite extension not vendored on this host")
        from awm.services.replication.schema import new_uuid

        conn = get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            # Need a room row first because room_posts references it (no FK
            # post-v30, but the API insists on room_id being non-empty).
            conn.execute(
                "INSERT INTO rooms (id, host_peer_id, created_at, status) "
                "VALUES ('crr-room', 'test-host', ?, 'active')",
                (now,),
            )
            conn.execute(
                "INSERT INTO room_posts "
                "(uuid, legacy_id, origin_peer, room_id, author, body, kind, ts) "
                "VALUES (?, 1, 'test-host', 'crr-room', 'system', 'hi', 'text', ?)",
                (new_uuid(), now),
            )
            conn.commit()
            changes = conn.execute(
                'SELECT COUNT(*) FROM crsql_changes WHERE "table" = ?',
                ("room_posts",),
            ).fetchone()[0]
            assert changes > 0
        finally:
            repl_schema.finalize(conn)
            conn.close()


# ---------------------------------------------------------------------------
# Disambiguation: same legacy_id, different origin_peer
# ---------------------------------------------------------------------------

class TestRoomPostsDisambiguation:
    def test_get_post_resolves_local_vs_remote(self, awm_workspace, monkeypatch):
        """After replication, peer A's post id=42 and peer B's post id=42
        both live in the local DB (different uuid + origin_peer). A bare
        ``42`` resolves to the local peer's row; ``42@other`` resolves to
        the remote row."""
        from awm.services import rooms as rooms_svc
        from awm.services.replication.schema import new_uuid

        # Force a known local peer id.
        monkeypatch.setattr(rooms_svc, "_local_peer_id", lambda: "alpha")

        conn = get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO rooms (id, host_peer_id, created_at, status) "
                "VALUES ('disamb-room', 'alpha', ?, 'active')",
                (now,),
            )
            # Two rows with the same legacy_id but different origin_peer.
            conn.execute(
                "INSERT INTO room_posts "
                "(uuid, legacy_id, origin_peer, room_id, author, body, kind, ts) "
                "VALUES (?, 42, 'alpha', 'disamb-room', 'a', 'local body', 'text', ?)",
                (new_uuid(), now),
            )
            conn.execute(
                "INSERT INTO room_posts "
                "(uuid, legacy_id, origin_peer, room_id, author, body, kind, ts) "
                "VALUES (?, 42, 'beta',  'disamb-room', 'b', 'remote body', 'text', ?)",
                (new_uuid(), now),
            )
            conn.commit()
        finally:
            conn.close()

        local = rooms_svc.get_post(42)
        assert local is not None
        assert local.body == "local body"
        assert local.id == 42

        remote = rooms_svc.get_post("42@beta")
        assert remote is not None
        assert remote.body == "remote body"
        assert remote.id == 42


# ---------------------------------------------------------------------------
# Per-peer monotonic legacy_id sequence
# ---------------------------------------------------------------------------

class TestRoomPostsLegacySequence:
    def test_consecutive_posts_get_monotonic_legacy_ids(self, awm_workspace, monkeypatch):
        """Three posts to the same room get legacy_id 1, 2, 3."""
        from awm.services import rooms as rooms_svc

        monkeypatch.setattr(rooms_svc, "_local_peer_id", lambda: "alpha")
        room = rooms_svc.create_room(opener="user:operator", scopes=())
        # create_room itself emits one system post, so the next user post
        # should be legacy_id=2.
        p1 = rooms_svc.post(room.id, author="user:operator",
                            body="first", kind="text")
        p2 = rooms_svc.post(room.id, author="user:operator",
                            body="second", kind="text")
        assert p2.id == p1.id + 1

    def test_two_db_convergence(self, tmp_path):
        """End-to-end CRR round-trip on the new room_posts shape: insert on
        DB A, ship the change log to DB B, assert the row appears on B with
        every replicated column intact."""
        if not repl_schema.crsqlite_available():
            pytest.skip("cr-sqlite extension not vendored on this host")
        from awm.services.replication.endpoint import _b64, apply_changes
        from awm.services.replication.schema import new_uuid

        db_a = tmp_path / "a.db"
        db_b = tmp_path / "b.db"
        init_db(db_a)
        init_db(db_b)

        now = datetime.now(timezone.utc).isoformat()
        uid = new_uuid()

        conn_a = get_connection(db_a)
        try:
            conn_a.execute(
                "INSERT INTO rooms (id, host_peer_id, created_at, status) "
                "VALUES ('conv-room', 'peer-a', ?, 'active')",
                (now,),
            )
            conn_a.execute(
                "INSERT INTO room_posts "
                "(uuid, legacy_id, origin_peer, room_id, author, body, kind, ts) "
                "VALUES (?, 1, 'peer-a', 'conv-room', 'system', 'across', 'text', ?)",
                (uid, now),
            )
            conn_a.commit()
            rows = conn_a.execute(
                'SELECT "table", pk, cid, val, col_version, db_version, '
                'site_id, cl, seq FROM crsql_changes WHERE db_version > 0 '
                'ORDER BY db_version, seq'
            ).fetchall()
        finally:
            repl_schema.finalize(conn_a)
            conn_a.close()

        changes = [
            {
                "table": r["table"],
                "pk": _b64(r["pk"]),
                "cid": r["cid"],
                "val": _b64(r["val"]),
                "col_version": r["col_version"],
                "db_version": r["db_version"],
                "site_id": _b64(r["site_id"]),
                "cl": r["cl"],
                "seq": r["seq"],
            }
            for r in rows
        ]

        conn_b = get_connection(db_b)
        try:
            applied = apply_changes(conn_b, changes)
            assert applied > 0
            row = conn_b.execute(
                "SELECT uuid, legacy_id, origin_peer, room_id, body "
                "FROM room_posts WHERE uuid = ?",
                (uid,),
            ).fetchone()
            assert row is not None
            assert row["legacy_id"] == 1
            assert row["origin_peer"] == "peer-a"
            assert row["room_id"] == "conv-room"
            assert row["body"] == "across"
        finally:
            repl_schema.finalize(conn_b)
            conn_b.close()

    def test_remote_row_does_not_advance_local_sequence(
        self, awm_workspace, monkeypatch
    ):
        """A replicated row with origin_peer != self must not bump our
        own legacy_id sequence — otherwise post() would skip ids."""
        from awm.services import rooms as rooms_svc
        from awm.services.replication.schema import new_uuid

        monkeypatch.setattr(rooms_svc, "_local_peer_id", lambda: "alpha")
        room = rooms_svc.create_room(opener="user:operator", scopes=())
        p1 = rooms_svc.post(room.id, author="user:operator",
                            body="first", kind="text")

        # Simulate a replicated row from peer beta with a much higher
        # legacy_id (beta has been posting independently).
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO room_posts "
                "(uuid, legacy_id, origin_peer, room_id, author, body, kind, ts) "
                "VALUES (?, 9999, 'beta', ?, 'b', 'from beta', 'text', ?)",
                (new_uuid(), room.id,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

        # Local sequence keeps going from p1, not from 9999.
        p2 = rooms_svc.post(room.id, author="user:operator",
                            body="after replication", kind="text")
        assert p2.id == p1.id + 1
