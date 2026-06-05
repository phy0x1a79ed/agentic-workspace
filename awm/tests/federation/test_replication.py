"""Tests for cr-sqlite replication infrastructure: extension loading, CRR
registration idempotency, /peer/db-sync endpoint round-trip, two-DB
convergence."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.federation, pytest.mark.slow]

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from awm.db import get_connection, init_db
from awm.services.replication import schema as repl_schema


def test_extension_available_or_skipped(awm_workspace):
    """Either the binary loaded, or the daemon ran without it. Both fine."""
    # crsqlite_available reflects the vendored binary's presence, not whether
    # any specific connection loaded it.
    assert isinstance(repl_schema.crsqlite_available(), bool)


def test_crr_registration_idempotent(awm_workspace):
    """register_all_crrs can run repeatedly without erroring or duplicating
    rows."""
    if not repl_schema.crsqlite_available():
        pytest.skip("cr-sqlite extension not vendored on this host")
    conn = get_connection()
    try:
        first = repl_schema.register_all_crrs(conn)
        second = repl_schema.register_all_crrs(conn)
        assert sorted(first) == sorted(second)
        # Self-registered tables include rooms and peers.
        assert "rooms" in first
        assert "peers" in first
    finally:
        repl_schema.finalize(conn)
        conn.close()


def test_rooms_insert_produces_changes(awm_workspace):
    """Inserting into a CRR table populates crsql_changes."""
    if not repl_schema.crsqlite_available():
        pytest.skip("cr-sqlite extension not vendored on this host")
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO rooms (id, host_peer_id, created_at, status) "
            "VALUES (?, ?, ?, 'active')",
            ("test-room", "host1", now),
        )
        conn.commit()
        rows = conn.execute(
            'SELECT COUNT(*) FROM crsql_changes WHERE "table" = ?',
            ("rooms",),
        ).fetchone()[0]
        assert rows > 0
    finally:
        repl_schema.finalize(conn)
        conn.close()


def test_db_sync_endpoint_returns_changes(awm_workspace, monkeypatch):
    """GET /peer/db-sync returns a JSON envelope with the local site's
    changes since the given cursor."""
    if not repl_schema.crsqlite_available():
        pytest.skip("cr-sqlite extension not vendored on this host")

    awm_dir = awm_workspace["awm_dir"]
    token_file = awm_dir / "auth.token"
    token_file.write_text("test-local-token\n")
    monkeypatch.setattr("awm.config.AUTH_TOKEN_FILE", token_file)
    monkeypatch.setattr("awm.config.EXPOSED_PID_FILE", awm_dir / "awm-exposed.pid")
    monkeypatch.setattr("awm.config.EXPOSED_LOG_FILE", awm_dir / "awm-exposed.log")
    monkeypatch.setattr(
        "awm.services.agent_instances.PROJECTS_DIR",
        awm_workspace["projects_dir"],
    )
    from awm.services import auth as _auth
    _auth._token_cache.update({"value": None, "mtime": None})
    _auth._challenges.clear()
    from awm.services import agent_instances
    agent_instances._registry_by_id.clear(); agent_instances._by_scope.clear(); agent_instances._by_agent_id.clear()
    agent_instances._by_scope.clear()
    monkeypatch.delenv("AWM_AUTH_TOKEN", raising=False)

    # Register a peer so X-Awm-From: capella passes peer-bearer auth.
    from awm.services.network import peers as peer_svc
    peer_svc.add_peer("capella", "self", peer_priority=10)
    # Install capella's token (same as local, since this is a single-process test).
    token_dir = awm_dir / "peers"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "capella.token").write_text("test-local-token\n")

    # Seed a row so there are changes to fetch.
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO rooms (id, host_peer_id, created_at, status) "
            "VALUES (?, ?, ?, 'active')",
            ("sync-room", "host1", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        repl_schema.finalize(conn)
        conn.close()

    from awm.exposed import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get(
            "/peer/db-sync?since=0",
            headers={
                "Authorization": "Bearer test-local-token",
                "X-Awm-From": "capella",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "site_id" in body
    assert "db_version" in body
    assert isinstance(body["changes"], list)
    assert any(c["table"] == "rooms" for c in body["changes"])


def test_db_sync_requires_peer_bearer(awm_workspace, monkeypatch):
    """/peer/db-sync rejects un-peered callers (no X-Awm-From)."""
    if not repl_schema.crsqlite_available():
        pytest.skip("cr-sqlite extension not vendored on this host")

    awm_dir = awm_workspace["awm_dir"]
    token_file = awm_dir / "auth.token"
    token_file.write_text("test-local-token\n")
    monkeypatch.setattr("awm.config.AUTH_TOKEN_FILE", token_file)
    monkeypatch.setattr("awm.config.EXPOSED_PID_FILE", awm_dir / "awm-exposed.pid")
    monkeypatch.setattr("awm.config.EXPOSED_LOG_FILE", awm_dir / "awm-exposed.log")
    monkeypatch.setattr(
        "awm.services.agent_instances.PROJECTS_DIR",
        awm_workspace["projects_dir"],
    )
    from awm.services import auth as _auth
    _auth._token_cache.update({"value": None, "mtime": None})
    _auth._challenges.clear()
    from awm.services import agent_instances
    agent_instances._registry_by_id.clear(); agent_instances._by_scope.clear(); agent_instances._by_agent_id.clear()
    agent_instances._by_scope.clear()
    monkeypatch.delenv("AWM_AUTH_TOKEN", raising=False)

    from awm.exposed import app
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get(
            "/peer/db-sync?since=0",
            headers={"Authorization": "Bearer test-local-token"},  # no X-Awm-From
        )
    assert r.status_code == 400  # missing X-Awm-From


def test_apply_changes_round_trip(awm_workspace, tmp_path):
    """End-to-end: build two separate DBs, INSERT on one, GET changes,
    apply on the other, assert the row appears."""
    if not repl_schema.crsqlite_available():
        pytest.skip("cr-sqlite extension not vendored on this host")

    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    init_db(db_a)
    init_db(db_b)

    # Write on A.
    conn_a = get_connection(db_a)
    try:
        conn_a.execute(
            "INSERT INTO rooms (id, host_peer_id, created_at, status) "
            "VALUES (?, ?, ?, 'active')",
            ("a-room", "host-a", datetime.now(timezone.utc).isoformat()),
        )
        conn_a.commit()
        rows = conn_a.execute(
            'SELECT "table", pk, cid, val, col_version, db_version, site_id, cl, seq '
            'FROM crsql_changes WHERE db_version > 0 ORDER BY db_version, seq'
        ).fetchall()
    finally:
        repl_schema.finalize(conn_a)
        conn_a.close()

    # Serialize to the JSON-envelope shape the endpoint produces.
    from awm.services.replication.endpoint import _b64, apply_changes
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

    # Apply on B.
    conn_b = get_connection(db_b)
    try:
        n = apply_changes(conn_b, changes)
        assert n > 0
        row = conn_b.execute(
            "SELECT id, host_peer_id FROM rooms WHERE id = 'a-room'"
        ).fetchone()
        assert row is not None
        assert row["id"] == "a-room"
        assert row["host_peer_id"] == "host-a"
    finally:
        repl_schema.finalize(conn_b)
        conn_b.close()
