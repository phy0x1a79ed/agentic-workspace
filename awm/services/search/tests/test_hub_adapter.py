"""The manifest and handlers: what the gateway sees of the search service."""

from __future__ import annotations

import pytest

from awm.search import hub_adapter
from awm.search.hub_adapter import API_MANIFEST, HANDLERS
from awm.search.query import parse

from .conftest import doc, synced

pytestmark = [pytest.mark.search]

FNS = {f["name"]: f for f in API_MANIFEST["functions"]}


def test_the_read_verbs_open_to_a_granted_peer():
    for name in ("query", "get", "sources", "status"):
        assert FNS[name]["effect"] == "read" and FNS[name]["category"] == "archive", name
        assert "surfaces" not in FNS[name]


def test_reindex_is_hidden_and_closed_to_peers():
    assert FNS["reindex"]["surfaces"] == ["cli", "http"]
    assert FNS["reindex"]["effect"] == "write" and "category" not in FNS["reindex"]


def test_every_verb_is_published_under_the_search_tool_and_wired():
    assert set(HANDLERS) == set(FNS) == {"query", "get", "sources", "status", "reindex"}
    for name, fn in FNS.items():
        assert fn["tool"] == f"search_{name}"


def test_the_query_verb_declares_every_key_it_reads_and_every_key_it_sends_a_peer():
    declared = {p["name"] for p in FNS["query"]["params"]}
    full = {"query": "q", "sources": ["a"], "project": "p", "scope": "s", "kind": "k",
            "after": "2026-01-01", "before": "2026-02-01", "limit": 3, "peers": True}
    assert set(full) == declared
    sent = parse(full).forwarded()
    assert set(sent) <= declared
    assert {"query", "limit", "peers"} <= set(sent)


def test_the_grants_the_gateway_injects_are_not_a_declared_argument():
    for fn in FNS.values():
        assert "_grants" not in {p["name"] for p in fn["params"]}


async def test_the_handlers_answer_a_granted_peer_only_its_sources(archive, gateway, monkeypatch):
    gateway.plug("journals", {"j1": doc("Sink", "kitchen sink journal")})
    gateway.plug("precedence", {"p1": doc("Sink", "kitchen sink precedent")})
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    granted = {"_grants": ["journals"]}
    result = await HANDLERS["query"]({"query": "kitchen sink", **granted}, "peer:shaula")
    assert {h["source"] for h in result["hits"]} == {"journals"}
    assert [s["name"] for s in (await HANDLERS["sources"](granted))["sources"]] == ["journals"]
    assert (await HANDLERS["status"](granted))["sources"] == {"journals": 1}
    with pytest.raises(ValueError, match="no source"):
        await HANDLERS["get"]({"source": "precedence", "id": "p1", **granted})
    everything = await HANDLERS["sources"]({})
    assert {s["name"] for s in everything["sources"]} == {"journals", "precedence"}


async def test_reindex_queues_a_pass_and_names_a_bad_source(archive, gateway, monkeypatch):
    gateway.plug("journals", {"j1": doc("Sink", "kitchen sink journal")})
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    out = await HANDLERS["reindex"]({"source": "journals", "force": "true"})
    assert out["resync"] == ["journals"] and out["engine"]["documents"] == 1
    assert archive._due["journals"] <= archive.now()
    with pytest.raises(ValueError, match="not a running index source"):
        await HANDLERS["reindex"]({"source": "nothing"})
