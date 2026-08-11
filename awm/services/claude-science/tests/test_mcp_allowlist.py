"""The allowlist is the only boundary between the workbench and all of awm.

awm has no per-caller mode and no read-only credential: whatever can reach the
loopback bus can call `ssh`, `compute`, `social`, gateway control and the agent
write verbs. This bridge hands a tool surface to a model running in another
product, so the list of what it may call is a security control, and these tests
are what stop it eroding — particularly the distinction between *hiding* a tool
and *refusing* it, which is the one an attacker cares about.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from awm.claude_science import mcp_bridge

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _call(message, handler=None):
    """Drive one JSON-RPC message against a fake gateway."""
    async def _default(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected gateway call: {request.url}")

    async def _run():
        transport = httpx.MockTransport(handler or _default)
        async with httpx.AsyncClient(transport=transport) as client:
            return await mcp_bridge.handle_rpc(message, client)

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Refusal, not concealment
# ---------------------------------------------------------------------------

def test_a_non_allowlisted_verb_is_refused_on_call():
    """The listing is a convenience; the caller composes the request itself, so
    the gate has to be on the call."""
    reply = _call({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": mcp_bridge.tool_name("ssh", "connect"),
                   "arguments": {"host": "capella"}},
    })
    assert "error" in reply
    assert "allowlist" in reply["error"]["message"]


def test_a_dangerous_verb_on_an_allowed_domain_is_still_refused():
    """Allowlisting a domain must not allowlist the domain."""
    assert "scope" in mcp_bridge.ALLOW
    reply = _call({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": mcp_bridge.tool_name("scope", "delete"),
                   "arguments": {}},
    })
    assert "error" in reply


def test_a_malformed_tool_name_is_rejected_before_anything_else():
    for name in ("scope_search", "awm__scope", "", "awm__a__b__c"):
        reply = _call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": name}})
        assert "error" in reply, name


def test_the_default_allowlist_excludes_the_dangerous_domains():
    """A regression guard on the list itself, so widening it is deliberate."""
    for domain in ("ssh", "vpn", "2fa", "social", "agent", "orch", "compute",
                   "services", "gateway", "auth", "reflection"):
        assert domain not in mcp_bridge.DEFAULT_ALLOWLIST, domain


def test_no_write_shaped_verb_is_allowlisted_by_default():
    forbidden = {"create", "delete", "complete", "post", "update", "remove",
                 "write", "merge", "scatter", "gather", "restore", "import"}
    for domain, verbs in mcp_bridge.DEFAULT_ALLOWLIST.items():
        assert not (set(verbs) & forbidden), (domain, verbs)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_an_allowlisted_verb_reaches_the_gateway():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": {"scopes": ["svc-notes"]}})

    reply = _call({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": mcp_bridge.tool_name("scope", "search"),
                   "arguments": {"query": "notes"}},
    }, handler)

    assert seen["body"] == {"name": "scope_search", "args": {"query": "notes"}}
    assert seen["url"].endswith("/invoke")
    assert "svc-notes" in reply["result"]["content"][0]["text"]


def test_a_gateway_failure_is_reported_as_a_tool_error_not_a_crash():
    """A bridge that raises takes the connector down for every other tool."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    reply = _call({
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": mcp_bridge.tool_name("scope", "search"),
                   "arguments": {}},
    }, handler)
    assert reply["result"]["isError"] is True


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def test_tools_list_returns_only_allowlisted_pairs():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tools": [
            {"name": "scope_search", "description": "find scopes",
             "inputSchema": {"type": "object"}},
            {"name": "scope_delete", "description": "destroy a scope"},
            {"name": "ssh_connect", "description": "open a session"},
        ]})

    reply = _call({"jsonrpc": "2.0", "id": 6, "method": "tools/list"}, handler)
    names = [t["name"] for t in reply["result"]["tools"]]
    assert names == [mcp_bridge.tool_name("scope", "search")]


def test_listing_survives_a_verb_whose_name_contains_an_underscore():
    """`scope_data_status` must resolve to (scope, data_status), not (scope,
    data) — the reason the catalog is indexed by exact name rather than split."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tools": [
            {"name": "scope_data_status", "description": "data view"},
        ]})

    reply = _call({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, handler)
    names = [t["name"] for t in reply["result"]["tools"]]
    assert mcp_bridge.tool_name("scope", "data_status") in names
    assert mcp_bridge.split_tool(names[0]) == ("scope", "data_status")


def test_an_allowlisted_domain_that_is_not_running_is_simply_absent():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tools": []})

    reply = _call({"jsonrpc": "2.0", "id": 8, "method": "tools/list"}, handler)
    assert reply["result"]["tools"] == []


# ---------------------------------------------------------------------------
# Protocol basics
# ---------------------------------------------------------------------------

def test_initialize_advertises_tools():
    reply = _call({"jsonrpc": "2.0", "id": 9, "method": "initialize",
                   "params": {}})
    assert reply["result"]["protocolVersion"] == mcp_bridge.PROTOCOL_VERSION
    assert "tools" in reply["result"]["capabilities"]


def test_a_notification_gets_no_reply():
    assert _call({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_an_unparseable_allowlist_does_not_widen_the_surface(monkeypatch):
    monkeypatch.setenv("CLAUDE_SCIENCE_MCP_ALLOW", "{not json")
    assert mcp_bridge._load_allowlist() == dict(mcp_bridge.DEFAULT_ALLOWLIST)


def test_an_empty_allowlist_is_honoured(monkeypatch):
    """Wiring the connector but exposing nothing is a legitimate posture."""
    monkeypatch.setenv("CLAUDE_SCIENCE_MCP_ALLOW", "{}")
    assert mcp_bridge._load_allowlist() == {}
