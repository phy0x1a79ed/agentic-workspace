"""Tests for the v34 artifacts INTEGER→UUID migration.

Gates phase 6 (artifact content federation): the new origin_peer column
+ uuid PK is what the content-fetch endpoint will consult to decide
between local FileResponse and federated GET /peer/artifacts/{id}/content.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.artifacts, pytest.mark.slow]

import sqlite3
from datetime import datetime, timezone

import pytest

from awm.db import get_connection, init_db, SCHEMA_VERSION
from awm.services.replication import schema as repl_schema


def _build_v33_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (33);

        CREATE TABLE artifacts (
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
        CREATE UNIQUE INDEX idx_artifacts_path ON artifacts(path);
        INSERT INTO artifacts (project, scope, name, artifact_type, path,
                               status, created_at, updated_at) VALUES
            ('p', 's', 'doc',  'markdown', 'data/doc.md',  'current', 't', 't'),
            ('p', 's', 'fig',  'image',    'data/fig.png', 'current', 't', 't'),
            ('q', 't', 'note', 'markdown', 'data/note.md', 'stale',   't', 't');

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


class TestV34Migration:
    def test_migration_preserves_rows(self, tmp_path, monkeypatch):
        assert SCHEMA_VERSION >= 34
        db_path = tmp_path / "v33.db"
        monkeypatch.setattr("awm.db.AWM_DIR", tmp_path)
        _build_v33_db(db_path)

        init_db(db_path)

        conn = get_connection(db_path)
        try:
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(artifacts)"
            ).fetchall()}
            assert {"uuid", "legacy_id", "origin_peer"}.issubset(cols)
            assert "id" not in cols

            rows = conn.execute(
                "SELECT legacy_id, origin_peer, name, status "
                "FROM artifacts ORDER BY legacy_id"
            ).fetchall()
            assert [r["legacy_id"] for r in rows] == [1, 2, 3]
            assert [r["name"] for r in rows] == ["doc", "fig", "note"]
            assert all(r["origin_peer"] == "alpha" for r in rows)

            # idx_artifacts_path is no longer UNIQUE.
            idx = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='index' AND name='idx_artifacts_path'"
            ).fetchone()
            assert idx is not None
            assert "UNIQUE" not in (idx["sql"] or "").upper()
        finally:
            conn.close()


class TestArtifactsCRR:
    def test_artifacts_in_crr_tables(self):
        assert "artifacts" in repl_schema.CRR_TABLES

    def test_artifacts_registered(self, awm_workspace):
        if not repl_schema.crsqlite_available():
            pytest.skip("cr-sqlite extension not vendored on this host")
        conn = get_connection()
        try:
            registered = repl_schema.register_all_crrs(conn)
            assert "artifacts" in registered
        finally:
            repl_schema.finalize(conn)
            conn.close()


class TestArtifactsUpsertScopedToOriginPeer:
    """Two peers may now register the same path independently — the
    legacy upsert-on-path moves to upsert-on-(path, origin_peer=local)."""

    def test_remote_origin_row_is_not_overwritten(self, awm_workspace, monkeypatch):
        from awm.models import ArtifactRegisterRequest
        from awm.services import artifacts
        from awm.services.replication.schema import new_uuid

        monkeypatch.setattr(artifacts, "_local_peer_id", lambda: "alpha")

        # Pretend a remote row for the same path already replicated in.
        conn = get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO artifacts "
                "(uuid, legacy_id, origin_peer, project, scope, name, "
                " artifact_type, path, status, created_at, updated_at) "
                "VALUES (?, 1, 'beta', 'p', 's', 'remote-name', 'doc', "
                " 'data/shared.md', 'current', ?, ?)",
                (new_uuid(), now, now),
            )
            conn.commit()
        finally:
            conn.close()

        # Local registration of the same path should INSERT a new row, not
        # update the remote one.
        info = artifacts.register_artifact(ArtifactRegisterRequest(
            project="p", scope="s", name="local-name",
            artifact_type="other", path="data/shared.md",
        ))
        assert info.origin_peer == "alpha"
        assert info.name == "local-name"

        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT origin_peer, name FROM artifacts WHERE path='data/shared.md' "
                "ORDER BY origin_peer"
            ).fetchall()
        finally:
            conn.close()
        assert [(r["origin_peer"], r["name"]) for r in rows] == [
            ("alpha", "local-name"),
            ("beta", "remote-name"),
        ]


class TestArtifactsDisambiguation:
    def test_delete_with_peer_suffix(self, awm_workspace, monkeypatch):
        from awm.services import artifacts
        from awm.services.replication.schema import new_uuid

        monkeypatch.setattr(artifacts, "_local_peer_id", lambda: "alpha")

        conn = get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            for origin, name in (("alpha", "local"), ("beta", "remote")):
                conn.execute(
                    "INSERT INTO artifacts "
                    "(uuid, legacy_id, origin_peer, project, scope, name, "
                    " artifact_type, path, status, created_at, updated_at) "
                    "VALUES (?, 5, ?, 'p', 's', ?, 'd', ?, 'current', ?, ?)",
                    (new_uuid(), origin, name, f"data/{name}.md", now, now),
                )
            conn.commit()
        finally:
            conn.close()

        # Bare 5 targets local.
        result = artifacts.delete_artifact(5)
        assert result["name"] == "local"

        # "5@beta" targets remote.
        result = artifacts.delete_artifact("5@beta")
        assert result["name"] == "remote"
