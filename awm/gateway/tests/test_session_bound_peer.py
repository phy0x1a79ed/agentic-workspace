"""A verb that acts on the caller's own session is refused with `peer`, not forwarded.

The gateway stamps `_caller_pid` on its own copy of the call and hands a peer
redirect back to the proxy, which dials the peer without it. A session-bound verb
would then fail on the peer with "no calling session", so `/invoke` refuses it at
the redirect. Stamped verbs that use the pid only as a label stay routable.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.hub, pytest.mark.smoke]

from awm.gateway import peer_catalog, server

HEAD = {"X-Awm-Session-Pid": str(os.getpid()), "X-Awm-Peer-Redirect": "1"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "_caller_mode", lambda headers, as_: None)
    return TestClient(server.app, raise_server_exceptions=False)


@pytest.fixture
def dispatched(monkeypatch):
    """Redirect any call that names a peer, as the catalog does, and record the rest."""
    ran: list[dict] = []

    async def fake(name, args, as_=None):
        if args.get("peer"):
            raise peer_catalog.PeerRedirect(args["peer"], name, args.get("verb"))
        ran.append(args)
        return "ok"

    monkeypatch.setattr(server.catalog, "dispatch", fake)
    return ran


def call(client, tool, verb, peer=None):
    args = {"verb": verb, "args": {}}
    if peer:
        args["peer"] = peer
    return client.post("/invoke", json={"name": tool, "args": args}, headers=HEAD)


@pytest.mark.parametrize("tool,verb", [
    ("operations", "request_human"), ("operations", "task_claim"),
    ("reflection", "compact"), ("reflection", "message_self")])
def test_a_session_bound_verb_with_peer_is_refused_not_redirected(client, dispatched, tool, verb):
    resp = call(client, tool, verb, peer="capella")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert f"{tool}.{verb} acts on your own session" in detail
    assert "without peer" in detail
    assert dispatched == []


def test_the_review_alias_with_peer_is_refused(client, dispatched, monkeypatch):
    from awm.gateway import aliases

    monkeypatch.setattr(server.catalog, "resolve_alias", lambda name, args: aliases.resolve(
        name, args, flat_names=lambda: {"operations_request_human"},
        visible=lambda tool, verb: (tool, verb) == ("operations", "request_human")))
    resp = client.post("/invoke", json={"name": "reporting", "args": {
        "verb": "request_human_review", "args": {"summary": "done"}, "peer": "capella"}},
        headers=HEAD)
    assert resp.status_code == 400 and "request_human" in resp.json()["detail"]


def test_a_session_bound_verb_without_peer_runs_with_the_stamp(client, dispatched):
    resp = call(client, "operations", "request_human")
    assert resp.status_code == 200
    assert dispatched[0]["args"]["_caller_pid"] == os.getpid()


@pytest.mark.parametrize("tool,verb", [
    ("reflection", "list_other"), ("reflection", "start_other"),
    ("reflection", "stop_other"), ("operations", "board_list")])
def test_a_label_only_stamped_verb_is_still_redirected_to_a_peer(client, dispatched, tool, verb):
    resp = call(client, tool, verb, peer="capella")
    assert resp.status_code == 200
    assert resp.json()["peer_redirect"]["peer"] == "capella"


def test_every_session_bound_verb_is_a_stamped_one():
    for tool, verbs in server._SESSION_BOUND.items():
        stamped = server._CALLER_STAMPED[tool]
        assert stamped is None or verbs <= stamped


def test_the_reporting_verbs_that_act_on_a_session_are_session_bound():
    assert {"task_claim", "task_track", "board_watch", "board_unwatch"} <= server._SESSION_BOUND["operations"]
