"""Filling the store: feed paging and cursors, edits and deletions, rebuilds, the frozen rule, push and a
plug-in arriving mid-run."""

from __future__ import annotations

import asyncio

import pytest

from awm.search import archive as archive_mod, store
from awm.search.query import parse

from .conftest import FIELDS, doc, ids, section, synced

pytestmark = [pytest.mark.search]

JOURNALS = {f"j{i}": doc(f"Entry {i}", f"journal entry {i} about the kitchen sink", project="awm", scope="dev")
            for i in range(5)}


def count(archive, name):
    held = archive.lance.has(name) and archive.lance.tables(name, *_declared(archive, name))
    return held.count() if held else 0


def _declared(archive, name):
    source = archive.sources[name]
    return source.fields, source.hit


def state(name):
    conn = store.connect()
    try:
        return store.feed_state(conn, name)
    finally:
        conn.close()


async def test_a_first_read_pages_every_entry_into_the_store(archive, gateway, monkeypatch):
    monkeypatch.setattr(archive_mod, "PAGE", 2)
    feed = gateway.plug("journals", dict(JOURNALS), topic="posts")
    result = await synced(archive, gateway)
    assert count(archive, "journals") == 5 and feed.calls == 3
    sources = (await archive.sources_view())["sources"]
    assert sources[0]["count"] == 5 and sources[0]["last_sync"] and not sources[0]["frozen"]
    assert sources[0]["last_error"] is None and sources[0]["topic"] == "posts"
    assert result.sources["journals"].fields.keys() == FIELDS.keys()


async def test_each_read_starts_at_the_cursor_the_last_one_stored(archive, gateway, monkeypatch):
    monkeypatch.setattr(archive_mod, "PAGE", 2)
    feed = gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    assert [args.get("since") for _, _, args in gateway.calls] == [None, "2:j1", "4:j3"]
    assert state("journals")["cursor"] == "5:j4" and state("journals")["epoch"] == feed.epoch
    gateway.calls.clear()
    result = await archive.sweep("journals")
    assert [args.get("since") for _, _, args in gateway.calls] == ["5:j4"]
    assert result["upserted"] == 0 and "merged" not in result


async def test_a_deletion_drops_the_entry_and_an_edit_replaces_it(archive, gateway):
    feed = gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    del feed.docs["j0"]
    feed.docs["j1"] = doc("Entry 1", "now about the garden shed")
    result = await archive.sweep("journals")
    assert result["deleted"] == 1 and result["upserted"] == 1 and result["pages"] == 1
    assert count(archive, "journals") == 4
    kitchen = await archive.query(parse({"query": "kitchen sink"}))
    assert "j0" not in ids(section(kitchen, "exact") + section(kitchen, "related"))
    assert "j1" not in ids(section(kitchen, "exact"))
    shed = await archive.query(parse({"query": "garden shed"}))
    assert ids(section(shed, "exact")) == ["j1"]


async def test_a_retag_embeds_nothing(archive, gateway, stub_embedder):
    feed = gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    before = stub_embedder.encoded
    feed.docs["j2"] = {**JOURNALS["j2"], "tag": ["cache"]}
    assert (await archive.sweep("journals"))["embedded"] == 0
    assert stub_embedder.encoded == before
    found = await archive.query(parse({"query": {"journals": {"tag": "cache"}}}))
    assert ids(section(found, "exact")) == ["j2"]


async def test_a_failing_export_changes_nothing_and_is_retried(archive, gateway, clock, monkeypatch):
    monkeypatch.setattr(archive_mod, "PAGE", 2)
    feed = gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    feed.fail = RuntimeError("store unreadable")
    feed.docs.clear()
    result = await archive.sweep("journals")
    assert "store unreadable" in result["error"]
    assert count(archive, "journals") == 5
    row = (await archive.sources_view())["sources"][0]
    assert "store unreadable" in row["last_error"]
    assert archive._due["journals"] == clock() + archive_mod.RETRY_S


async def test_a_read_that_dies_between_pages_keeps_what_it_wrote_and_resumes_there(archive, gateway, monkeypatch):
    monkeypatch.setattr(archive_mod, "PAGE", 2)
    feed = gateway.plug("journals", dict(JOURNALS))
    await archive.refresh()

    def flaky(args):
        if args.get("since"):
            raise RuntimeError("went away")
        return feed(args)

    gateway.handlers[("journals", "export")] = flaky
    assert "error" in await archive.sweep("journals")
    assert count(archive, "journals") == 2 and state("journals")["cursor"] == "2:j1"
    gateway.handlers[("journals", "export")] = feed
    gateway.calls.clear()
    await archive.sweep("journals")
    assert gateway.calls[0][2]["since"] == "2:j1" and count(archive, "journals") == 5


async def test_a_changed_epoch_rebuilds_the_source_from_the_start(archive, gateway):
    feed = gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    feed.epoch = "epoch-2"
    feed.docs.pop("j0")
    result = await archive.sweep("journals")
    assert result["rebuilt"] is True and count(archive, "journals") == 4
    assert state("journals")["epoch"] == "epoch-2"


async def test_a_reset_rebuilds_the_source_from_the_start(archive, gateway):
    feed = gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    with store.connect() as conn:
        conn.execute("UPDATE sources SET cursor = '99:' WHERE name = 'journals'")
    result = await archive.sweep("journals")
    assert result["rebuilt"] is True and count(archive, "journals") == 5
    assert feed.calls == 3


async def test_a_changed_declaration_rebuilds_the_source(archive, gateway):
    feed = gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    gateway.plug("journals", feed.docs, fields={**FIELDS, "kind": "categorical"})
    gateway.handlers[("journals", "export")] = feed
    await archive.refresh()
    result = await archive.sweep("journals")
    assert result["rebuilt"] is True and count(archive, "journals") == 5


