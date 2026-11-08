import tomllib
from pathlib import Path

import pytest

from awm.kb import hub_adapter

OPERATOR = ("start", "stop", "restart", "logs")


@pytest.mark.parametrize("verb", OPERATOR)
async def test_operator_verbs_refuse_an_edge_caller(verb):
    with pytest.raises(PermissionError):
        await hub_adapter.HANDLERS[verb]({}, as_="user:someone")


def test_kb_publishes_no_mcp_tool():
    for f in hub_adapter.API_MANIFEST["functions"]:
        assert f.get("surfaces") == ["cli", "http"], f["name"]
    toml = tomllib.loads((Path(hub_adapter.__file__).parents[2] / "awm-plugin.toml").read_text())
    assert toml["tool"] == [{"name": "kb", "major": 1, "tier": "off"}]


def test_the_plugin_manifest_declares_the_kb_and_library_sources():
    toml = tomllib.loads((Path(hub_adapter.__file__).parents[2] / "awm-plugin.toml").read_text())
    [service] = toml["service"]
    kb, library = service["archive"]
    assert {k: kb[k] for k in ("name", "mode", "query", "get", "hit")} == {
        "name": "kb", "mode": "live", "query": "kb_query", "get": "kb_get", "hit": ["title", "ts"]}
    assert kb["fields"] == {"title": "content", "text": "content", "ts": "datetime", "author": "categorical",
                            "tag": "categorical", "cites": "categorical", "cited_by": "categorical"}
    assert library == {"name": "library", "mode": "live", "query": "kb_library", "get": "kb_get"}
