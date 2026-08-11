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


def test_a_url_without_a_nonce_is_an_error_not_an_empty_string():
    """An empty nonce posts happily and comes back 401 somewhere else."""
    with pytest.raises(api.WorkbenchError):
        api.nonce_from("http://127.0.0.1:12203/")


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


def test_a_spent_nonce_fails_where_it_happened():
    """No cookie back means the nonce was already used — say so here, not as a
    401 three calls later."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid nonce"})

    with pytest.raises(api.WorkbenchError, match="no .* cookie|spent"):
        api.connect("http://127.0.0.1:12203/?nonce=abc123",
                    transport=_routes(handler))


def test_the_daemons_refusal_reaches_the_caller_verbatim():
    """The daemon's validation is the reason to use the API at all, so its
    `detail` is the useful half of the failure."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "detail": "path is a symlink; grant the directory it points at"})

    with pytest.raises(api.WorkbenchError) as caught:
        api.add_grant(_owner(handler), path="/x", mode="ro")
    assert caught.value.status == 400
    assert "symlink" in (caught.value.detail or "")


# ---------------------------------------------------------------------------
# Registering, versus connecting
# ---------------------------------------------------------------------------

def test_probe_reports_a_server_that_registered_but_cannot_launch():
    """The tool-permissions route answers 200 with an `error` string — a
    non-2xx never happens, so a status check alone would call this healthy."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "tools": [], "error": "Couldn't load tools: spawn ENOENT"})

    out = api.probe_tools(_owner(handler), "local:awm")
    assert out["tools"] == 0
    assert "ENOENT" in out["error"]


def test_probe_counts_the_tools_of_a_server_that_works():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tools": [{"name": "a"},
                                                   {"name": "b"}]})
    out = api.probe_tools(_owner(handler), "local:awm")
    assert out == {"tools": 2, "error": None}


def test_only_local_servers_come_back_from_the_listing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"id": "local:awm", "name": "awm", "source": "local-stdio"},
            {"id": "bundled:pubmed", "name": "pubmed", "source": "bundled"},
            {"id": "cf6b", "name": "bioRxiv", "source": "directory"},
        ])
    rows = api.list_local_servers(_owner(handler))
    assert [r["name"] for r in rows] == ["awm"]


def test_the_listing_asks_the_route_that_knows_about_local_servers():
    """`/api/mcp-servers` is the custom *remote* table and answers `[]` on a
    node with none — indistinguishable from "your local server isn't there",
    which is exactly how it was misread once."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, json=[])

    api.list_local_servers(_owner(handler))
    assert asked == ["/api/mcp-servers/connectors"]


# ---------------------------------------------------------------------------
# The store, which is how idempotence is decided
# ---------------------------------------------------------------------------

def _store(tmp_path, monkeypatch, doc):
    path = tmp_path / "mcp" / "local-mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    monkeypatch.setattr(api, "local_store_path", lambda: path)
    return path


def test_stored_server_is_found_by_name(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, {"servers": [
        {"name": "awm", "command": "/opt/awm-mcp", "args": [], "env": {}}]})
    assert api.stored_local_server("awm")["command"] == "/opt/awm-mcp"
    assert api.stored_local_server("other") is None


def test_a_missing_store_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "local_store_path",
                        lambda: tmp_path / "nope.json")
    assert api.stored_local_server("awm") is None


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


def test_listing_connectors_does_not_probe_unless_asked(wired):
    hub_adapter, calls, state = wired
    state["servers"] = [{"id": "local:awm", "name": "awm",
                         "source": "local-stdio"}]
    out = hub_adapter._connector({})
    assert out["probed"] is False
    assert not any(p.endswith("/tool-permissions") for _, p in calls)
    assert "tools" not in out["servers"][0]

    calls.clear()
    out = hub_adapter._connector({"probe": True})
    assert out["servers"][0]["tools"] == 1


def test_registering_without_a_command_is_refused_before_any_call(wired):
    hub_adapter, calls, _ = wired
    with pytest.raises(ValueError, match="command"):
        hub_adapter._connector({"name": "awm"})
    assert ("POST", "/api/mcp-servers/local") not in calls


def test_granting_the_same_path_twice_reports_no_change(wired):
    hub_adapter, _, _ = wired
    first = hub_adapter._grants({"path": "/ws/projects"})
    assert first["changed"] is True
    assert first["grants"] == [{"path": "/ws/projects", "mode": "ro"}]

    second = hub_adapter._grants({"path": "/ws/projects"})
    assert second["changed"] is False


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
