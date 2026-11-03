"""The manifest and handlers: what the gateway sees of the search service."""

from __future__ import annotations

import pytest

from awm.search import hub_adapter
from awm.search.hub_adapter import API_MANIFEST, HANDLERS
from awm.search.query import parse

from .conftest import doc, synced

pytestmark = [pytest.mark.search]

FNS = {f["name"]: f for f in API_MANIFEST["functions"]}


def test_the_read_verbs_are_reads_on_every_surface():
    for name in ("query", "get", "sources", "status"):
        assert FNS[name]["effect"] == "read", name
        assert "surfaces" not in FNS[name]


def test_reindex_is_a_hidden_write():
    assert FNS["reindex"]["surfaces"] == ["cli", "http"]
    assert FNS["reindex"]["effect"] == "write"


def test_every_verb_is_published_under_the_search_tool_and_wired():
    assert set(HANDLERS) == set(FNS) == {"query", "get", "sources", "status", "reindex"}
    for name, fn in FNS.items():
        assert fn["tool"] == f"search_{name}"


def test_the_query_verb_declares_every_key_it_reads_and_every_key_it_sends_a_peer():
    declared = {p["name"] for p in FNS["query"]["params"]}
    full = {"query": "q", "sources": ["a"], "project": "p", "scope": "s", "kind": "k",
            "after": "2026-01-01", "before": "2026-02-01", "limit": 3, "peers": "fleet"}
    assert set(full) == declared
    sent = parse(full).forwarded()
    assert set(sent) <= declared
    assert {"query", "limit", "peers"} <= set(sent)


async def test_the_handlers_answer_an_admitted_peer_every_source(archive, gateway, monkeypatch):
    gateway.plug("journals", {"j1": doc("Sink", "kitchen sink journal")})
    gateway.plug("precedence", {"p1": doc("Sink", "kitchen sink precedent")})
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    monkeypatch.setattr(hub_adapter.privacy, "marks", lambda: [])
    result = await HANDLERS["query"]({"query": "kitchen sink"}, "peer:shaula")
    assert {h["source"] for h in result["hits"]} == {"journals", "precedence"}
    assert {s["name"] for s in (await HANDLERS["sources"]({}, "peer:shaula"))["sources"]} == {"journals", "precedence"}
    status = await HANDLERS["status"]({}, "peer:shaula")
    assert status["sources"] == {"journals": 1, "precedence": 1} and "registry" in status
    got = await HANDLERS["get"]({"source": "precedence", "id": "p1"}, "peer:shaula")
    assert got["document"]["origin"] == {"node": "local"}


async def test_reindex_queues_a_pass_and_names_a_bad_source(archive, gateway, monkeypatch):
    gateway.plug("journals", {"j1": doc("Sink", "kitchen sink journal")})
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    out = await HANDLERS["reindex"]({"source": "journals", "force": "true"})
    assert out["resync"] == ["journals"] and out["engine"]["documents"] == 1
    assert archive._due["journals"] <= archive.now()
    with pytest.raises(ValueError, match="not a running index source"):
        await HANDLERS["reindex"]({"source": "nothing"})
