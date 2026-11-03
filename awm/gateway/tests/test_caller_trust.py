"""The caller's trust level and own scope reach every service call.

The journal and the browser bound a session by `_caller_trust` and
`_caller_scope`. The gateway reads the level from reflection's ledger, where a
session with no entry is trusted, and the scope from the session record's cwd.
"""

import json

import pytest

from awm.gateway import mcp_caller
from awm.gateway import server as _server
from awm.gateway.server import _stamp_trust

pytestmark = [pytest.mark.smoke]

UUID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
R9 = "cc" + UUID.replace("-", "")


@pytest.fixture
def node(tmp_path, monkeypatch):
    sessions, projects = tmp_path / "sessions", tmp_path / "projects"
    worktree = projects / "awm" / "redux-x"
    (worktree / ".awm").mkdir(parents=True)
    (worktree / "core").mkdir()
    sessions.mkdir()
    rec = {"pid": 4242, "kind": "bg", "jobId": "j1", "name": "n",
           "sessionId": UUID, "cwd": str(worktree / "core")}
    (sessions / "4242.json").write_text(json.dumps(rec))
    (sessions / "5151.json").write_text(json.dumps({"sessionId": "f" * 32, "cwd": "/tmp"}))
    ledger = tmp_path / "trust.json"
    ledger.write_text(json.dumps({R9: {"level": "cooperative", "origin": "seat:ana"}}))
    monkeypatch.setattr(mcp_caller, "_default_sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(mcp_caller, "_default_ledger", lambda: str(ledger))
    monkeypatch.setattr(mcp_caller, "_default_projects", lambda: str(projects))
    return ledger


def test_a_session_gets_its_ledger_level_and_its_scope(node):
    assert mcp_caller.caller_trust(4242) == {"_caller_trust": "cooperative",
                                             "_caller_scope": "awm/redux-x"}


def test_a_session_with_no_entry_is_trusted_and_outside_a_project_has_no_scope(node):
    assert mcp_caller.caller_trust(5151) == {"_caller_trust": "trusted"}


def test_no_record_or_no_ledger_reads_as_trusted(node):
    assert mcp_caller.caller_trust(77) == {"_caller_trust": "trusted"}
    node.unlink()
    assert mcp_caller.caller_trust(4242)["_caller_trust"] == "trusted"


def test_every_domain_call_is_stamped_and_the_models_values_are_dropped(node):
    args = {"verb": "fetch", "args": {"project": "awm", "_caller_trust": "trusted",
                                      "_caller_scope": "awm/other"}}
    _stamp_trust("journal", args, "4242")
    assert args["args"] == {"project": "awm", "_caller_trust": "cooperative",
                            "_caller_scope": "awm/redux-x"}
    bare = {"verb": "viewer"}
    _stamp_trust("browser", bare, "4242")
    assert bare["args"]["_caller_trust"] == "cooperative"


def test_the_flat_shape_is_stamped_at_the_top(node):
    flat = {"seat": "s1", "_caller_trust": "trusted"}
    _stamp_trust("browser_tab_open", flat, "4242")
    assert flat["_caller_trust"] == "cooperative" and flat["_caller_scope"] == "awm/redux-x"


def test_a_descendant_pid_resolves_to_its_session(node, monkeypatch):
    monkeypatch.setattr(mcp_caller, "resolve_caller_pid", lambda pid, **kw: 4242)
    args = {"verb": "fetch", "args": {}}
    _stamp_trust("journal", args, None, "999")
    assert args["args"]["_caller_trust"] == "cooperative"


def test_no_session_pid_carries_neither_key(node):
    args = {"verb": "fetch", "args": {"_caller_trust": "trusted", "_caller_scope": "a/b"}}
    _stamp_trust("journal", args, None)
    assert args["args"] == {}


def test_an_edge_request_keeps_the_far_proxys_pair(node):
    sent = {"_caller_trust": "untrusted", "_caller_scope": "awm/x"}
    args = {"verb": "fetch", "args": dict(sent)}
    _stamp_trust("journal", args, "4242", as_="peer:altair")
    assert args["args"] == sent


def test_a_redirect_carries_the_callers_pair_to_the_peer(node):
    args = {"verb": "fetch", "args": {"_caller_trust": "trusted"}, "peer": "altair"}
    out = mcp_caller.forwarded_args("journal", args, "4242")
    assert out == {"verb": "fetch", "args": {"_caller_trust": "cooperative",
                                             "_caller_scope": "awm/redux-x"}}
    flat = mcp_caller.forwarded_args("journal_fetch", {"_caller_scope": "x/y"}, None)
    assert flat == {}


def test_invoke_stamps_the_call_it_dispatches(node, monkeypatch):
    from fastapi.testclient import TestClient

    seen = {}

    async def dispatch(name, args, as_=None):
        seen.update(args)
        return "ok"

    monkeypatch.setattr(_server.catalog, "dispatch", dispatch)
    monkeypatch.setattr(_server, "_caller_mode", lambda headers, as_: None)
    client = TestClient(_server.app)
    r = client.post("/invoke", headers={"X-Awm-Session-Pid": "4242"},
                    json={"name": "journal", "args": {"verb": "fetch", "args": {}}})
    assert r.status_code == 200, r.text
    assert seen["args"]["_caller_trust"] == "cooperative"
    assert seen["args"]["_caller_scope"] == "awm/redux-x"
