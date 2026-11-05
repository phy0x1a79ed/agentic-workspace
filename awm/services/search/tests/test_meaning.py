"""Meaning matches for the archive's own documents from kb's entry index, and the fallback when kb is away."""

from __future__ import annotations

import pytest

from awm.gatewayclient import GatewayCallError
from awm.search import index, meaning
from awm.search.query import parse

from .conftest import doc, every, ids, section, synced

pytestmark = [pytest.mark.search]

SINK = "we decided to refit the kitchen sink and tap"
SHED = "the garden shed roof was replaced"


def entry(source, doc_id, score, text=SINK, part="text", start=0, end=None, zone=None):
    return {"score": score, "gid": 1, "zone": zone or "journal", "source": source, "doc": doc_id, "part": part,
            "start": start, "end": len(text) if end is None else end}


@pytest.fixture()
async def loaded(archive, gateway):
    gateway.plug("journals", {
        "j1": doc("Kitchen refit", SINK, "2026-03-01T10:00:00Z", project="awm", scope="dev", kind="journal"),
        "j2": doc("Garden shed", SHED, "2026-05-01T10:00:00Z", project="home", scope="yard", kind="journal")},
        topic="posts")
    gateway.plug_live("kb", lambda args: {"exact": [], "related": []})
    await synced(archive, gateway)
    return archive


@pytest.fixture()
def kb(gateway, monkeypatch):
    """kb's `related` verb: whatever is in `answer`, or an error; `brute` counts the calls of the archive's own
    embeddings."""
    state = {"answer": [], "error": None, "calls": [], "brute": 0}

    def related(args):
        state["calls"].append(args)
        if state["error"]:
            return state["error"]
        return {"hits": state["answer"], "generation": "gen-0001"}

    gateway.handlers[("kb", "related")] = related
    original = index.related

    def spy(conn, query, allowed, limit):
        state["brute"] += 1
        return original(conn, query, allowed, limit)

    monkeypatch.setattr(index, "related", spy)
    return state


def about(text, **source):
    return {"query": {"text": {"$about": text}, "journals": source}}


async def ranked(archive, text="plumbing overhaul", **source):
    result = await archive.query(parse(about(text, **source)))
    return ids(section(result, "exact")), result


async def test_kb_entries_fuse_with_words_and_the_archives_own_embeddings(loaded, kb):
    kb["answer"] = [entry("journals", "j1", 0.9, start=3, end=20)]
    found, result = await ranked(loaded)
    assert found[0] == "j1" and result["journals"].keys() == {"exact"}
    assert all(set(h) == {"id", "title", "ts"} for h in every(result))
    assert kb["brute"] == 1


async def test_the_request_names_the_source_and_its_zone(loaded, kb):
    await ranked(loaded)
    args = kb["calls"][0]
    assert args["text"] == "plumbing overhaul" and args["sources"] == ["journals"]
    assert args["zones"] == ["journal"] and args["k"] >= 24


async def test_a_filtered_or_text_matched_query_asks_kb_for_more_entries(loaded, kb):
    await ranked(loaded, project="awm")
    await loaded.query(parse({"query": {"text": {"$and": [{"$about": "plumbing"}, {"$contains": "sink"}]}}}))
    assert [c["k"] for c in kb["calls"]] == [meaning.ENTRIES_FILTERED] * 2


async def test_a_document_the_archive_no_longer_holds_is_dropped(loaded, kb):
    kb["answer"] = [entry("journals", "gone", 0.99), entry("journals", "j2", 0.5, text=SHED)]
    found, _ = await ranked(loaded)
    assert found[0] == "j2" and "gone" not in found


async def test_filters_apply_to_the_resolved_documents(loaded, kb):
    kb["answer"] = [entry("journals", "j1", 0.9), entry("journals", "j2", 0.8, text=SHED)]
    found, _ = await ranked(loaded, project="home")
    assert found == ["j2"]
    found, _ = await ranked(loaded, ts={"$lt": "2026-04-01"})
    assert found == ["j1"]


async def test_a_span_past_the_end_of_an_edited_document_keeps_the_hit(loaded, kb):
    kb["answer"] = [entry("journals", "j2", 0.9, text=SHED, start=900, end=950)]
    found, _ = await ranked(loaded)
    assert found[0] == "j2"


async def test_each_document_is_one_hit_at_its_best_entry(loaded, kb):
    kb["answer"] = [entry("journals", "j1", 0.7, start=0, end=10), entry("journals", "j1", 0.9, start=11, end=30),
                    entry("journals", "j2", 0.8, text=SHED)]
    found, _ = await ranked(loaded)
    assert found == ["j1", "j2"]


async def test_a_document_holding_some_of_the_words_joins_the_meaning_matches(loaded, kb):
    kb["answer"] = [entry("journals", "j2", 0.9, text=SHED)]
    found, _ = await ranked(loaded, "who replaced the leaking tap")
    assert set(found) >= {"j1", "j2"}
    assert kb["brute"] == 1


async def test_the_archives_own_head_survives_fusion(loaded, kb):
    kb["answer"] = [entry("journals", "j2", 0.9, text=SHED)]
    found, _ = await ranked(loaded, "kitchen sink refit")
    assert found[0] == "j1"


async def test_a_word_from_the_documents_scope_counts_beside_its_words(loaded, kb):
    result = await loaded.query(parse({"query": {"text": {"$about": "yard roof"}, "journals": {}}}))
    assert ids(result["journals"]["exact"])[0] == "j2"


@pytest.mark.parametrize("failure", [GatewayCallError(503, '{"detail":"encoder is loading"}'), KeyError("x")])
async def test_when_kb_cannot_answer_the_archive_ranks_by_its_own_embeddings(loaded, kb, gateway, failure):
    if isinstance(failure, KeyError):
        del gateway.handlers[("kb", "related")]
    else:
        kb["error"] = failure
    found, result = await ranked(loaded, "kitchen sink refit")
    assert kb["brute"] == 1 and found[0] == "j1" and "error" not in result["journals"]


async def test_without_kb_registered_no_call_is_made(archive, gateway, kb):
    gateway.plug("journals", {"j1": doc("Kitchen refit", SINK, "2026-03-01T10:00:00Z")})
    await synced(archive, gateway)
    result = await archive.query(parse(about("kitchen sink refit")))
    assert kb["calls"] == [] and kb["brute"] == 1
    assert ids(result["journals"]["exact"]) == ["j1"]
