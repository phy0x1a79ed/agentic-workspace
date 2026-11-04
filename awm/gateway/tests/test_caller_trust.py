"""The caller's trust level and own scope reach every service call.

The journal and the browser bound a session by `_caller_trust` and
`_caller_scope`. The gateway walks the caller's pid to its session and asks
reflection's session_of for both: the level from reflection's ledger, and the
scope from the session's cwd. A chain no harness claims is a user's shell and
trusted. A lookup that fails is untrusted (fleet.md R103 item 2).
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
    """reflection's answers for two sessions, faked at the lookup."""
    projects = tmp_path / "projects"
    worktree = projects / "awm" / "redux-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: x\n")
    (worktree / "core").mkdir()
    answers = {
        4242: {"ok": True, "session_id": R9, "hosting": "background", "job": "j1",
               "name": "n", "trust": "cooperative", "cwd": str(worktree / "core")},
        # Started by hand: reflection's ledger has no entry, which it reads as trusted.
        5151: {"ok": True, "session_id": "cc" + "f" * 32, "trust": "trusted", "cwd": "/tmp"},
        6161: {"ok": True, "session_id": "cc" + "e" * 32, "cwd": "/tmp"},
    }
    # 900 is a CLI run inside session 4242. Every other pid has no parent.
    monkeypatch.setattr(mcp_caller, "_ppid_of", {900: 4242}.get)

    def ask(pid):
        if answers.get("down"):
            raise mcp_caller.LookupUnavailable("reflection is down")
        return answers.get(pid, {"ok": True, "known": False, "mode": None})

    monkeypatch.setattr(mcp_caller, "_ask_reflection", ask)
    monkeypatch.setattr(mcp_caller, "_cache", {})
    monkeypatch.setattr(mcp_caller, "_default_projects", lambda: str(projects))
    return answers


def test_a_session_gets_its_ledger_level_and_its_scope(node):
    assert mcp_caller.caller_trust(4242) == {"_caller_trust": "cooperative",
                                             "_caller_scope": "awm/redux-x"}


def test_a_hand_started_session_is_trusted_and_outside_a_project_has_no_scope(node):
    assert mcp_caller.caller_trust(5151) == {"_caller_trust": "trusted"}


def test_a_session_reflection_gives_no_level_is_untrusted(node):
    assert mcp_caller.caller_trust(6161) == {"_caller_trust": "untrusted"}


def test_a_cli_under_a_cooperative_session_is_cooperative(node):
    assert mcp_caller.caller_trust(900)["_caller_trust"] == "cooperative"
    assert mcp_caller.resolve_caller_pid(900) == 4242


def test_a_shell_no_harness_claims_is_trusted(node):
    assert mcp_caller.resolve(77)[0] == mcp_caller.NO_OWNER
    assert mcp_caller.caller_trust(77) == {"_caller_trust": "trusted"}


def test_a_failed_lookup_is_untrusted(node):
    node["down"] = True
    mcp_caller._cache.clear()
    assert mcp_caller.resolve(4242)[0] == mcp_caller.FAILED
    assert mcp_caller.caller_trust(4242) == {"_caller_trust": "untrusted"}
    args = {"verb": "fetch", "args": {}}
    _stamp_trust("journal", args, None, "900")
    assert args["args"]["_caller_trust"] == "untrusted"


def test_the_answer_is_remembered_for_a_while(node, monkeypatch):
    asked = []
    real = mcp_caller._ask_reflection
    monkeypatch.setattr(mcp_caller, "_ask_reflection", lambda pid: asked.append(pid) or real(pid))
    mcp_caller.caller_trust(4242)
    mcp_caller.caller_trust(4242)
    assert asked == [4242]
    monkeypatch.setattr(mcp_caller, "LOOKUP_TTL_S", 0.0)
    mcp_caller.caller_trust(4242)
    assert asked == [4242, 4242]


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


def test_a_descendant_pid_resolves_to_its_session(node):
    args = {"verb": "fetch", "args": {}}
    _stamp_trust("journal", args, None, "900")
    assert args["args"]["_caller_trust"] == "cooperative"


def test_no_pid_is_a_local_tool_and_trusted(node):
    args = {"verb": "fetch", "args": {"_caller_trust": "untrusted", "_caller_scope": "a/b"}}
    _stamp_trust("journal", args, None)
    assert args["args"] == {"_caller_trust": "trusted"}
    bad = {"verb": "fetch", "args": {}}
    _stamp_trust("journal", bad, "12x")
    assert bad["args"] == {"_caller_trust": "untrusted"}


def test_an_edge_request_keeps_the_far_proxys_pair(node):
    sent = {"_caller_trust": "untrusted", "_caller_scope": "awm/x"}
    args = {"verb": "fetch", "args": dict(sent)}
    _stamp_trust("journal", args, "4242", as_="peer:altair")
    assert args["args"] == sent
    unsent = {"verb": "fetch", "args": {}}
    _stamp_trust("journal", unsent, None, as_="peer:altair")
    assert unsent["args"] == {"_caller_trust": "trusted"}, "a peer's service sends no pair"


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
