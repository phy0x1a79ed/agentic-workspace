"""Filling the index: export paging, vanished ids, the frozen rule, push and a plug-in arriving mid-run."""

from __future__ import annotations

import asyncio

import pytest

from awm.search import archive as archive_mod, store
from awm.search.query import parse

from .conftest import doc, synced

pytestmark = [pytest.mark.search]

JOURNALS = {f"j{i}": doc(f"Entry {i}", f"journal entry {i} about the kitchen sink", project="awm", scope="dev",
                         kind="journal") for i in range(5)}


def count(name):
    conn = store.connect()
    try:
        return len(store.document_ids(conn, name))
    finally:
        conn.close()


async def test_a_first_pass_pages_every_document_into_the_index(archive, gateway, monkeypatch):
    monkeypatch.setattr(archive_mod, "PAGE", 2)
    exporter = gateway.plug("journals", dict(JOURNALS), topic="posts")
    await synced(archive, gateway)
    assert count("journals") == 5 and exporter.calls == 3
    sources = (await archive.sources_view())["sources"]
    assert sources[0]["count"] == 5 and sources[0]["last_sync"] and not sources[0]["frozen"]
    assert sources[0]["last_error"] is None and sources[0]["topic"] == "posts"


async def test_cursors_are_the_last_key_sent(archive, gateway, monkeypatch):
    monkeypatch.setattr(archive_mod, "PAGE", 2)
    gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    cursors = [args.get("cursor") for _, _, args in gateway.calls]
    assert cursors == [None, "j1", "j3"]


async def test_a_vanished_id_is_dropped_and_an_edit_is_reindexed(archive, gateway):
    exporter = gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    del exporter.docs["j0"]
    exporter.docs["j1"] = doc("Entry 1", "now about the garden shed", kind="journal")
    result = await archive.sweep("journals")
    assert result["dropped"] == 1
    assert count("journals") == 4
    kitchen = await archive.query(parse({"query": "kitchen sink"}))
    assert "j0" not in [h["id"] for h in kitchen["hits"]]
    assert kitchen["hits"][-1]["id"] == "j1"
    shed = await archive.query(parse({"query": "garden shed"}))
    assert shed["hits"][0]["id"] == "j1"


async def test_a_failing_export_changes_nothing_and_is_retried(archive, gateway, clock, monkeypatch):
    monkeypatch.setattr(archive_mod, "PAGE", 2)
    exporter = gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    exporter.fail = RuntimeError("store unreadable")
    exporter.docs.clear()
    result = await archive.sweep("journals")
    assert "store unreadable" in result["error"]
    assert count("journals") == 5
    row = (await archive.sources_view())["sources"][0]
    assert "store unreadable" in row["last_error"]
    assert archive._due["journals"] == clock() + archive_mod.RETRY_S


async def test_a_pass_that_dies_between_pages_drops_nothing(archive, gateway, monkeypatch):
    monkeypatch.setattr(archive_mod, "PAGE", 2)
    exporter = gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    inner, seen = exporter.__call__, []

    def flaky(args):
        seen.append(args.get("cursor"))
        if args.get("cursor"):
            raise RuntimeError("went away")
        return inner(args)

    gateway.handlers[("journals", "export")] = flaky
    exporter.docs.pop("j4")
    assert "error" in await archive.sweep("journals")
    assert count("journals") == 5


@pytest.mark.parametrize("page, message", [
    ({"documents": [], "next_cursor": None}, "documents and done"),
    ({"documents": [], "next_cursor": None, "done": True}, "no ids"),
    ({"documents": [], "next_cursor": "same", "done": False}, "does not advance"),
])
async def test_a_malformed_page_fails_the_pass(archive, gateway, page, message):
    gateway.plug("journals", dict(JOURNALS))
    gateway.handlers[("journals", "export")] = lambda args: {**page, "next_cursor": args.get("cursor") or page["next_cursor"]}
    await archive.refresh()
    assert message in (await archive.sweep("journals"))["error"]


