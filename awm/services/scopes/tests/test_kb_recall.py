"""scope_fetch ranks with kb where kb holds every post, and with the local index otherwise."""

from __future__ import annotations

import pytest


@pytest.fixture
def posts(scopes_workspace, monkeypatch):
    from awm.scopes import channel, search_index
    monkeypatch.setattr(channel, "KB_RECALL", True)
    a = channel.post("awm", "dev", author="agent:awm/dev", kind="journal", body="the telescope mirror cracked")
    b = channel.post("awm", "dev", author="agent:awm/dev", kind="journal", body="unrelated lunch plans")
    search_index.flush()
    return a, b


def _kb(monkeypatch, reply=None, exc=None):
    calls = []

    def call_sync(service, fn, args=None, **kw):
        calls.append((service, fn, args, kw))
        if exc:
            raise exc
        return reply

    monkeypatch.setattr("awm.gatewayclient.call_sync", call_sync)
    return calls


def test_kb_ranks_when_it_answers(posts, monkeypatch):
    from awm.scopes import channel
    a, b = posts
    calls = _kb(monkeypatch, {"hits": [{"ref": {"id": b.id}, "score": 0.9, "snippet": "from kb"},
                                       {"ref": {"id": "gone"}, "score": 0.5}]})
    got, degraded, semantic = channel.search(query="telescope", project="awm", kind="journal", limit=5)
    assert semantic == "kb" and degraded is None
    assert [p.id for p in got] == [b.id]
    assert got[0].match == {"score": 0.9, "snippet": "from kb"}
    service, fn, args, kw = calls[0]
    assert (service, fn) == ("kb", "recall")
    assert args["require_complete"] is True and args["sources"] == ["posts"]
    assert args["project"] == "awm" and args["kind"] == "journal" and args["limit"] == 5
    assert kw["timeout"] == channel.KB_TIMEOUT_S


def test_refusal_falls_back_to_the_local_index(posts, monkeypatch):
    from awm.gatewayclient import GatewayCallError
    from awm.scopes import channel
    a, _ = posts
    _kb(monkeypatch, exc=GatewayCallError(502, '{"error": "kb 409: posts incomplete"}', service="kb"))
    got, _, semantic = channel.search(query="telescope mirror", project="awm")
    assert semantic == "local"
    assert got[0].id == a.id


def test_filters_kb_cannot_apply_stay_local(posts, monkeypatch):
    from awm.scopes import channel
    calls = _kb(monkeypatch, {"hits": []})
    _, _, semantic = channel.search(query="telescope", author="agent:awm/dev")
    assert semantic == "local" and calls == []
    _, _, semantic = channel.search(query="telescope", kind="system")
    assert semantic == "none" and calls == []


def test_reply_reports_semantic_only_for_a_query(posts, monkeypatch):
    from awm.scopes.operations.scope_channel import _handle_scope_fetch
    _kb(monkeypatch, exc=RuntimeError("no kb service"))
    assert _handle_scope_fetch({"project": "awm", "query": "telescope"})["semantic"] == "local"
    assert "semantic" not in _handle_scope_fetch({"project": "awm", "scope": "dev"})
