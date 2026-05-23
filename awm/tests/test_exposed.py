"""Tests for the network-exposed listener: auth, gate, sessions, redaction."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def exposed_workspace(awm_workspace, monkeypatch):
    """Same as awm_workspace but also patches exposed-listener config knobs."""
    awm_dir = awm_workspace["awm_dir"]
    token_file = awm_dir / "auth.token"
    access_log = awm_dir / "access.log"
    pid_file = awm_dir / "awm-exposed.pid"
    log_file = awm_dir / "awm-exposed.log"

    monkeypatch.setattr("awm.config.AUTH_TOKEN_FILE", token_file)
    monkeypatch.setattr("awm.config.ACCESS_LOG", access_log)
    monkeypatch.setattr("awm.config.EXPOSED_PID_FILE", pid_file)
    monkeypatch.setattr("awm.config.EXPOSED_LOG_FILE", log_file)

    # The new service module imports PROJECTS_DIR at module load time —
    # patch the imported reference too, matching the pattern in conftest.
    monkeypatch.setattr(
        "awm.services.agent_instances.PROJECTS_DIR",
        awm_workspace["projects_dir"],
    )

    # Reset the auth-service token cache so each test's token file is re-read.
    from awm.services import auth as _auth
    _auth._token_cache.update({"value": None, "mtime": None})

    # Reset live-session registry between tests.
    from awm.services import agent_instances
    agent_instances._registry.clear()

    monkeypatch.delenv("AWM_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("AWM_ALLOW_DESTRUCTIVE", raising=False)

    return {**awm_workspace, "token_file": token_file, "access_log": access_log}


@pytest.fixture()
def good_token(exposed_workspace):
    token = "test-secret-token-abc123"
    exposed_workspace["token_file"].write_text(token + "\n")
    return token


@pytest.fixture()
def exposed_client(exposed_workspace):
    """TestClient for the exposed app. Skips lifespan since the test fixture
    already initialized the DB and we don't want it touching real PID files."""
    from awm.exposed import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ===========================================================================
# Auth
# ===========================================================================

class TestAuth:
    def test_no_token_configured_rejects(self, exposed_client):
        # No token file, no env var → all requests 401.
        resp = exposed_client.get("/status")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "unauthorized"

    def test_missing_header_rejects(self, exposed_client, good_token):
        resp = exposed_client.get("/status")
        assert resp.status_code == 401

    def test_wrong_token_rejects(self, exposed_client, good_token):
        resp = exposed_client.get("/status", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_valid_token_passes(self, exposed_client, good_token):
        resp = exposed_client.get(
            "/status", headers={"Authorization": f"Bearer {good_token}"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_token_from_env_overrides_file(self, exposed_client, exposed_workspace, monkeypatch):
        exposed_workspace["token_file"].write_text("file-token")
        monkeypatch.setenv("AWM_AUTH_TOKEN", "env-token")
        resp = exposed_client.get(
            "/status", headers={"Authorization": "Bearer env-token"}
        )
        assert resp.status_code == 200
        resp = exposed_client.get(
            "/status", headers={"Authorization": "Bearer file-token"}
        )
        assert resp.status_code == 401

    def test_token_rotation_without_restart(self, exposed_client, exposed_workspace):
        path = exposed_workspace["token_file"]
        path.write_text("token-v1")
        # Force a fresh mtime
        time.sleep(0.01)
        resp = exposed_client.get(
            "/status", headers={"Authorization": "Bearer token-v1"}
        )
        assert resp.status_code == 200

        # Rotate
        time.sleep(0.01)
        path.write_text("token-v2")
        # Bump mtime to ensure cache invalidates (filesystems with second
        # resolution can otherwise miss the change).
        new_mtime = path.stat().st_mtime + 1
        os.utime(path, (new_mtime, new_mtime))

        resp = exposed_client.get(
            "/status", headers={"Authorization": "Bearer token-v1"}
        )
        assert resp.status_code == 401
        resp = exposed_client.get(
            "/status", headers={"Authorization": "Bearer token-v2"}
        )
        assert resp.status_code == 200


# ===========================================================================
# Destructive-op gate
# ===========================================================================

class TestGate:
    def test_delete_scope_blocked_by_default(self, exposed_client, good_token, seeded_scopes):
        resp = exposed_client.delete(
            "/scopes/proj-a/scope-1",
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert resp.status_code == 403
        assert "destructive" in resp.json()["detail"].lower()

    def test_delete_scope_allowed_when_opted_in(
        self, exposed_client, good_token, seeded_scopes, monkeypatch
    ):
        monkeypatch.setenv("AWM_ALLOW_DESTRUCTIVE", "1")
        # The actual deletion may fail for unrelated reasons in this isolated
        # workspace, but it should NOT be 403.
        resp = exposed_client.delete(
            "/scopes/proj-a/scope-1",
            headers={"Authorization": f"Bearer {good_token}"},
        )
        assert resp.status_code != 403

    def test_create_project_blocked_by_default(self, exposed_client, good_token):
        resp = exposed_client.post(
            "/projects",
            headers={"Authorization": f"Bearer {good_token}"},
            json={"name": "newproj"},
        )
        assert resp.status_code == 403


# ===========================================================================
# Live agent sessions
# ===========================================================================

class _FakeProc:
    """Stand-in for asyncio.subprocess.Process."""

    def __init__(self, pid: int = 12345):
        self.pid = pid
        self.stdin = MagicMock()
        self.stdin.is_closing = MagicMock(return_value=False)
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.stdout = MagicMock()
        self._exit_future = asyncio.get_event_loop().create_future()
        # readline returns EOF immediately so reader_loop terminates.
        self.stdout.readline = AsyncMock(return_value=b"")
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        if not self._exit_future.done():
            self._exit_future.set_result(0)

    def kill(self):
        self.killed = True
        if not self._exit_future.done():
            self._exit_future.set_result(-9)

    async def wait(self):
        return await self._exit_future


@pytest.fixture()
def mock_subprocess(monkeypatch):
    """Replace asyncio.create_subprocess_exec with a fake."""
    fakes = []

    async def _fake_exec(*args, **kwargs):
        proc = _FakeProc(pid=10000 + len(fakes))
        fakes.append(proc)
        return proc

    monkeypatch.setattr(
        "awm.services.agent_instances.asyncio.create_subprocess_exec",
        _fake_exec,
    )
    return fakes


# TestSessions removed — the /agent-sessions/* surface no longer exists
# (rooms are the orchestration primitive; see M4 /rooms/* tests).


# ===========================================================================
# Error redaction
# ===========================================================================

class TestRedaction:
    def test_absolute_paths_scrubbed_from_errors(
        self, exposed_client, good_token, seeded_scopes
    ):
        # Hit an unauthenticated route to trigger a 401 with a redacted body.
        # The redactor runs on any 4xx/5xx JSON response.
        resp = exposed_client.post(
            "/inbox",
            headers={"Authorization": "Bearer wrong"},
            json={"scope": "workspace", "sender": "x", "msg_type": "notification",
                  "subject": "s", "body": "b"},
        )
        assert resp.status_code == 401
        body = resp.text
        assert "/home/" not in body
        assert "/tmp/" not in body
