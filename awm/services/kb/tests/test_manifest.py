import pytest

from awm.kb import hub_adapter

OPERATOR = ("start", "stop", "restart", "logs", "sweep", "sync", "snapshot")


def test_every_function_is_a_kb_tool_with_a_handler():
    names = [f["name"] for f in hub_adapter.API_MANIFEST["functions"]]
    assert set(names) == set(hub_adapter.HANDLERS)
    for f in hub_adapter.API_MANIFEST["functions"]:
        assert f["tool"] == f"kb_{f['name']}"
    assert hub_adapter.API_MANIFEST["description"]


@pytest.mark.parametrize("verb", OPERATOR)
async def test_operator_verbs_refuse_an_edge_caller(verb):
    with pytest.raises(PermissionError):
        await hub_adapter.HANDLERS[verb]({}, as_="user:someone")


async def test_recall_passes_only_set_fields(monkeypatch):
    seen = {}

    async def fake_recall(body, **kw):
        seen.update(body)
        return {"hits": []}

    monkeypatch.setattr(hub_adapter.client, "recall", fake_recall)
    await hub_adapter.HANDLERS["recall"]({"query": "q", "sources": ["posts"], "mode": None, "verb": "recall"},
                                         as_="user:someone")
    assert seen == {"query": "q", "sources": ["posts"]}
