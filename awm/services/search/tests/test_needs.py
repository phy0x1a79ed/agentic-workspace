"""search's ``[[needs.tool]]`` against the gateway calls it really makes (plugin contract v1.10, R116 item 3).

The gateway refuses a service's call to a verb its manifest does not list, so a missing verb fails only at run
time. This drives every path that calls the gateway, against the archive tables the real journal, kb and
transcripts manifests declare, and compares the calls made with the verbs listed, both ways.
"""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from typing import Any

import pytest

from awm.search import hub_adapter
from awm.search.hub_adapter import HANDLERS

from .conftest import Exporter, doc

pytestmark = [pytest.mark.search]

SERVICES = Path(hub_adapter.__file__).resolve().parents[3]
#: The plugins search reads, by folder.
READ = ("journal", "kb", "transcripts")


def manifest(folder: str) -> dict[str, Any]:
    return tomllib.loads((SERVICES / folder / "awm-plugin.toml").read_text())


def entry(folder: str, extra: tuple[str, ...] = ()) -> dict[str, Any]:
    """The registry entry the gateway lists for a plugin: its archive tables, and a function for each verb they
    name plus ``extra``."""
    m = manifest(folder)
    [service] = m["service"]
    tool = m["tool"][0]["name"]
    tables = service.get("archive", [])
    published = {t[k] for t in tables for k in ("export", "query", "get", "hop") if t.get(k)}
    published |= {f"{tool}_{v}" for v in extra}
    functions = [{"name": p.split("_", 1)[1], "tool": p} for p in sorted(published)]
    return {"name": service["name"], "kind": "service", "backend_status": "ready", "archive": tables,
            "api": {"functions": functions}}


def declared() -> set[tuple[str, str]]:
    m = manifest("search")
    return {(d["name"], v) for d in m["needs"]["tool"] for v in d["verbs"]}


@pytest.fixture()
async def replayed(archive, gateway, monkeypatch):
    gateway.entries = [entry("journal", ("note_read", "tag_list")), entry("kb", ("related",)), entry("transcripts")]
    page = Exporter({"j1": doc("Kitchen", "kitchen sink journal", tags=["journal"]),
                     "j2": doc("Reply", "kitchen reply", tags=["journal"], reply_to=["j1"])})
    paper = {"exact": [{"id": "doi:1", "title": "Kitchen taps"}], "related": []}
    gateway.handlers.update({
        ("journal", "export"): page,
        ("journal", "note_read"): lambda args: {"ok": True},
        ("journal", "tag_list"): lambda args: {"tags": [], "clashes": []},
        ("kb", "query"): lambda args: paper,
        ("kb", "library"): lambda args: paper,
        ("kb", "get"): lambda args: {"document": {"title": "Kitchen taps", "text": "whole"}},
        ("kb", "hop"): lambda args: {"hits": [{"id": "doi:2", "title": "Cited"}]},
        ("kb", "related"): lambda args: {"hits": []},
        ("transcripts", "query"): lambda args: {"hits": [{"id": "s1:1", "title": "kitchen"}]},
    })
    monkeypatch.setattr(hub_adapter, "ARCHIVE", archive)
    await archive.refresh()
    for name in archive._due_names():
        await archive.sweep(name)
    await HANDLERS["query"]({"query": "kitchen", "sources": ["journals", "kb", "library", "transcripts"],
                             "hop": {"via": "citations"}}, None)
    await HANDLERS["query"]({"query": "kitchen", "sources": ["journals"], "hop": {"via": "reply_to"}}, "peer:altair")
    await HANDLERS["get"]({"source": "kb", "id": "doi:1"}, None)
    await HANDLERS["get"]({"source": "journals", "id": "j1", "_caller_pid": 4242}, None)
    await asyncio.gather(*hub_adapter._NOTING)
    return {(tool, verb) for tool, verb, _ in gateway.calls}


async def test_every_gateway_call_search_makes_is_listed_in_its_needs(replayed):
    assert replayed - declared() == set()


async def test_every_verb_its_needs_list_is_one_it_calls(replayed):
    assert declared() - replayed == set()


def test_each_tool_it_needs_is_one_a_plugin_declares():
    served = {t["name"] for folder in READ for t in manifest(folder).get("tool", [])}
    assert {tool for tool, _ in declared()} <= served
