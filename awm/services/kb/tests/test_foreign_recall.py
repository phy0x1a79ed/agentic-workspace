"""Which recall modes a foreign peer may use, and that recall never reads posts."""

import pytest

from awm.kb import hub_adapter

BOOK = {
    "dom": {"relation": "domestic", "grants": []},
    "kbonly": {"relation": "foreign", "grants": ["kb"]},
    "kbj": {"relation": "foreign", "grants": ["kb", "journals"]},
}


@pytest.fixture
def seen(monkeypatch):
    calls = []

    async def fake_recall(body, **kw):
        calls.append(body)
        return {"hits": ["h"]}

    def fake_record(name):
        entry = BOOK.get(name)
        return {"name": name, **entry} if entry else None

    monkeypatch.setattr(hub_adapter.client, "recall", fake_recall)
    monkeypatch.setattr(hub_adapter.config, "peer_record", fake_record)
    monkeypatch.setattr(hub_adapter.config, "peer_relation",
                        lambda n: (fake_record(n) or {}).get("relation"))
    return calls


async def recall(as_, **args):
    return await hub_adapter.HANDLERS["recall"]({"query": "q", **args}, as_=as_)


@pytest.mark.parametrize("as_", [None, "peer:dom", "peer:kbonly", "peer:kbj", "peer:stranger"])
async def test_recall_reads_papers_only_whatever_the_caller_names(seen, as_):
    await recall(as_, sources=["posts", "papers"])
    assert seen[-1]["sources"] == ["papers"]
    await recall(as_)
    assert seen[-1]["sources"] == ["papers"]


@pytest.mark.parametrize("peer", ["peer:kbonly", "peer:kbj", "peer:stranger"])
@pytest.mark.parametrize("extra", [{"mode": "graph"}, {"mode": "answer"}, {"search_type": "CHUNKS"},
                                   {"mode": "hybrid", "search_type": "RAG_COMPLETION"}])
async def test_foreign_refused_graph_answer_and_search_type(seen, peer, extra):
    with pytest.raises(PermissionError):
        await recall(peer, **extra)
    assert seen == []


@pytest.mark.parametrize("mode", ["hybrid", "vector", "lexical"])
async def test_foreign_plain_modes_pass(seen, mode):
    await recall("peer:kbonly", mode=mode)
    assert seen[-1]["mode"] == mode


@pytest.mark.parametrize("extra", [{"mode": "graph"}, {"mode": "answer"}, {"search_type": "CHUNKS"}])
@pytest.mark.parametrize("as_", [None, "peer:dom", "user:someone"])
async def test_domestic_may_use_graph_answer_and_search_type(seen, as_, extra):
    await recall(as_, **extra)
    assert seen[-1]["query"] == "q"


async def test_foreign_status_is_counts_only(monkeypatch):
    async def full():
        return {"counts": {"posts": 3, "papers": 2}, "failures": ["x"], "spend": {"usd": 1},
                "zotero": {"library_json": "/p"}, "allow": None}

    def boom():
        raise AssertionError("child snapshot must not be read for a foreign caller")

    monkeypatch.setattr(hub_adapter.client, "status", full)
    monkeypatch.setattr(hub_adapter.CHILD, "snapshot", boom)
    monkeypatch.setattr(hub_adapter.config, "peer_relation", lambda n: "foreign")
    assert await hub_adapter.HANDLERS["status"]({}, as_="peer:x") == {
        "kb": {"counts": {"posts": 3, "papers": 2}}}


async def test_foreign_status_hides_a_failure(monkeypatch):
    async def down():
        raise RuntimeError("/secret/path refused")

    monkeypatch.setattr(hub_adapter.client, "status", down)
    monkeypatch.setattr(hub_adapter.config, "peer_relation", lambda n: "foreign")
    assert await hub_adapter.HANDLERS["status"]({}, as_="peer:x") == {"kb": {"error": "unavailable"}}
