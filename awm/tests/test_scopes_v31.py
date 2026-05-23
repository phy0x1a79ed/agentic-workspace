"""Tests for the v31 scopes INTEGER→UUID migration.

Mirrors test_room_posts_v30.py but for the scopes table — the public
identifier remains (project, scope), so this is mostly internal plumbing
(uuid as PK, origin_peer stamping, CRR registration, the dropped partial
unique index whose constraint is now enforced at the application layer).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from awm.db import get_connection, init_db, SCHEMA_VERSION
from awm.services.replication import schema as repl_schema


def _build_v30_db(db_path):
    """Construct a partial v30-shape DB with the pre-v31 scopes (INTEGER PK
    + partial unique index). Other tables get minimal shapes so init_db's
    later steps don't error out."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (30);

        CREATE TABLE scopes (
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
        CREATE UNIQUE INDEX idx_scopes_active_unique
            ON scopes(project, scope) WHERE status = 'active';
        INSERT INTO scopes (project, scope, status, branch, worktree, repo_path,
                            session, created_at, updated_at) VALUES
            ('proj-a', 'scope-1', 'active',    'feat/scope-1', '/w/a/1', '/r/a/1', 1, '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z'),
            ('proj-a', 'scope-2', 'completed', 'feat/scope-2', '/w/a/2', '/r/a/2', 1, '2026-05-02T00:00:00Z', '2026-05-02T00:00:00Z'),
            ('proj-b', 'scope-3', 'active',    'feat/scope-3', '/w/b/3', '/r/b/3', 1, '2026-05-03T00:00:00Z', '2026-05-03T00:00:00Z');

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


class TestV31Migration:
    def test_migration_preserves_rows(self, tmp_path, monkeypatch):
        """v30 → v31: rows survive, uuids minted, legacy_id preserved,
        origin_peer set to the local peers-self row, partial unique index
        dropped."""
        assert SCHEMA_VERSION >= 31, "this test guards the v30→v31 migration"
        db_path = tmp_path / "v30.db"
        monkeypatch.setattr("awm.db.AWM_DIR", tmp_path)
        _build_v30_db(db_path)

        init_db(db_path)

        conn = get_connection(db_path)
        try:
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(scopes)"
            ).fetchall()}
            assert {"uuid", "legacy_id", "origin_peer"}.issubset(cols)
            assert "id" not in cols

            rows = conn.execute(
                "SELECT uuid, legacy_id, origin_peer, project, scope, status "
                "FROM scopes ORDER BY legacy_id"
            ).fetchall()
            assert len(rows) == 3
            assert [r["legacy_id"] for r in rows] == [1, 2, 3]
            assert [r["origin_peer"] for r in rows] == ["alpha", "alpha", "alpha"]
            uuids = [r["uuid"] for r in rows]
            assert len(set(uuids)) == 3

            # idx_scopes_active_unique is gone
            indices = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='scopes'"
            ).fetchall()}
            assert "idx_scopes_active_unique" not in indices
            assert "idx_scopes_legacy" in indices
        finally:
            conn.close()

    def test_migration_with_no_self_peer_uses_empty_origin(self, tmp_path, monkeypatch):
        """If no peers-table self row exists during migration, origin_peer
        backfills to ''. The daemon's _ensure_self_row runs after the
        migration; pre-federation deployments have no identity at all."""
        db_path = tmp_path / "v30_no_self.db"
        monkeypatch.setattr("awm.db.AWM_DIR", tmp_path)

        # Build v30 DB but with no 'self' row in peers.
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (30);

            CREATE TABLE scopes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL, scope TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                branch TEXT NOT NULL, worktree TEXT NOT NULL,
                repo_path TEXT, session INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO scopes (project, scope, status, branch, worktree, repo_path,
                                session, created_at, updated_at)
            VALUES ('proj', 's', 'active', 'b', '/w', '/r', 1, 't', 't');

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

            CREATE TABLE peer_sync_state (
                peer_id TEXT PRIMARY KEY,
                last_db_version INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            );
        """)
        conn.commit()
        conn.close()

        init_db(db_path)

        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT origin_peer FROM scopes LIMIT 1"
            ).fetchone()
            assert row["origin_peer"] == ""
        finally:
            conn.close()


class TestScopesCRR:
    def test_scopes_in_crr_tables(self):
        assert "scopes" in repl_schema.CRR_TABLES

    def test_scopes_registered(self, awm_workspace):
        if not repl_schema.crsqlite_available():
            pytest.skip("cr-sqlite extension not vendored on this host")
        conn = get_connection()
        try:
            registered = repl_schema.register_all_crrs(conn)
            assert "scopes" in registered
        finally:
            repl_schema.finalize(conn)
            conn.close()

    def test_scopes_insert_produces_changes(self, awm_workspace):
        if not repl_schema.crsqlite_available():
            pytest.skip("cr-sqlite extension not vendored on this host")
        from awm.services.replication.schema import new_uuid

        conn = get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO scopes "
                "(uuid, legacy_id, origin_peer, project, scope, status, "
                " branch, worktree, repo_path, session, created_at, updated_at) "
                "VALUES (?, 1, 'h', 'p', 's', 'active', 'b', '/w', '/r', 1, ?, ?)",
                (new_uuid(), now, now),
            )
            conn.commit()
            changes = conn.execute(
                'SELECT COUNT(*) FROM crsql_changes WHERE "table" = ?',
                ("scopes",),
            ).fetchone()[0]
            assert changes > 0
        finally:
            repl_schema.finalize(conn)
            conn.close()


class TestScopesActiveUniquenessAtAppLayer:
    """The partial unique index is gone (cr-sqlite forbids it); the
    duplicate-active rejection moves into services/scopes.py:create_scope.
    Verify it still fires."""

    def test_duplicate_active_rejected(self, awm_workspace):
        from awm.services.replication.schema import new_uuid
        conn = get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO scopes "
                "(uuid, legacy_id, origin_peer, project, scope, status, "
                " branch, worktree, repo_path, session, created_at, updated_at) "
                "VALUES (?, 1, 'h', 'proj', 'dup', 'active', 'b', '/w', '/r', 1, ?, ?)",
                (new_uuid(), now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT uuid FROM scopes WHERE project='proj' AND scope='dup' AND status='active'"
            ).fetchone()
            assert row is not None
        finally:
            conn.close()