async def test_a_stopped_service_freezes_its_source_and_keeps_its_documents(archive, gateway):
    gateway.plug("writing", {"w1": doc("Essay", "a long essay on rivers")})
    await synced(archive, gateway)
    gateway.stop("writing")
    await archive.refresh()
    (row,) = (await archive.sources_view())["sources"]
    assert row["frozen"] is True and row["count"] == 1
    assert count("writing") == 1
    with pytest.raises(ValueError, match="not a running index source"):
        await archive.sweep("writing")


async def test_an_unreadable_registry_freezes_nothing(archive, gateway):
    gateway.plug("writing", {"w1": doc("Essay", "a long essay on rivers")})
    await synced(archive, gateway)
    gateway.error = ConnectionError("gateway down")
    assert await archive.refresh() is False
    (row,) = (await archive.sources_view())["sources"]
    assert row["frozen"] is False and "gateway down" in archive.registry_error


async def test_a_frozen_source_that_returns_is_swept_again(archive, gateway):
    exporter = gateway.plug("writing", {"w1": doc("Essay", "a long essay on rivers")})
    await synced(archive, gateway)
    gateway.stop("writing")
    await archive.refresh()
    exporter.docs["w2"] = doc("Poem", "a short poem on lakes")
    gateway.plug("writing", exporter.docs)
    gateway.handlers[("writing", "export")] = exporter
    await synced(archive, gateway)
    assert count("writing") == 2


async def test_a_plugin_appearing_mid_run_joins_with_no_code_change(archive, gateway):
    gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    assert [s["name"] for s in (await archive.sources_view())["sources"]] == ["journals"]
    gateway.plug("precedence", {"p1": doc("Tabs or spaces", "the user prefers tabs")})
    await synced(archive, gateway)
    names = {s["name"]: s["count"] for s in (await archive.sources_view())["sources"]}
    assert names == {"journals": 5, "precedence": 1}


async def test_a_pushed_event_brings_the_sweep_forward(archive, gateway, clock):
    gateway.plug("journals", dict(JOURNALS), topic="posts")
    await synced(archive, gateway)
    assert archive._due["journals"] == clock() + archive_mod.SWEEP_EVERY_S
    gateway.fire("journals", "posts")
    for _ in range(50):
        if archive._due["journals"] <= clock() + archive_mod.SETTLE_S:
            break
        await asyncio.sleep(0.02)
    assert archive._due["journals"] == clock() + archive_mod.SETTLE_S


async def test_an_event_during_a_pass_asks_for_another(archive, gateway, clock):
    exporter = gateway.plug("journals", dict(JOURNALS), topic="posts")
    await archive.refresh()

    def export_and_hear_a_post(args):
        archive._pushed(("journals", "posts"))
        return exporter(args)

    gateway.handlers[("journals", "export")] = export_and_hear_a_post
    await archive.sweep("journals")
    assert archive._due["journals"] == clock() + archive_mod.SETTLE_S


async def test_a_source_whose_table_is_unusable_is_listed_but_never_swept(archive, gateway):
    gateway.entries.append({"name": "odd", "kind": "service", "backend_status": "ready", "api": {"functions": []},
                            "archive": [{"name": "odd", "mode": "index", "export": "odd_export"}]})
    await archive.refresh()
    assert archive._due_names() == []
    (row,) = (await archive.sources_view())["sources"]
    assert "does not publish" in row["last_error"]


async def test_the_sync_loop_sweeps_what_is_due(archive, gateway, monkeypatch):
    monkeypatch.setattr(archive_mod, "TICK_S", 0.05)
    gateway.plug("journals", dict(JOURNALS))
    task = asyncio.create_task(archive.run())
    for _ in range(100):
        if count("journals") == 5:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    assert count("journals") == 5


async def test_a_document_with_no_content_is_left_out_and_one_that_empties_is_dropped(archive, gateway):
    exporter = gateway.plug("notes", {"n1": doc("Title", "body text here"), "n2": doc("", "")})
    await synced(archive, gateway)
    assert count("notes") == 1
    exporter.docs["n1"] = doc("", "")
    await archive.sweep("notes")
    assert count("notes") == 0
