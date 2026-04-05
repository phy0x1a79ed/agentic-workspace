"""Tests for awm.mcp_server — _serialize, _handle_tool dispatch."""

from __future__ import annotations

import json
import subprocess

import pytest

from awm.mcp_server import _serialize, _handle_tool


class TestSerialize:
    def test_serialize_pydantic(self):
        from awm.models import StatusResponse
        obj = StatusResponse(workspace_root="/tmp")
        result = _serialize(obj)
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["workspace_root"] == "/tmp"

    def test_serialize_dict(self):
        obj = {"key": "value", "num": 42}
        result = _serialize(obj)
        data = json.loads(result)
        assert data["key"] == "value"


class TestHandleToolSkills:
    def test_skills_list(self, awm_workspace, sample_skills_dir):
        result = _handle_tool("skills_list", {})
        data = json.loads(result)
        assert data["total"] >= 1

    def test_skills_search(self, awm_workspace, sample_skills_dir):
        result = _handle_tool("skills_search", {"query": "mamba"})
        data = json.loads(result)
        assert data["total"] >= 1

    def test_skills_get(self, awm_workspace, sample_skills_dir):
        result = _handle_tool("skills_get", {"path": "tools/git.md"})
        data = json.loads(result)
        assert data["skill"]["name"] == "git"

    def test_skills_reindex(self, awm_workspace, sample_skills_dir):
        result = _handle_tool("skills_reindex", {})
        data = json.loads(result)
        assert data["message"] == "Index regenerated"


class TestHandleToolLocks:
    def test_lock_acquire_and_release(self, awm_workspace):
        result = _handle_tool("lock_acquire", {
            "resource_path": "file.txt", "holder_id": "a1",
        })
        data = json.loads(result)
        assert data["lock"]["holder_id"] == "a1"

        result = _handle_tool("lock_release", {
            "resource_path": "file.txt", "holder_id": "a1",
        })
        data = json.loads(result)
        assert "released" in data["message"].lower()

    def test_lock_list(self, awm_workspace, seeded_locks):
        result = _handle_tool("lock_list", {})
        data = json.loads(result)
        assert data["total"] == 3

    def test_lock_heartbeat(self, awm_workspace):
        _handle_tool("lock_acquire", {"resource_path": "f.txt", "holder_id": "a1"})
        result = _handle_tool("lock_heartbeat", {"holder_id": "a1"})
        data = json.loads(result)
        assert "1 lock(s)" in data["message"]


class TestHandleToolScopes:
    def test_scope_list(self, awm_workspace, seeded_scopes):
        result = _handle_tool("scope_list", {})
        data = json.loads(result)
        assert data["total"] == 3

    def test_scope_list_filtered(self, awm_workspace, seeded_scopes):
        result = _handle_tool("scope_list", {"status": "active"})
        data = json.loads(result)
        assert data["total"] == 1


class TestHandleToolSessions:
    def test_session_list(self, awm_workspace, seeded_sessions):
        result = _handle_tool("session_list", {})
        data = json.loads(result)
        assert data["total"] == 3

    def test_session_get(self, awm_workspace, seeded_sessions):
        list_result = json.loads(_handle_tool("session_list", {}))
        entry_id = list_result["entries"][0]["id"]
        result = _handle_tool("session_get", {"session_id": entry_id})
        data = json.loads(result)
        assert data["entry"]["id"] == entry_id
        assert "content" in data


class TestHandleToolStatus:
    def test_awm_status(self, awm_workspace):
        result = _handle_tool("awm_status", {})
        data = json.loads(result)
        assert data["status"] == "ok"


class TestHandleToolErrors:
    def test_unknown_tool(self, awm_workspace):
        with pytest.raises(ValueError, match="Unknown tool"):
            _handle_tool("nonexistent_tool", {})
