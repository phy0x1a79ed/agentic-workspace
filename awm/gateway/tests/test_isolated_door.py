"""The isolated-session door (fleet.md R109): a caller token, a whitelist, one scope.

A fake token provider stands in for ``harness-cc caller_of_token``. Its answers have
the shape cch's ``hub_adapter._caller_of_token`` returns on feat/redux-harness.
"""

import json

import pytest
from fastapi.testclient import TestClient
from mcp.types import Tool

from awm.gateway import catalog, isolated
from awm.gateway import server as _server
from awm.gateway.server import _stamp_trust

pytestmark = [pytest.mark.smoke]

SESSION = "cc" + "0123456789abcdef" * 2
GOOD = {"ok": True, "known": True, "session_id": SESSION, "claude_session_id": "x",
        "name": "iso", "job": None, "pid": None, "live": True, "kind": "isolated",
        "container": "awm-iso-0123", "project": "awm", "scope": "iso", "cwd": "/w",
        "trust": "untrusted",
        "tools": {"journal": ["fetch", "post"], "reflection": ["whoami", "start_other"],
                  "ssh": ["run"], "harness-cc": ["send"]}}


def _entry(domain, verb, params):
    schema = {"type": "object", "properties": {p: {"type": "string"} for p in params}}
    return {"verb": verb, "tool": Tool(name=f"{domain}_{verb}", description=f"{verb} it.",
                                       inputSchema=schema),
            "effect": "read", "category": None, "op": None, "rec": None, "fn": verb}


CATALOG = {
    "journal": [_entry("journal", "fetch", ["project", "scope"]),
                _entry("journal", "post", ["project", "scope", "body", "to_scope"]),
                _entry("journal", "subscribe", ["project", "scope", "guest"])],
    "reflection": [_entry("reflection", "whoami", []),
                   _entry("reflection", "start_other", ["project", "scope"])],
    "ssh": [_entry("ssh", "run", ["host"])],
    "harness-cc": [_entry("harness-cc", "send", ["text"])],
    "search": [_entry("search", "query", ["q"])],
}


@pytest.fixture
def door(monkeypatch):
    ran = []
    asked = []

    async def provider(token):
        asked.append(token)
        if token == "broken":
            raise RuntimeError("harness-cc is not up")
        return GOOD if token == "good" else {"ok": True, "known": False}

    async def run_entry(entry, args, as_, grants=None, *, unknown):
        ran.append((entry["tool"].name, args))
        return json.dumps(args)

    isolated._seen.clear()
    monkeypatch.setattr(isolated, "_ask_provider", provider)
    monkeypatch.setattr(catalog, "_domain_catalog", lambda: CATALOG)
    monkeypatch.setattr(catalog, "_domain_blurbs", lambda: {})
    monkeypatch.setattr(catalog, "_run_entry", run_entry)
    monkeypatch.setattr(isolated, "node_name", lambda: "mizar")
    client = TestClient(isolated.door_app())
    client.ran, client.asked = ran, asked
    return client


def rpc(client, method, params=None, token="good"):
    headers = {TOKEN: token} if token else {}
    body = {"jsonrpc": "2.0", "method": method}
    if not method.startswith("notifications/"):
        body.update(id=7, params=params or {})
    return client.post("/mcp", headers=headers, json=body)


TOKEN = isolated.TOKEN_HEADER


def call(client, tool, arguments):
    r = rpc(client, "tools/call", {"name": tool, "arguments": arguments})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    return res["content"][0]["text"], res["isError"]


def test_no_token_an_unknown_one_or_no_provider_gets_no_door(door):
    assert rpc(door, "initialize", token=None).status_code == 401
    assert rpc(door, "initialize", token="guess").status_code == 401
    assert rpc(door, "initialize", token="broken").status_code == 503


def test_initialize_and_a_notification(door):
    r = rpc(door, "initialize", {"protocolVersion": "2025-06-18"})
    assert r.json()["result"]["serverInfo"]["name"] == "awm"
    assert rpc(door, "notifications/initialized").status_code == 202
    assert door.asked == ["good"], "a known token is asked once, then cached"


def test_the_list_is_the_whitelist_under_the_ceiling(door):
    tools = {t["name"]: t for t in rpc(door, "tools/list").json()["result"]["tools"]}
    assert sorted(tools) == ["journal", "reflection"]
    assert tools["journal"]["inputSchema"]["properties"]["verb"]["enum"] == ["fetch", "post", "describe"]
    assert tools["reflection"]["inputSchema"]["properties"]["verb"]["enum"] == ["whoami", "describe"]
    assert "peer" not in tools["journal"]["inputSchema"]["properties"]


