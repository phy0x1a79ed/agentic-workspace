"""Recall reads papers only, and every admitted caller has the full set of modes."""

import pytest

from awm.kb import hub_adapter


@pytest.fixture
def seen(monkeypatch):
    calls = []

    async def fake_recall(body, **kw):
        calls.append(body)
        return {"hits": ["h"]}

    monkeypatch.setattr(hub_adapter.client, "recall", fake_recall)
    return calls


async def recall(as_, **args):
    return await hub_adapter.HANDLERS["recall"]({"query": "q", **args}, as_=as_)


@pytest.mark.parametrize("as_", [None, "peer:home", "user:someone"])
async def test_recall_reads_papers_only_whatever_the_caller_names(seen, as_):
    await recall(as_, sources=["posts", "papers"])
    assert seen[-1]["sources"] == ["papers"]
    await recall(as_)
    assert seen[-1]["sources"] == ["papers"]


@pytest.mark.parametrize("extra", [{"mode": "graph"}, {"mode": "answer"}, {"search_type": "CHUNKS"}])
@pytest.mark.parametrize("as_", [None, "peer:home", "user:someone"])
async def test_any_caller_may_use_graph_answer_and_search_type(seen, as_, extra):
    await recall(as_, **extra)
    assert seen[-1]["query"] == "q"
