"""Phase 7 — lock federation.

Tests:
- _resolve_owner: each resource-path prefix resolves correctly; @peer
  suffix overrides DB lookup; unstructured paths return None (local-only).
- acquire/release/heartbeat federate to the owning peer when origin_peer
  is remote, and stay local otherwise.
- The /peer/lock/{acquire,release,heartbeat} routes correctly auth and
  perform the local operation.
- Federation-unreachable errors surface as a clear LockActionResponse.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from awm.db import get_connection
from awm.models import LockAcquireRequest, LockActionResponse
from awm.services import locks
from awm.services.network.federation import FederationError
from awm.services.replication.schema import new_uuid


# ---------------------------------------------------------------------------
# _resolve_owner
# ---------------------------------------------------------------------------

class TestResolveOwner:
    def test_scope_local(self, awm_workspace, monkeypatch):
        monkeypatch.setattr(locks, "_local_peer_id", lambda: "alpha")
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO scopes "
                "(uuid, legacy_id, origin_peer, project, scope, status, "
                " branch, worktree, repo_path, session, created_at, updated_at) "
                "VALUES (?, 1, 'alpha', 'p', 's', 'active', 'b', '/w', '/r', 1, ?, ?)",
                (new_uuid(), now, now),
            )
            conn.commit()
        finally:
            conn.close()
        assert locks._resolve_owner("scope:p/s") == "alpha"

    def test_scope_remote_via_db(self, awm_workspace):
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO scopes "
                "(uuid, legacy_id, origin_peer, project, scope, status, "
                " branch, worktree, repo_path, session, created_at, updated_at) "
                "VALUES (?, 1, 'beta', 'p', 's', 'active', 'b', '/w', '/r', 1, ?, ?)",
                (new_uuid(), now, now),
            )
            conn.commit()
        finally:
            conn.close()
        assert locks._resolve_owner("scope:p/s") == "beta"

    def test_explicit_peer_suffix_wins(self, awm_workspace):
        # No DB row; the @peer suffix alone should resolve.
        assert locks._resolve_owner("scope:p/s@gamma") == "gamma"

    def test_artifact_resolves_from_legacy_id(self, awm_workspace):
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO artifacts "
                "(uuid, legacy_id, origin_peer, project, scope, name, "
                " artifact_type, path, status, created_at, updated_at) "
                "VALUES (?, 42, 'mira', 'p', 's', 'n', 'other', "
                " 'data/x.bin', 'current', ?, ?)",
                (new_uuid(), now, now),
            )
            conn.commit()
        finally:
            conn.close()
        assert locks._resolve_owner("artifact:42") == "mira"

    def test_room_resolves_from_host_peer(self, awm_workspace):
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO rooms (id, host_peer_id, created_at, status) "
                "VALUES ('demo-room', 'capella', ?, 'active')",
                (now,),
            )
            conn.commit()
        finally:
            conn.close()
        assert locks._resolve_owner("room:demo-room") == "capella"

    def test_unstructured_path_returns_none(self, awm_workspace):
        assert locks._resolve_owner("/tmp/some/file") is None
        assert locks._resolve_owner("not-a-resource-path") is None


# ---------------------------------------------------------------------------
# acquire/release/heartbeat federate when owner is remote
# ---------------------------------------------------------------------------

class TestAcquireFederates:
    def test_remote_owner_forwards_request(self, awm_workspace, monkeypatch):
        monkeypatch.setattr(locks, "_local_peer_id", lambda: "alpha")
        # scope owned by beta.
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO scopes "
                "(uuid, legacy_id, origin_peer, project, scope, status, "
                " branch, worktree, repo_path, session, created_at, updated_at) "
                "VALUES (?, 1, 'beta', 'p', 's', 'active', 'b', '/w', '/r', 1, ?, ?)",
                (new_uuid(), now, now),
            )
            conn.commit()
        finally:
            conn.close()

        with patch(
            "awm.services.network.federation.forward_lock",
            return_value={"message": "Lock acquired", "lock": None},
        ) as mock_fed:
            resp = locks.acquire(LockAcquireRequest(
                resource_path="scope:p/s", holder_id="agent-1"))
        mock_fed.assert_called_once()
        called_peer = mock_fed.call_args.args[0]
        called_op = mock_fed.call_args.args[1]
        assert called_peer == "beta"
        assert called_op == "acquire"
        assert resp.message == "Lock acquired"

    def test_local_owner_does_not_federate(self, awm_workspace, monkeypatch):
        monkeypatch.setattr(locks, "_local_peer_id", lambda: "alpha")
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO scopes "
                "(uuid, legacy_id, origin_peer, project, scope, status, "
                " branch, worktree, repo_path, session, created_at, updated_at) "
                "VALUES (?, 1, 'alpha', 'p', 's', 'active', 'b', '/w', '/r', 1, ?, ?)",
                (new_uuid(), now, now),
            )
            conn.commit()
        finally:
            conn.close()

        with patch(
            "awm.services.network.federation.forward_lock"
        ) as mock_fed:
            resp = locks.acquire(LockAcquireRequest(
                resource_path="scope:p/s", holder_id="agent-1"))
        mock_fed.assert_not_called()
        assert resp.message == "Lock acquired"

    def test_unstructured_path_is_local(self, awm_workspace, monkeypatch):
        monkeypatch.setattr(locks, "_local_peer_id", lambda: "alpha")
        with patch(
            "awm.services.network.federation.forward_lock"
        ) as mock_fed:
            resp = locks.acquire(LockAcquireRequest(
                resource_path="/projects/p/scopes/s", holder_id="a"))
        mock_fed.assert_not_called()
        assert resp.message == "Lock acquired"

    def test_federation_unreachable_surfaces_error(self, awm_workspace, monkeypatch):
        monkeypatch.setattr(locks, "_local_peer_id", lambda: "alpha")
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO scopes "
                "(uuid, legacy_id, origin_peer, project, scope, status, "
                " branch, worktree, repo_path, session, created_at, updated_at) "
                "VALUES (?, 1, 'beta', 'p', 's', 'active', 'b', '/w', '/r', 1, ?, ?)",
                (new_uuid(), now, now),
            )
            conn.commit()
        finally:
            conn.close()
        with patch(
            "awm.services.network.federation.forward_lock",
            side_effect=FederationError("beta down"),
        ):
            resp = locks.acquire(LockAcquireRequest(
                resource_path="scope:p/s", holder_id="a"))
        assert "federation_unreachable" in resp.message


# ---------------------------------------------------------------------------
# Federated routes (/peer/lock/*) end-to-end
# ---------------------------------------------------------------------------

@pytest.fixture()
def peer_locks_client(awm_workspace, monkeypatch):
    awm_dir = awm_workspace["awm_dir"]
    (awm_dir / "auth.token").write_text("test-local-token\n")
    token_dir = awm_dir / "peers"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "beta.token").write_text("beta-secret\n")

    monkeypatch.setattr("awm.config.AUTH_TOKEN_FILE", awm_dir / "auth.token")
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
    agent_instances._registry.clear()
    agent_instances._by_scope.clear()

    from awm.services.network import peers as peer_svc
    peer_svc.add_peer("beta", "self", peer_priority=20)

    from awm.exposed import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestPeerLockRoutes:
    def test_acquire_release_round_trip(self, peer_locks_client):
        r = peer_locks_client.post(
            "/peer/lock/acquire",
            headers={
                "Authorization": "Bearer beta-secret",
                "X-Awm-From": "beta",
            },
            json={"resource_path": "scope:p/s", "holder_id": "remote-agent"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["message"] == "Lock acquired"
        assert body["lock"]["resource_path"] == "scope:p/s"

        # Second acquire from a different holder conflicts.
        r = peer_locks_client.post(
            "/peer/lock/acquire",
            headers={
                "Authorization": "Bearer beta-secret",
                "X-Awm-From": "beta",
            },
            json={"resource_path": "scope:p/s", "holder_id": "other"},
        )
        body = r.json()
        assert "Conflict" in body["message"]

        # Release the original.
        r = peer_locks_client.post(
            "/peer/lock/release",
            headers={
                "Authorization": "Bearer beta-secret",
                "X-Awm-From": "beta",
            },
            json={"resource_path": "scope:p/s", "holder_id": "remote-agent"},
        )
        assert r.status_code == 200
        assert r.json()["message"] == "Lock released"

    def test_heartbeat_renews_count(self, peer_locks_client):
        peer_locks_client.post(
            "/peer/lock/acquire",
            headers={
                "Authorization": "Bearer beta-secret",
                "X-Awm-From": "beta",
            },
            json={"resource_path": "scope:p/s", "holder_id": "remote-agent"},
        )
        r = peer_locks_client.post(
            "/peer/lock/heartbeat",
            headers={
                "Authorization": "Bearer beta-secret",
                "X-Awm-From": "beta",
            },
            json={"holder_id": "remote-agent"},
        )
        assert r.status_code == 200
        assert "1 lock" in r.json()["message"]

    def test_refuses_without_peer_bearer(self, peer_locks_client):
        r = peer_locks_client.post(
            "/peer/lock/acquire",
            headers={"Authorization": "Bearer beta-secret"},  # no X-Awm-From
            json={"resource_path": "scope:p/s", "holder_id": "x"},
        )
        assert r.status_code == 400
