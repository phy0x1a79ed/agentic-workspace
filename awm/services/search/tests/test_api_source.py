"""The awm source: this node's tools as one entry per verb, opened as their describe, and the singletons a
relayed caller may not call left out."""

from __future__ import annotations

import pytest
import yaml

from awm.config import edges
from awm.search import api_source, registry
from awm.search.query import parse

from .conftest import HERE, entry, ids, section, synced

pytestmark = [pytest.mark.search]

TOOLS = {
    "journal": {"plugin": "journal", "tier": "on", "verbs": [
        {"verb": "post", "description": "Post a journal entry to a scope.\nIt lands in the inbox.",
         "params": [{"name": "text", "required": True, "description": "The body."},
                    {"name": "tags", "type": "array", "required": False}]},
        {"verb": "inbox", "description": "The messages a scope has not read.", "params": []}]},
    "2fa": {"plugin": "2fa", "tier": "on", "verbs": [
        {"verb": "approve", "description": "Approve a pending login prompt.", "params": []}]},
}


class Table:
    """The gateway's describe over ``TOOLS``: the overview, and one tool's verbs."""

    def __init__(self, tools):
        self.tools = tools
        self.broken: set[str] = set()

    def overview(self, _args):
        return {"node": HERE, "tools": [
            {"tool": t, "plugin": spec["plugin"], "tier": spec["tier"],
             "verbs": [{"verb": v["verb"], "summary": v["description"].splitlines()[0],
                        "required": [p["name"] for p in v["params"] if p.get("required")]}
                       for v in spec["verbs"]]}
            for t, spec in self.tools.items()]}

    def describer(self, tool):
        def describe(args):
            if tool in self.broken:
                return RuntimeError("starting")
            wanted = args.get("verbs")
            verbs = [v for v in self.tools[tool]["verbs"] if wanted is None or v["verb"] in wanted]
            return {"tool": tool, "plugin": self.tools[tool]["plugin"], "verbs": verbs}
        return describe


@pytest.fixture()
def table(gateway):
    t = Table({k: {**v, "verbs": list(v["verbs"])} for k, v in TOOLS.items()})
    gateway.entries = [entry("search", registry.manifest_tables("search")),
                       entry("journal", archive=False), entry("2fa", archive=False)]
    gateway.handlers[("awm", "describe")] = t.overview
    for tool in TOOLS:
        gateway.handlers[(tool, "describe")] = t.describer(tool)
    gateway.handlers[("search", "api_export")] = lambda args: api_source.export(gateway, args)
    return t


@pytest.fixture()
def fleet(tmp_path):
    """2fa is a singleton hosted here that ``home`` may call and ``work`` may not."""
    (tmp_path / "edges.yml").write_text(yaml.safe_dump({
        "nodes": {HERE: {"role": "standby"}, "home": {"role": "homebase"}, "work": {"role": "work"}},
        "edges": {HERE: {"home": "lan", "work": "lan"}},
        "singleton_doors": {HERE: {"port": 2201, "network": "lan"}},
        "singletons": [{"tool": "2fa", "host": HERE, "callers": ["home"]}]}))
    edges._load.cache_clear()


def awm_ids(result):
    return ids(section(result, "exact") + section(result, "related"))


async def test_each_verb_is_one_entry_whose_hit_shows_its_title(archive, gateway, table):
    await synced(archive, gateway)
    result = await archive.query(parse({"query": {"text": {"$contains": "journal entry"}, "awm": {}}}))
    assert result["awm"]["exact"] == [{"id": "journal.post", "title": "journal post: Post a journal entry to a scope."}]
    every = await archive.query(parse({"query": {"awm": {"tool": "journal"}, "text": {"$about": "scope"}}}))
    assert sorted(awm_ids(every)) == ["journal.inbox", "journal.post"]


async def test_params_are_searchable_text(archive, gateway, table):
    await synced(archive, gateway)
    result = await archive.query(parse({"query": {"text": {"$contains": "the body"}, "awm": {}}}))
    assert awm_ids(result) == ["journal.post"]


async def test_get_returns_the_verbs_describe(archive, gateway, table):
    await synced(archive, gateway)
    doc = (await archive.get("awm", "journal.post"))["document"]
    assert doc["tool"] == "journal" and [v["verb"] for v in doc["verbs"]] == ["post"]
    assert doc["verbs"][0]["params"][0]["name"] == "text" and doc["source"] == "awm"
    assert ("journal", "describe", {"verbs": ["post"]}) in gateway.calls
    with pytest.raises(ValueError, match="no document"):
        await archive.get("awm", "journal.nope")


async def test_a_relayed_caller_sees_no_singleton_it_may_not_call(archive, gateway, table, fleet):
    await synced(archive, gateway)
    ask = {"query": {"text": {"$about": "approve login"}, "awm": {}}}
    assert "2fa.approve" in awm_ids(await archive.query(parse(ask)))
    assert "2fa.approve" in awm_ids(await archive.query(parse(ask, "peer:home"), "home"))
    hidden = await archive.query(parse(ask, "peer:work"), "work")
    assert "2fa.approve" not in awm_ids(hidden) and awm_ids(hidden)
    assert (await archive.get("awm", "2fa.approve", caller="home"))["document"]["tool"] == "2fa"
    with pytest.raises(ValueError, match="no document"):
        await archive.get("awm", "2fa.approve", caller="work")


async def test_a_table_change_brings_the_read_forward_and_rebuilds(archive, gateway, table, clock):
    await synced(archive, gateway)
    table.tools["journal"]["verbs"] = table.tools["journal"]["verbs"][:1]
    await archive.refresh()
    assert "awm" not in archive._due_names()
    gateway.entries = [{**e, "id": "id-new"} if e["name"] == "journal" else e for e in gateway.entries]
    await archive.refresh()
    clock.t += 10
    assert archive._due_names() == ["awm"]
    result = await archive.sweep("awm")
    assert result.get("rebuilt") and result["upserted"] == 2
    gone = await archive.query(parse({"query": {"text": {"$contains": "has not read"}, "awm": {}}}))
    assert awm_ids(gone) == []


async def test_a_tool_that_cannot_describe_keeps_its_summaries(gateway, table):
    table.broken.add("journal")
    changes = await api_source.table(gateway)
    post = next(c for c in changes if c["id"] == "journal.post")["entry"]
    assert post["title"] == "journal post: Post a journal entry to a scope." and "- text (required)" in post["text"]


async def test_the_feed_pages_by_offset_and_resets_on_another_epoch(gateway, table):
    first = await api_source.export(gateway, {"limit": 2})
    assert [c["id"] for c in first["changes"]] == ["2fa.approve", "journal.inbox"] and not first["done"]
    rest = await api_source.export(gateway, {"since": first["next"], "limit": 2})
    assert [c["id"] for c in rest["changes"]] == ["journal.post"] and rest["done"]
    assert rest["epoch"] == first["epoch"]
    stale = await api_source.export(gateway, {"since": "other:3"})
    assert stale["reset"] and stale["changes"] == []
