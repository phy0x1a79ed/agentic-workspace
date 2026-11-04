"""cx probe: whether another node's cx answers, with no session rows."""

from __future__ import annotations

import asyncio

import pytest

from awm.cch import cx_adapter as hub_adapter, probe


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def here(monkeypatch):
    monkeypatch.setattr(probe, "node_name", lambda: "deneb")


def test_a_node_that_lists_answers_and_no_rows_travel(monkeypatch, here):
    async def call_peer(node, service, fn, args, timeout):
        assert (node, service, fn) == ("altair", "harness-cc", "list")
        assert timeout < 30
        return {"sessions": [{"job": "secret"}], "daemon": {"state": "up"}}

    monkeypatch.setattr(probe.gatewayclient, "call_peer", call_peer)
    out = run(probe.probe("altair"))
    assert out == {"ok": True, "node": "altair", "from": "deneb",
                   "answers": True, "daemon": "up"}


def test_a_node_that_does_not_answer(monkeypatch, here):
    async def call_peer(*a, **k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(probe.gatewayclient, "call_peer", call_peer)
    out = run(probe.probe("altair"))
    assert out["answers"] is False and "timed out" in out["reason"]


def test_a_refused_list_does_not_count_as_an_answer(monkeypatch, here):
    async def call_peer(*a, **k):
        return {"ok": False, "reason": "nope"}

    monkeypatch.setattr(probe.gatewayclient, "call_peer", call_peer)
    assert run(probe.probe("altair"))["answers"] is False


def test_the_probing_node_lists_itself_locally(monkeypatch, here):
    async def call(service, fn, args, timeout):
        return {"sessions": [], "daemon": {"state": "down"}}

    monkeypatch.setattr(probe.gatewayclient, "call", call)
    assert run(probe.probe("deneb"))["daemon"] == "down"


def test_a_foreign_caller_is_refused(monkeypatch):
    monkeypatch.setattr(hub_adapter.lifecycle, "caller_refusal", lambda as_: "foreign")
    assert run(hub_adapter._probe({"node": "altair"}, "peer:x")) == {"ok": False, "reason": "foreign"}


def test_a_probe_needs_a_node(monkeypatch):
    monkeypatch.setattr(hub_adapter.lifecycle, "caller_refusal", lambda as_: None)
    assert run(hub_adapter._probe({}, None))["ok"] is False
