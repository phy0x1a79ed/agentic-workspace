"""The manifest and handlers: what the gateway sees of the search service."""

from __future__ import annotations

import pytest

from awm.search import hub_adapter
from awm.search.hub_adapter import API_MANIFEST, HANDLERS
from awm.search.query import parse

from .conftest import doc, every, synced

pytestmark = [pytest.mark.search]

FNS = {f["name"]: f for f in API_MANIFEST["functions"]}


def test_the_read_verbs_are_on_every_surface():
    for name in ("query", "get", "sources", "status"):
        assert "surfaces" not in FNS[name], name


def test_reindex_is_hidden():
    assert FNS["reindex"]["surfaces"] == ["cli", "http"]


def test_every_verb_is_published_under_the_search_tool_and_wired():
    assert set(HANDLERS) == set(FNS) == {"query", "get", "sources", "status", "reindex", "judge", "judgements"}
    for name, fn in FNS.items():
        assert fn["tool"] == f"search_{name}"


def test_the_query_verb_declares_every_key_it_reads_and_every_key_it_sends_a_peer():
    declared = {p["name"] for p in FNS["query"]["params"]}
    full = {"query": "q", "sources": ["a", "b@altair"], "project": "p", "scope": "s", "tags": "x and !y",
            "after": "2026-01-01", "before": "2026-02-01", "limit": 3, "context": "awm/dev", "author": "Lee",
            "hop": {"via": "citations", "from": ["doi:1"]}}
    assert set(full) == declared
    q = parse(full)
    sent = q.forwarded(q.remote["altair"])
    assert set(sent) <= declared
    assert sent["sources"] == ["b"] and sent["tags"] == "x and !y" and "context" not in sent
    assert sent["hop"] == {"via": "citations", "from": ["doi:1"], "limit": 20}


def test_only_sources_are_required_and_the_retired_params_are_gone():
    params = {p["name"]: p for p in FNS["query"]["params"]}
    assert [n for n, p in params.items() if p.get("required")] == ["sources"]
    assert not {"kind", "peers", "passages"} & set(params)
    assert "kb hop" not in FNS["query"]["description"]


def test_the_judgement_verbs_are_closed_to_peers():
    for name in ("judge", "judgements"):
        assert "category" not in FNS[name], name
    assert FNS["judgements"]["surfaces"] == ["cli", "http"]


async def test_a_verdict_is_recorded_replaced_and_cleared(search_db):
    mark = {"query": "kitchen sink", "source": "journals", "id": "j1", "verdict": "relevant",
            "section": "exact", "set": "gold"}
    await HANDLERS["judge"](mark, "scope:awm/svc-kb")
    await HANDLERS["judge"]({**mark, "verdict": "irrelevant", "section": ""}, "tony")
    await HANDLERS["judge"]({**mark, "id": "j2"})
    rows = (await HANDLERS["judgements"]({"set": "gold"}))["judgements"]
    assert [(r["doc_id"], r["verdict"], r["section"], r["judge"]) for r in rows] == \
        [("j1", "irrelevant", "", "tony"), ("j2", "relevant", "exact", "")]
    assert (await HANDLERS["judge"]({**mark, "verdict": "clear"}))["cleared"] == 1
    assert [r["doc_id"] for r in (await HANDLERS["judgements"]({}))["judgements"]] == ["j2"]
    assert (await HANDLERS["judgements"]({"set": "t5"}))["judgements"] == []


async def test_a_bad_verdict_or_section_is_refused(search_db):
    mark = {"query": "q", "source": "journals", "id": "j1", "verdict": "relevant"}
    with pytest.raises(ValueError, match="verdict"):
        await HANDLERS["judge"]({**mark, "verdict": "maybe"})
    with pytest.raises(ValueError, match="section"):
        await HANDLERS["judge"]({**mark, "section": "abstract"})
    with pytest.raises(ValueError, match="needs"):
        await HANDLERS["judge"]({**mark, "id": ""})


async def test_the_handlers_answer_an_admitted_peer_every_source(archive, gateway, monkeypatch):
    gateway.plug("journals", {"j1": doc("Sink", "kitchen sink journal")})
    gateway.plug("wiki", {"p1": doc("Sink", "kitchen sink precedent")})
    await synced(archive, gateway)
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    monkeypatch.setattr(hub_adapter.privacy, "marks", lambda: [])
    result = await HANDLERS["query"]({"query": "kitchen sink", "sources": ["journals", "wiki"]},
                                     "peer:shaula")
    assert {h["source"] for h in every(result)} == {"journals", "wiki"}
    assert {s["name"] for s in (await HANDLERS["sources"]({}, "peer:shaula"))["sources"]} == {"journals", "wiki"}
    status = await HANDLERS["status"]({}, "peer:shaula")
    assert status["sources"] == {"journals": 1, "wiki": 1} and "registry" in status
    got = await HANDLERS["get"]({"source": "wiki", "id": "p1"}, "peer:shaula")
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
