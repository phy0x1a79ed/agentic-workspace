import tomllib
from pathlib import Path

import pytest

from awm.kb import hub_adapter

OPERATOR = ("start", "stop", "restart", "logs")


def test_every_function_is_a_kb_verb_with_a_handler():
    names = [f["name"] for f in hub_adapter.API_MANIFEST["functions"]]
    assert set(names) == set(hub_adapter.HANDLERS)
    for f in hub_adapter.API_MANIFEST["functions"]:
        assert f["tool"] == f"kb_{f['name']}"
    assert hub_adapter.API_MANIFEST["description"]


@pytest.mark.parametrize("verb", OPERATOR)
async def test_operator_verbs_refuse_an_edge_caller(verb):
    with pytest.raises(PermissionError):
        await hub_adapter.HANDLERS[verb]({}, as_="user:someone")


def test_the_retired_verbs_are_gone():
    by_name = {f["name"]: f for f in hub_adapter.API_MANIFEST["functions"]}
    assert not {"sweep", "export", "graph", "schema", "sync", "recall", "snapshot", "rank"} & set(by_name)
    assert not {"recall", "snapshot", "rank"} & set(hub_adapter.HANDLERS)


def test_kb_publishes_no_mcp_tool():
    for f in hub_adapter.API_MANIFEST["functions"]:
        assert f.get("surfaces") == ["cli", "http"], f["name"]
    toml = tomllib.loads((Path(hub_adapter.__file__).parents[2] / "awm-plugin.toml").read_text())
    assert "tool" not in toml


def test_the_query_takes_no_kind():
    by_name = {f["name"]: f for f in hub_adapter.API_MANIFEST["functions"]}
    assert "kind" not in {p["name"] for p in by_name["query"]["params"]}
    assert "kind" not in {p["name"] for p in by_name["library"]["params"]}


def test_the_plugin_manifest_declares_the_kb_and_library_sources_and_their_hop():
    toml = tomllib.loads((Path(hub_adapter.__file__).parents[2] / "awm-plugin.toml").read_text())
    [service] = toml["service"]
    links = ["citations", "cited_by", "authors"]
    assert service["archive"] == [
        {"name": "kb", "mode": "live", "query": "kb_query", "get": "kb_get", "hop": "kb_hop", "links": links},
        {"name": "library", "mode": "live", "query": "kb_library", "get": "kb_get", "hop": "kb_hop",
         "links": links}]
