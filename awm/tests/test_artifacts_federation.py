"""Phase 6 — artifact content federation.

Tests the GET /artifacts/{id}/content surface:
- Local-origin rows: served directly from disk.
- Remote-origin rows: proxied to GET /peer/artifacts/{id}/content on the
  owning peer (federated httpx call mocked).
- Missing files / dead peers turn into clean HTTP errors.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from awm.config import WORKSPACE_ROOT
from awm.db import get_connection
from awm.services import artifacts
from awm.services.replication.schema import new_uuid


def _seed_local_row(workspace, project="p", scope="s", name="doc",
                    rel_path="data/doc.bin", body=b"hello") -> int:
    """Insert a local-origin artifact row + write its file to disk."""
    full = workspace / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(body)

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        legacy = conn.execute(
            "SELECT COALESCE(MAX(legacy_id), 0) + 1 FROM artifacts"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO artifacts "
            "(uuid, legacy_id, origin_peer, project, scope, name, "
            " artifact_type, path, status, created_at, updated_at) "
            "VALUES (?, ?, '', ?, ?, ?, 'other', ?, 'current', ?, ?)",
            (new_uuid(), legacy, project, scope, name, rel_path, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return legacy


def _seed_remote_row(legacy_id=1, origin="beta", rel_path="data/remote.bin"):
    """Insert a metadata-only row that came in via CRR from another peer."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO artifacts "
            "(uuid, legacy_id, origin_peer, project, scope, name, "
            " artifact_type, path, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'p', 's', 'remote-doc', 'other', ?, "
            " 'current', ?, ?)",
            (new_uuid(), legacy_id, origin, rel_path, now, now),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

class TestGetContentLocal:
    def test_returns_local_bytes(self, awm_workspace):
        aid = _seed_local_row(awm_workspace["workspace"], body=b"local-bytes")
        assert artifacts.get_content(aid) == b"local-bytes"

    def test_missing_file_raises_not_found(self, awm_workspace):
        aid = _seed_local_row(awm_workspace["workspace"], body=b"x")
        # Delete the file out-of-band.
        (awm_workspace["workspace"] / "data/doc.bin").unlink()
        with pytest.raises(artifacts.ArtifactNotFound):
            artifacts.get_content(aid)

    def test_unknown_id_raises_not_found(self, awm_workspace):
        with pytest.raises(artifacts.ArtifactNotFound):
            artifacts.get_content(9999)


class TestGetContentRemote:
    def test_federates_to_origin_peer(self, awm_workspace):
        _seed_remote_row(legacy_id=7, origin="beta")
        with patch(
            "awm.services.network.federation.forward_artifact_content",
            return_value=b"from-beta",
        ) as mock_fed:
            assert artifacts.get_content("7@beta") == b"from-beta"
        mock_fed.assert_called_once_with("beta", 7)

    def test_federation_failure_raises_unavailable(self, awm_workspace):
        from awm.services.network.federation import FederationError
        _seed_remote_row(legacy_id=8, origin="beta")
        with patch(
            "awm.services.network.federation.forward_artifact_content",
            side_effect=FederationError("beta unreachable"),
        ):
            with pytest.raises(artifacts.ArtifactContentUnavailable):
                artifacts.get_content("8@beta")

    def test_bare_id_prefers_local_when_both_exist(self, awm_workspace, monkeypatch):
        monkeypatch.setattr(artifacts, "_local_peer_id", lambda: "alpha")
        # Local row with legacy_id=5 (alpha).
        _seed_local_row(
            awm_workspace["workspace"], rel_path="data/alpha.bin",
            body=b"alpha-bytes",
        )
        # Push the local row's origin to 'alpha' so the local-prefer branch
        # finds it.
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE artifacts SET origin_peer='alpha' WHERE path='data/alpha.bin'"
            )
            conn.commit()
        finally:
            conn.close()

        # Also insert a beta row with the same legacy_id.
        local_id = conn.execute  # noqa: F841 (silence linter)
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT legacy_id FROM artifacts WHERE path='data/alpha.bin'"
            ).fetchone()
            legacy = row["legacy_id"]
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO artifacts "
                "(uuid, legacy_id, origin_peer, project, scope, name, "
                " artifact_type, path, status, created_at, updated_at) "
                "VALUES (?, ?, 'beta', 'p', 's', 'r', 'other', ?, "
                " 'current', ?, ?)",
                (new_uuid(), legacy, "data/beta.bin", now, now),
            )
            conn.commit()
        finally:
            conn.close()

        # Bare ref -> local.
        assert artifacts.get_content(legacy) == b"alpha-bytes"


# ---------------------------------------------------------------------------
# HTTP — local /artifacts/{id}/content
# ---------------------------------------------------------------------------

@pytest.fixture()
def local_client(awm_workspace, monkeypatch):
    """TestClient against awm.server.app with auth disabled at the
    middleware layer (server.py doesn't have one — it relies on exposed.py
    when wrapped). Direct hits to the core app skip auth, which is
    appropriate for these unit tests."""
    from awm.server import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestLocalContentRoute:
    def test_returns_local_file(self, local_client, awm_workspace):
        aid = _seed_local_row(awm_workspace["workspace"], body=b"local-route")
        r = local_client.get(f"/artifacts/{aid}/content")
        assert r.status_code == 200, r.text
        assert r.content == b"local-route"

    def test_404_on_missing_artifact(self, local_client):
        r = local_client.get("/artifacts/9999/content")
        assert r.status_code == 404

    def test_502_when_federation_fails(self, local_client, awm_workspace):
        from awm.services.network.federation import FederationError
        _seed_remote_row(legacy_id=12, origin="beta")
        with patch(
            "awm.services.network.federation.forward_artifact_content",
            side_effect=FederationError("boom"),
        ):
            r = local_client.get("/artifacts/12@beta/content")
        assert r.status_code == 502


# ---------------------------------------------------------------------------
# HTTP — federated /peer/artifacts/{id}/content
# ---------------------------------------------------------------------------

@pytest.fixture()
def peer_federation_client(awm_workspace, monkeypatch):
    """TestClient against awm.exposed.app with one registered peer so
    /peer/* routes accept its bearer."""
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


class TestFederatedContentRoute:
    def test_serves_local_origin_to_peer(self, peer_federation_client, awm_workspace):
        aid = _seed_local_row(awm_workspace["workspace"], body=b"fed-bytes")
        r = peer_federation_client.get(
            f"/peer/artifacts/{aid}/content",
            headers={
                "Authorization": "Bearer beta-secret",
                "X-Awm-From": "beta",
            },
        )
        assert r.status_code == 200, r.text
        assert r.content == b"fed-bytes"

    def test_refuses_without_peer_bearer(self, peer_federation_client):
        r = peer_federation_client.get(
            "/peer/artifacts/1/content",
            headers={"Authorization": "Bearer beta-secret"},  # no X-Awm-From
        )
        assert r.status_code == 400

    def test_404_when_no_local_origin_row(self, peer_federation_client):
        r = peer_federation_client.get(
            "/peer/artifacts/9999/content",
            headers={
                "Authorization": "Bearer beta-secret",
                "X-Awm-From": "beta",
            },
        )
        assert r.status_code == 404