async def test_reindex_rebuilds_from_the_start(archive, gateway):
    gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    assert await archive.reindex("journals") == {"rebuild": ["journals"]}
    assert state("journals") == {"cursor": None, "epoch": None, "layout": None}
    assert (await archive.sweep("journals"))["rebuilt"] is True
    with pytest.raises(ValueError, match="not a running index source"):
        await archive.reindex("nope")


@pytest.mark.parametrize("page, message", [
    ({"changes": [], "next": None}, "changes, done and epoch"),
    ({"changes": [], "done": False, "epoch": "e"}, "does not advance"),
])
async def test_a_malformed_page_fails_the_read(archive, gateway, page, message):
    gateway.plug("journals", dict(JOURNALS))
    gateway.handlers[("journals", "export")] = lambda args: {**page, "next": args.get("since")}
    await archive.refresh()
    assert message in (await archive.sweep("journals"))["error"]


async def test_a_stopped_service_freezes_its_source_and_keeps_its_entries(archive, gateway):
    gateway.plug("writing", {"w1": doc("Essay", "a long essay on rivers")})
    await synced(archive, gateway)
    gateway.stop("writing")
    await archive.refresh()
    (row,) = (await archive.sources_view())["sources"]
    assert row["frozen"] is True and row["count"] == 1
    got = (await archive.get("writing", "w1"))["document"]
    assert got["frozen"] is True and got["text"] == "a long essay on rivers"
    with pytest.raises(ValueError, match="not a running index source"):
        await archive.sweep("writing")


async def test_an_unreadable_registry_freezes_nothing(archive, gateway):
    gateway.plug("writing", {"w1": doc("Essay", "a long essay on rivers")})
    await synced(archive, gateway)
    gateway.error = ConnectionError("gateway down")
    assert await archive.refresh() is False
    (row,) = (await archive.sources_view())["sources"]
    assert row["frozen"] is False and "gateway down" in archive.registry_error


async def test_a_frozen_source_that_returns_is_read_again(archive, gateway):
    feed = gateway.plug("writing", {"w1": doc("Essay", "a long essay on rivers")})
    await synced(archive, gateway)
    gateway.stop("writing")
    await archive.refresh()
    feed.docs["w2"] = doc("Poem", "a short poem on lakes")
    gateway.plug("writing", feed.docs)
    gateway.handlers[("writing", "export")] = feed
    await synced(archive, gateway)
    assert count(archive, "writing") == 2


async def test_a_source_that_turns_live_loses_its_tables(archive, gateway):
    gateway.plug("writing", {"w1": doc("Essay", "a long essay on rivers")})
    await synced(archive, gateway)
    gateway.stop("writing")
    gateway.plug_live("writing", lambda args: {"hits": []})
    await archive.refresh()
    assert not archive.lance.has("writing") and state("writing")["layout"] is None


async def test_a_plugin_appearing_mid_run_joins_with_no_code_change(archive, gateway):
    gateway.plug("journals", dict(JOURNALS))
    await synced(archive, gateway)
    assert [s["name"] for s in (await archive.sources_view())["sources"]] == ["journals"]
    gateway.plug("recipes", {"r1": doc("Bread", "flour, water, salt and time")})
    await synced(archive, gateway)
    names = {s["name"]: s["count"] for s in (await archive.sources_view())["sources"]}
    assert names == {"journals": 5, "recipes": 1}


async def test_a_pushed_event_brings_the_read_forward(archive, gateway, clock):
    gateway.plug("journals", dict(JOURNALS), topic="posts")
    await synced(archive, gateway)
    assert archive._due["journals"] == clock() + archive_mod.SWEEP_EVERY_S
    gateway.fire("journals", "posts")
    for _ in range(50):
        if archive._due["journals"] <= clock() + archive_mod.SETTLE_S:
            break
        await asyncio.sleep(0.02)
    assert archive._due["journals"] == clock() + archive_mod.SETTLE_S


async def test_an_event_during_a_read_asks_for_another(archive, gateway, clock):
    feed = gateway.plug("journals", dict(JOURNALS), topic="posts")
    await archive.refresh()

    def export_and_hear_a_post(args):
        archive._pushed(("journals", "posts"))
        return feed(args)

    gateway.handlers[("journals", "export")] = export_and_hear_a_post
    await archive.sweep("journals")
    assert archive._due["journals"] == clock() + archive_mod.SETTLE_S


async def test_a_source_whose_table_is_unusable_is_listed_but_never_read(archive, gateway):
    gateway.entries.append({"name": "odd", "status": "ready", "tools": ["other"],
                            "archive": [{"name": "odd", "mode": "index", "export": "odd_export",
                                         "fields": {"text": "content"}}]})
    await archive.refresh()
    assert archive._due_names() == []
    (row,) = (await archive.sources_view())["sources"]
    assert "does not publish" in row["last_error"]


async def test_the_sync_loop_reads_what_is_due(archive, gateway, monkeypatch):
    monkeypatch.setattr(archive_mod, "TICK_S", 0.05)
    gateway.plug("journals", dict(JOURNALS))
    task = asyncio.create_task(archive.run())
    for _ in range(200):
        if "journals" in archive.sources and count(archive, "journals") == 5:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    assert count(archive, "journals") == 5


async def test_an_entry_with_no_content_is_left_out_and_one_that_empties_is_dropped(archive, gateway):
    feed = gateway.plug("notes", {"n1": doc("Title", "body text here"), "n2": doc("", "")})
    await synced(archive, gateway)
    assert count(archive, "notes") == 1
    feed.docs["n1"] = doc("", "")
    await archive.sweep("notes")
    assert count(archive, "notes") == 0