def test_a_call_is_pinned_to_the_own_scope_and_stamped(door):
    text, err = call(door, "journal", {"verb": "post", "args": {
        "body": "hi", "_caller_trust": "trusted", "_caller_pid": 1, "_caller_session_id": "cc9"}})
    assert not err, text
    assert json.loads(text) == {"body": "hi", "project": "awm", "scope": "iso",
                                "_caller_trust": "untrusted", "_caller_scope": "awm/iso",
                                "_caller_session_id": SESSION}
    for bad in ({"scope": "other"}, {"project": "cloud"}, {"to_scope": "awm/other"}):
        text, err = call(door, "journal", {"verb": "post", "args": {"body": "x", **bad}})
        assert err and "403" in text and "own scope awm/iso" in text, bad
    text, err = call(door, "journal", {"verb": "fetch", "args": {"scope": "awm/iso"}})
    assert not err and json.loads(text)["scope"] == "awm/iso"


@pytest.mark.parametrize("tool,arguments,why", [
    ("journal", {"verb": "subscribe", "args": {"guest": "x"}}, "whitelist"),
    ("reflection", {"verb": "start_other", "args": {}}, "whitelist"),
    ("ssh", {"verb": "run", "args": {"host": "fir"}}, "whitelist"),
    ("harness-cc", {"verb": "send", "args": {"text": "x"}}, "whitelist"),
    ("search", {"verb": "query", "args": {"q": "x"}}, "whitelist"),
    ("journal", {"verb": "fetch", "args": {}, "peer": "altair"}, "this node only"),
])
def test_off_the_whitelist_under_the_ceiling_or_on_a_peer_is_refused(door, tool, arguments, why):
    text, err = call(door, tool, arguments)
    assert err and "403" in text and why in text, text
    assert door.ran == []


def test_this_node_by_name_is_local(door):
    text, err = call(door, "journal", {"verb": "fetch", "args": {}, "peer": "mizar"})
    assert not err, text


def test_describe_shows_only_the_whitelisted_verbs(door):
    text, err = call(door, "journal", {"verb": "describe"})
    verbs = [v["verb"] for v in json.loads(text)["verbs"]]
    assert not err and verbs == ["fetch", "post"]
    text, _ = call(door, "journal", {"verb": "describe", "args": {"verbs": ["subscribe"]}})
    assert json.loads(text)["verbs"] == []


def test_the_door_app_serves_mcp_only(door):
    assert door.post("/invoke", json={"name": "journal"}).status_code == 404
    assert door.get("/mcp", headers={TOKEN: "good"}).status_code == 405


def test_the_main_invoke_refuses_a_token(monkeypatch):
    async def dispatch(name, args, as_=None):
        raise AssertionError("dispatched a token call")

    monkeypatch.setattr(_server.catalog, "dispatch", dispatch)
    r = TestClient(_server.app).post("/invoke", headers={TOKEN: "good"},
                                     json={"name": "journal", "args": {"verb": "fetch"}})
    assert r.status_code == 403


def test_the_main_app_also_serves_the_door(door):
    client = TestClient(_server.app)
    r = client.post("/mcp", headers={TOKEN: "good"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 200 and r.json()["result"] == {}


def test_a_model_cannot_send_the_session_id_on_the_normal_path():
    args = {"verb": "fetch", "args": {"_caller_session_id": SESSION}}
    _stamp_trust("journal", args, None)
    assert args["args"] == {"_caller_trust": "trusted"}
    args = {"verb": "fetch", "args": {"_caller_session_id": SESSION}}
    _stamp_trust("journal", args, None, as_="peer:altair")
    assert args["args"] == {"_caller_trust": "trusted"}


def test_a_token_is_never_trusted_and_needs_a_scope():
    assert isolated.Holder.from_answer({**GOOD, "trust": "trusted"}).trust == "untrusted"
    assert isolated.Holder.from_answer({**GOOD, "trust": "cooperative"}).trust == "cooperative"
    assert isolated.Holder.from_answer({**GOOD, "scope": None}) is None
    assert isolated.Holder.from_answer({"ok": True, "known": False}) is None


def test_the_listen_address(monkeypatch):
    monkeypatch.setenv(isolated.LISTEN_ENV, "172.17.0.1:8765")
    assert isolated.listen_address() == ("172.17.0.1", 8765)
    monkeypatch.delenv(isolated.LISTEN_ENV)
    assert isolated.listen_address() is None
