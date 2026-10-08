import time

import pytest

from awm.kb import feed as feed_mod
from awm.kb.feed import Feed


def post(i, kind="journal", body="text", **kw):
    return {"id": str(i), "project": "p", "scope": "s", "author": "agent:p/s", "kind": kind,
            "body": body, "meta": {}, "ts": "2026-10-02T00:00:00Z", **kw}


@pytest.fixture
def kb(monkeypatch):
    calls = {"upsert": [], "sweep": []}

    async def upsert(posts):
        calls["upsert"].append(posts)
        return {"queued": len(posts), "unchanged": 0, "skipped": 0}

    async def sweep(ids, full=True):
        calls["sweep"].append(sorted(ids))
        return {"missing": [], "forgotten": 1, "filtered": False}

    monkeypatch.setattr(feed_mod.client, "upsert", upsert)
    monkeypatch.setattr(feed_mod.client, "sweep", sweep)
    return calls


async def test_sweep_pages_every_kind_and_sends_the_full_id_set(kb, monkeypatch):
    monkeypatch.setattr(feed_mod, "PAGE", 2)
    monkeypatch.setattr(feed_mod, "UPSERT_BATCH", 2)
    store = {"journal": [post(1), post(2), post(3)], "goal": [post(4, "goal")],
             "message": [post(5, "message", body="")]}

    async def fetch(kind, limit, offset):
        return store.get(kind, [])[offset:offset + limit]

    f = Feed(fetch)
    res = await f.sweep()
    assert kb["sweep"] == [["1", "2", "3", "4"]]
    assert sum(len(b) for b in kb["upsert"]) == 4 and max(len(b) for b in kb["upsert"]) == 2
    assert res["posts"] == 4 and res["queued"] == 4 and res["forgotten"] == 1
    assert not f.due()


def test_title_is_prepended():
    assert feed_mod.to_kb(post(1, meta={"title": "T"}))["body"] == "T\n\ntext"


async def test_live_posts_forward_indexed_kinds_only(kb):
    f = Feed()
    await f.on_post({"project": "p", "scope": "s", "post": post(1)})
    await f.on_post({"project": "p", "scope": "s", "post": post(2, "system")})
    await f.on_post({"project": "p", "scope": "s", "post": post(3, body="")})
    assert [b[0]["id"] for b in kb["upsert"]] == ["1"]
    assert f.forwarded == 1


async def test_failed_forward_makes_a_sweep_due_soon(monkeypatch):
    async def down(posts):
        raise ConnectionError("kb down")

    monkeypatch.setattr(feed_mod.client, "upsert", down)
    f = Feed()
    f.last_ok = time.monotonic() - feed_mod.RETRY_S - 1
    assert not f.due()
    await f.on_post({"post": post(1)})
    assert f.dirty and f.forward_failures == 1
    assert f.due()
