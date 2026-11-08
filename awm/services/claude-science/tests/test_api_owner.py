"""Configuring the workbench through its own API.

Two things here are worth pinning rather than trusting. The daemon accepts the
owner token as a cookie *or* as a bearer, and only the bearer path skips the
double-submit CSRF hook — sending it as a cookie instead would fail on a header
nobody here would think to look for. And registering a local MCP server is not
the same event as that server connecting: a bad command registers with a clean
200 and fails later, so the verb has to report the second fact separately or a
broken connector reads as done.
"""

from __future__ import annotations

import json

import httpx
import pytest

from awm.claude_science import api

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

TOKEN = "tok-abc123"


def _routes(handler):
    return httpx.MockTransport(handler)


def _owner(handler) -> api.Owner:
    return api.Owner(TOKEN, transport=_routes(handler))


# ---------------------------------------------------------------------------
# Becoming the owner
# ---------------------------------------------------------------------------

def test_nonce_is_read_out_of_the_binarys_url():
    url = "http://127.0.0.1:12203/?nonce=deadbeef0123"
    assert api.nonce_from(url) == "deadbeef0123"


def test_connect_exchanges_the_nonce_and_authenticates_by_bearer():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/auth/nonce":
            return httpx.Response(
                302, headers={"location": "/",
                              "set-cookie": f"{api.AUTH_COOKIE}={TOKEN}; Path=/"})
        return httpx.Response(200, json={"ok": True})

    owner = api.connect("http://127.0.0.1:12203/?nonce=abc123",
                        transport=_routes(handler))
    owner.get("/api/me")

    assert seen[0].url.path == "/api/auth/nonce"
    assert b"nonce=abc123" in seen[0].content
    # The bearer is what makes this a program rather than a browser: the CSRF
    # preHandler returns early for header auth, so no token is fetched and none
    # is echoed. A cookie-authenticated client would need both.
    assert seen[-1].headers["authorization"] == f"Bearer {TOKEN}"
    assert not any(r.url.path == "/api/csrf" for r in seen)
    assert "x-operon-csrf" not in seen[-1].headers


# ---------------------------------------------------------------------------
# Registering, versus connecting
# ---------------------------------------------------------------------------

def test_only_local_servers_come_back_from_the_listing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"id": "local:awm", "name": "awm", "source": "local-stdio"},
            {"id": "bundled:pubmed", "name": "pubmed", "source": "bundled"},
            {"id": "cf6b", "name": "bioRxiv", "source": "directory"},
        ])
    rows = api.list_local_servers(_owner(handler))
    assert [r["name"] for r in rows] == ["awm"]


# ---------------------------------------------------------------------------
# The store, which is how idempotence is decided
# ---------------------------------------------------------------------------

def _store(tmp_path, monkeypatch, doc):
    path = tmp_path / "mcp" / "local-mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    monkeypatch.setattr(api, "local_store_path", lambda: path)
    return path


def test_a_malformed_store_reads_as_empty_rather_than_raising(tmp_path,
                                                              monkeypatch):
    """The daemon refuses to write a malformed store rather than wiping it, so
    one can persist; a verb that only wanted to *list* must not die on it."""
    _store(tmp_path, monkeypatch, "{not json")
    assert api.stored_local_server("awm") is None


# ---------------------------------------------------------------------------
# The verbs. These get called on every node, so re-running one must be a no-op
# rather than a second write — and must still say what the state is.
# ---------------------------------------------------------------------------

@pytest.fixture()
def wired(monkeypatch):
    """Drive the verbs against a recording daemon."""
    from awm.claude_science import hub_adapter

    calls: list[tuple[str, str]] = []
    state: dict = {"grants": [], "servers": [], "tools": [{"name": "t"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/api/mcp-servers/connectors":
            return httpx.Response(200, json=state["servers"])
        if path == "/api/mcp-servers/local":
            body = json.loads(request.content)
            row = {"id": f"local:{body['name']}", "name": body["name"],
                   "source": "local-stdio"}
            state["servers"].append(row)
            return httpx.Response(200, json=row)
        if path.endswith("/tool-permissions"):
            return httpx.Response(200, json={"tools": state["tools"]})
        if path == "/api/preferences/host-grants":
            if request.method == "POST":
                body = json.loads(request.content)
                if not any(g["path"] == body["path"] for g in state["grants"]):
                    state["grants"].append(body)
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"grants": state["grants"]})
        return httpx.Response(404, json={"detail": f"no route {path}"})

    monkeypatch.setattr(hub_adapter.SUPERVISOR, "login_url",
                        lambda: "http://127.0.0.1:12203/?nonce=abc")
    monkeypatch.setattr(
        api, "connect",
        lambda url, **kw: api.Owner(TOKEN, transport=_routes(handler)))
    return hub_adapter, calls, state


def test_registering_a_connector_twice_writes_once(wired, tmp_path,
                                                   monkeypatch):
    hub_adapter, calls, state = wired
    spec = {"name": "awm", "command": "/opt/bin/awm-mcp",
            "env": {"AWM_WORKSPACE": "/ws"}}

    first = hub_adapter._connector(dict(spec))
    assert first["changed"] is True
    assert first["tools"] == 1

    # Second call sees the persisted definition and must not POST again.
    _store(tmp_path, monkeypatch, {"servers": [
        {"name": "awm", "command": "/opt/bin/awm-mcp", "args": [],
         "env": {"AWM_WORKSPACE": "/ws"}}]})
    calls.clear()
    second = hub_adapter._connector(dict(spec))
    assert second["changed"] is False
    assert ("POST", "/api/mcp-servers/local") not in calls
    # …but it still reports whether the thing actually connects.
    assert second["tools"] == 1


def test_a_changed_command_is_not_mistaken_for_the_same_connector(
        wired, tmp_path, monkeypatch):
    """The listing API omits command/args/env, so comparing what it returns
    would call every re-registration a no-op."""
    hub_adapter, calls, _ = wired
    _store(tmp_path, monkeypatch, {"servers": [
        {"name": "awm", "command": "/old/awm-mcp", "args": [], "env": {}}]})
    out = hub_adapter._connector({"name": "awm", "command": "/new/awm-mcp"})
    assert out["changed"] is True
    assert ("POST", "/api/mcp-servers/local") in calls


def test_grants_defaults_to_read_only(wired):
    hub_adapter, _, state = wired
    hub_adapter._grants({"path": "/ws/data"})
    assert state["grants"][0]["mode"] == "ro"


def test_an_unknown_grant_mode_is_refused_here(wired):
    """'read-only' is the plausible typo, and the daemon would 400 on it — but
    only after a nonce has been spent."""
    hub_adapter, _, _ = wired
    with pytest.raises(ValueError, match="'ro' or 'rw'"):
        hub_adapter._grants({"path": "/ws/data", "mode": "read-only"})
