"""Meaning matches for the archive's own documents from kb's entry index, and the fallback when kb is away."""

from __future__ import annotations

import pytest

from awm.gatewayclient import GatewayCallError
from awm.search import index
from awm.search.query import parse

from .conftest import doc, every, flat, synced

pytestmark = [pytest.mark.search]

SINK = "we decided to refit the kitchen sink and tap"
SHED = "the garden shed roof was replaced"


def entry(source, doc_id, score, text=SINK, part="text", start=0, end=None, zone=None):
    zones = {"journals": "journal", "precedence": "precedent", "notes": "note"}
    return {"score": score, "gid": 1, "zone": zone or zones[source], "source": source, "doc": doc_id, "part": part,
            "start": start, "end": len(text) if end is None else end}


@pytest.fixture()
async def loaded(archive, gateway):
    gateway.plug("journals", {
        "j1": doc("Kitchen refit", SINK, "2026-03-01T10:00:00Z", project="awm", scope="dev", kind="journal"),
        "j2": doc("Garden shed", SHED, "2026-05-01T10:00:00Z", project="home", scope="yard", kind="journal")},
        topic="posts")
    gateway.plug("precedence", {"p1": doc("Sink choice", "the user prefers a deep kitchen sink",
                                          "2026-02-01T00:00:00Z", kind="decision")})
    gateway.plug_live("kb", lambda args: {"exact": [], "related": []})
    await synced(archive, gateway)
    return archive


@pytest.fixture()
def kb(gateway, monkeypatch):
    """kb's `related` verb: whatever is in `answer`, or an error; the archive's embeddings must stay unused."""
    state = {"answer": [], "error": None, "calls": []}

    def related(args):
        state["calls"].append(args)
        if state["error"]:
            return state["error"]
        return {"hits": state["answer"], "generation": "gen-0001"}

    gateway.handlers[("kb", "related")] = related
    state["brute"] = []
    original = index.related

    def spy(conn, query, filters, limit):
        state["brute"].append(list(filters.sources))
        return original(conn, query, filters, limit)

    monkeypatch.setattr(index, "related", spy)
    return state


async def related_ids(archive, **args):
    result = await archive.query(parse({"query": "tap replacement", **args}))
    return [(h["source"], h["id"]) for h in flat(result["related"])], result


async def test_kb_entries_become_related_hits_with_their_spans(loaded, kb):
    kb["answer"] = [entry("journals", "j1", 0.9, start=3, end=20), entry("precedence", "p1", 0.8, part="title",
                                                                       text="Sink choice")]
    ids, result = await related_ids(loaded, sources=["journals", "precedence"])
    assert ids == [("journals", "j1"), ("precedence", "p1")]
    first = flat(result["related"])[0]
    assert first["match"] == "meaning" and first["passages"][0]["text"] == SINK[3:20]
    assert (first["passages"][0]["field"], first["passages"][0]["start"], first["passages"][0]["end"]) == ("text", 3, 20)
    assert flat(result["related"])[1]["passages"][0]["field"] == "title"
    assert kb["brute"] == []
    assert "degraded" not in result


async def test_the_request_names_the_sources_and_their_zones(loaded, kb):
    await related_ids(loaded, sources=["journals", "precedence"], limit=4)
    args = kb["calls"][0]
    assert args["text"] == "tap replacement" and args["sources"] == ["journals", "precedence"]
    assert args["zones"] == ["journal", "precedent"] and args["k"] >= 24
    await related_ids(loaded, sources=["journals"], project="awm")
    assert kb["calls"][1]["k"] == 200 and kb["calls"][1]["zones"] == ["journal"]


async def test_a_document_the_archive_no_longer_holds_is_dropped(loaded, kb):
    kb["answer"] = [entry("journals", "gone", 0.99), entry("journals", "j2", 0.5, text=SHED)]
    ids, _ = await related_ids(loaded, sources=["journals"])
    assert ids == [("journals", "j2")]


async def test_filters_apply_to_the_resolved_documents(loaded, kb):
    kb["answer"] = [entry("journals", "j1", 0.9), entry("journals", "j2", 0.8, text=SHED)]
    ids, _ = await related_ids(loaded, sources=["journals"], project="home")
    assert ids == [("journals", "j2")]
    ids, _ = await related_ids(loaded, sources=["journals"], before="2026-04-01")
    assert ids == [("journals", "j1")]


async def test_a_span_past_the_end_of_an_edited_document_quotes_nothing_and_the_hit_stays(loaded, kb):
    kb["answer"] = [entry("journals", "j2", 0.9, text=SHED, start=900, end=950)]
    ids, result = await related_ids(loaded, sources=["journals"])
    assert ids == [("journals", "j2")] and flat(result["related"])[0]["passages"] == []


async def test_each_document_is_one_hit_quoting_its_best_entries_first(loaded, kb):
    kb["answer"] = [entry("journals", "j1", 0.7, start=0, end=10), entry("journals", "j1", 0.9, start=11, end=30),
                    entry("journals", "j2", 0.8, text=SHED)]
    ids, result = await related_ids(loaded, sources=["journals"], passages=2)
    assert ids == [("journals", "j1"), ("journals", "j2")]
    assert [p["start"] for p in flat(result["related"])[0]["passages"]] == [11, 0]


@pytest.mark.parametrize("failure", [GatewayCallError(503, '{"detail":"encoder is loading"}'), KeyError("x")])
async def test_when_kb_cannot_answer_the_archive_ranks_by_its_own_embeddings(loaded, kb, gateway, failure):
    if isinstance(failure, KeyError):
        del gateway.handlers[("kb", "related")]
    else:
        kb["error"] = failure
    _, result = await related_ids(loaded, query="kitchen sink refit", sources=["journals"])
    assert kb["brute"] == [["journals"]]
    assert ("journals", "j1") in [(h["source"], h["id"]) for h in every(result)]


async def test_sources_kb_does_not_hold_keep_the_archives_embeddings(loaded, kb, gateway):
    gateway.plug("extra", {"x1": doc("Tap washers", "replacing the tap washer", "2026-03-02T00:00:00Z")})
    await synced(loaded, gateway)
    kb["answer"] = [entry("journals", "j1", 0.9)]
    ids, result = await related_ids(loaded, query="tap washer", sources=["journals", "extra"])
    assert ("journals", "j1") in ids
    assert ("extra", "x1") in [(h["source"], h["id"]) for h in every(result)]
    assert kb["brute"] == [["extra"]]
    assert kb["calls"][0]["sources"] == ["journals"]


async def test_without_kb_registered_no_call_is_made(archive, gateway, kb):
    gateway.plug("journals", {"j1": doc("Kitchen refit", SINK, "2026-03-01T10:00:00Z")})
    await synced(archive, gateway)
    result = await archive.query(parse({"query": "kitchen sink refit"}))
    assert kb["calls"] == [] and kb["brute"] == [["journals"]]
    assert [h["id"] for h in every(result)] == ["j1"]


async def test_kb_papers_keep_the_degraded_note_the_live_source_sends(archive, gateway):
    gateway.plug_live("kb", lambda args: {"exact": [], "related": [{"id": "p", "title": "T", "passages": []}],
                                          "graph": [], "degraded": {"semantic": "keyword only: encoder is loading"}})
    result = await archive.query(parse({"query": "anything"}))
    assert result["sources"]["kb"]["degraded"] == {"semantic": "keyword only: encoder is loading"}
    assert [h["id"] for h in flat(result["related"])] == ["p"]
