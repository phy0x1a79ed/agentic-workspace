"""Tests for awm.tool_dispatch — _serialize and handle_tool.

The old in-process _handle_tool was factored out of awm.mcp_server into
awm.tool_dispatch so the new thin stdio proxy and the FastAPI /invoke
endpoint share a single dispatch body.
"""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.mcp, pytest.mark.slow, pytest.mark.subprocess]

import json
import subprocess

import pytest

from awm._lib.tool_dispatch import _serialize, handle_tool


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
        result = handle_tool("skills_list", {})
        data = json.loads(result)
        assert data["total"] >= 1

    def test_skills_search(self, awm_workspace, sample_skills_dir):
        result = handle_tool("skills_search", {"query": "mamba"})
        data = json.loads(result)
        assert data["total"] >= 1

    def test_skills_get(self, awm_workspace, sample_skills_dir):
        result = handle_tool("skills_get", {"path": "tools/git.md"})
        data = json.loads(result)
        assert data["skill"]["name"] == "git"

class TestHandleToolScopes:
    def test_scope_list(self, awm_workspace, seeded_scopes):
        result = handle_tool("scope_list", {})
        data = json.loads(result)
        assert data["total"] == 3

    def test_scope_list_filtered(self, awm_workspace, seeded_scopes):
        result = handle_tool("scope_list", {"status": "active"})
        data = json.loads(result)
        assert data["total"] == 1


class TestHandleToolSessions:
    def test_session_list(self, awm_workspace, seeded_sessions):
        result = handle_tool("session_list", {})
        data = json.loads(result)
        assert data["total"] == 3

    def test_session_get(self, awm_workspace, seeded_sessions):
        list_result = json.loads(handle_tool("session_list", {}))
        entry_id = list_result["entries"][0]["id"]
        result = handle_tool("session_get", {"session_id": entry_id})
        data = json.loads(result)
        assert data["entry"]["id"] == entry_id
        assert "content" in data


class TestHandleToolStatus:
    def test_awm_status(self, awm_workspace):
        result = handle_tool("awm_status", {})
        data = json.loads(result)
        assert data["status"] == "ok"


class TestHandleToolErrors:
    def test_unknown_tool(self, awm_workspace):
        with pytest.raises(ValueError, match="Unknown tool"):
            handle_tool("nonexistent_tool", {})
