"""Tests for awm.registry — operation definitions and auto-generation."""

from __future__ import annotations


import pytest
pytestmark = [pytest.mark.unit, pytest.mark.smoke]

import json

import pytest

from awm._lib.tool_dispatch import handle_tool
from awm.operations.sessions import SESSION_OPERATIONS
from awm._lib.operations import operations_to_mcp_tools, dispatch_operation


class TestMCPToolGeneration:
    def test_session_tools_generated(self):
        tools = operations_to_mcp_tools(SESSION_OPERATIONS)
        names = {t.name for t in tools}
        assert "session_log" in names
        assert "session_get" in names
        assert "session_resolve" in names
        assert "session_search" in names
        assert "session_list" not in names  # collapsed into session_search
        assert len(names) == 4

    def test_session_log_schema(self):
        tools = operations_to_mcp_tools(SESSION_OPERATIONS)
        log_tool = next(t for t in tools if t.name == "session_log")
        props = log_tool.inputSchema["properties"]
        assert "project" in props
        assert "scope" in props
        assert "summary" in props
        assert "session_log" in [log_tool.name]
        assert "required" in log_tool.inputSchema
        assert "project" in log_tool.inputSchema["required"]

    def test_session_search_schema(self):
        tools = operations_to_mcp_tools(SESSION_OPERATIONS)
        search_tool = next(t for t in tools if t.name == "session_search")
        props = search_tool.inputSchema["properties"]
        assert "project" in props
        assert "query" in props
        assert "status" in props
        assert "limit" in props
        assert "offset" in props

    def test_session_get_schema(self):
        tools = operations_to_mcp_tools(SESSION_OPERATIONS)
        get_tool = next(t for t in tools if t.name == "session_get")
        props = get_tool.inputSchema["properties"]
        assert "session_id" in props


class TestDispatch:
    def test_dispatch_session_search_status_all(self, awm_workspace, seeded_sessions):
        # status='all' overrides the new active-only default.
        result = dispatch_operation(
            "session_search", {"status": "all"}, SESSION_OPERATIONS,
        )
        assert result is not None
        assert result.total == 3

    def test_dispatch_session_get(self, awm_workspace, seeded_sessions):
        list_result = dispatch_operation(
            "session_search", {"status": "all"}, SESSION_OPERATIONS,
        )
        entry_id = list_result.entries[0].id
        result = dispatch_operation(
            "session_get", {"session_id": entry_id}, SESSION_OPERATIONS
        )
        assert result is not None
        assert result.entry.id == entry_id

    def test_dispatch_unknown_returns_none(self):
        result = dispatch_operation("unknown_op", {}, SESSION_OPERATIONS)
        assert result is None


class TestSessionMCPIntegration:
    def test_session_search_viahandle_tool(self, awm_workspace, seeded_sessions):
        result = handle_tool("session_search", {"status": "all"})
        data = json.loads(result)
        assert data["total"] == 3

    def test_session_get_viahandle_tool(self, awm_workspace, seeded_sessions):
        list_result = json.loads(handle_tool("session_search", {"status": "all"}))
        entry_id = list_result["entries"][0]["id"]
        result = handle_tool("session_get", {"session_id": entry_id})
        data = json.loads(result)
        assert data["entry"]["id"] == entry_id
        assert "content" in data
