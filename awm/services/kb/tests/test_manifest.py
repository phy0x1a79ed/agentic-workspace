import tomllib
from pathlib import Path

import pytest

from awm.kb import hub_adapter

OPERATOR = ("start", "stop", "restart", "logs", "sync", "snapshot")


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
    assert seen == {"query": "q", "sources": ["papers"]}


def test_export_is_hidden_and_the_posts_feed_is_gone():
    by_name = {f["name"]: f for f in hub_adapter.API_MANIFEST["functions"]}
    assert by_name["export"]["surfaces"] == ["cli", "http"]
    assert "sweep" not in by_name
    assert not any(p["name"] in ("project", "scope", "kind", "require_complete", "sources")
                   for p in by_name["recall"]["params"])


def test_service_toml_declares_the_kb_archive_source():
    toml = tomllib.loads((Path(hub_adapter.__file__).parents[2] / "service.toml").read_text())
    assert toml["tool"] == "kb" and toml["tier"] == "off"
    assert toml["archive"] == [{"name": "kb", "mode": "index", "export": "kb_export"}]
