"""Tests for awm.server — FastAPI integration tests via TestClient."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from awm.server import app


@pytest.fixture()
def client(awm_workspace):
    """TestClient that skips lifespan (DB already init'd by awm_workspace)."""
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestStatus:
    def test_get_status(self, client, awm_workspace):
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "workspace_root" in data


class TestLockEndpoints:
    def test_acquire_and_release(self, client):
        # Acquire
        resp = client.post("/locks", json={
            "resource_path": "file.txt",
            "holder_id": "agent-1",
            "holder_pid": os.getpid(),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["lock"]["holder_id"] == "agent-1"

        # List
        resp = client.get("/locks")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

        # Release
        resp = client.delete("/locks", params={"path": "file.txt", "holder": "agent-1"})
        assert resp.status_code == 200
        assert "released" in resp.json()["message"].lower()

    def test_acquire_conflict_409(self, client):
        client.post("/locks", json={
            "resource_path": "file.txt", "holder_id": "a1",
        })
        resp = client.post("/locks", json={
            "resource_path": "file.txt", "holder_id": "a2",
        })
        assert resp.status_code == 409

    def test_heartbeat(self, client):
        client.post("/locks", json={
            "resource_path": "file.txt", "holder_id": "a1",
        })
        resp = client.post("/locks/heartbeat", params={"holder": "a1"})
        assert resp.status_code == 200
        assert "1 lock(s)" in resp.json()["message"]

    def test_reap(self, client):
        resp = client.post("/locks/reap")
        assert resp.status_code == 200
        assert "reaped" in resp.json()


class TestSkillEndpoints:
    def test_list_skills(self, client, sample_skills_dir):
        resp = client.get("/skills")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_search_skills(self, client, sample_skills_dir):
        resp = client.get("/skills/search", params={"q": "mamba"})
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_get_skill(self, client, sample_skills_dir):
        resp = client.get("/skills/tools/git.md")
        assert resp.status_code == 200
        assert resp.json()["skill"]["name"] == "git"

    def test_get_skill_404(self, client, sample_skills_dir):
        resp = client.get("/skills/nonexistent.md")
        assert resp.status_code == 404


class TestScopeEndpoints:
    def test_list_scopes(self, client, seeded_scopes):
        resp = client.get("/scopes")
        assert resp.status_code == 200
        assert resp.json()["total"] == 3

    def test_list_scopes_filtered(self, client, seeded_scopes):
        resp = client.get("/scopes", params={"status": "active"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1


class TestSessionEndpoints:
    def test_list_sessions(self, client, seeded_sessions):
        resp = client.get("/sessions")
        assert resp.status_code == 200
        assert resp.json()["total"] == 3

    def test_get_session(self, client, seeded_sessions):
        list_resp = client.get("/sessions")
        entry_id = list_resp.json()["entries"][0]["id"]
        resp = client.get(f"/sessions/{entry_id}")
        assert resp.status_code == 200
        assert resp.json()["entry"]["id"] == entry_id
        assert "content" in resp.json()
