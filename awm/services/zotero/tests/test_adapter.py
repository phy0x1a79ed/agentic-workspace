"""What the service does around a sync: starting, status and items.

`status` and `items` read the bundle outside the sync itself, so they are where
a rename of a `stats()` key goes unnoticed.
"""

from __future__ import annotations

import asyncio

import pytest

from awm.zotero import bundle, hub_adapter

from .fakes import MemoryStore


@pytest.fixture
def _no_timer(monkeypatch):
    """Start the service without starting its 20-minute loop."""
    started: list[str] = []
    monkeypatch.setattr(hub_adapter, "spawn_supervised",
                        lambda name, fn: started.append(name))
    return started


def _bundle_at(tmp_path):
    item = {"version": 1, "data": {"key": "AAAA", "itemType": "journalArticle",
                                   "title": "A paper"}}
    b = bundle.Bundle(MemoryStore())
    b.write(bundle.merge([bundle.normalize([item], [], {})], {"users/0": 7}))
    return b


# -- status ------------------------------------------------------------------


def _status(monkeypatch, tmp_path, versions):
    """Run the status verb against a one-item bundle and a stubbed service."""
    b = _bundle_at(tmp_path)
    monkeypatch.setattr(hub_adapter.sync, "held", lambda vault: b)
    monkeypatch.setattr(hub_adapter.source, "versions", lambda: versions)
    monkeypatch.setattr(hub_adapter.source, "whoami",
                        lambda: {"user_id": "1", "username": "someone",
                                 "writes": False, "groups_read": True})
    return asyncio.run(hub_adapter._h_status({}))


def test_status_is_not_behind_when_every_library_matches(tmp_path, monkeypatch):
    out = _status(monkeypatch, tmp_path, {"users/0": 7})
    assert out["library"]["reachable"] is True
    assert out["library"]["key_can_write"] is False
    assert out["behind"] is False


@pytest.mark.asyncio
async def test_a_push_that_finds_the_lock_held_is_retried_not_dropped(
        monkeypatch):
    """The pass holding the lock may have read its libraries before this frame
    arrived, and the flag is cleared before the pass. Dropping the frame here
    leaves the paper for the floor tick, twenty minutes away."""
    monkeypatch.setattr(hub_adapter, "SETTLE_S", 0)
    monkeypatch.setattr(hub_adapter, "BUSY_RETRY_S", 0)

    attempts: list[str] = []

    async def _sync(args, as_=None):
        attempts.append(args["trigger"])
        if len(attempts) == 1:
            raise hub_adapter.sync.Busy("another sync is running")
        return {"pull": {"changed": False}}

    monkeypatch.setattr(hub_adapter, "_h_sync", _sync)
    hub_adapter.WOKEN.set()

    task = asyncio.create_task(hub_adapter._push_loop())
    for _ in range(20):
        await asyncio.sleep(0)
        if len(attempts) >= 2:
            break
    task.cancel()

    assert len(attempts) >= 2, "the busy push was dropped rather than retried"


# -- items -------------------------------------------------------------------


def _library(*versions):
    return {"versions": {"users/0": max(versions)}, "pulled": "2026-10-01T00:00:00Z",
            "items": [{"key": f"K{v}", "library": "users/0", "ref": f"users/0/K{v}", "version": v,
                       "title": f"paper {v}", "notes": {"N": "<p>x</p>"}} for v in versions]}


def test_items_pages_what_moved_past_the_cursor_without_notes():
    library = _library(3, 1, 5, 4)
    first = hub_adapter.items_page(library, {"users/0": 2}, 0, 2)
    assert [i["key"] for i in first["items"]] == ["K3", "K4"]
    assert first["total"] == 3 and first["next"] == 2
    assert "notes" not in first["items"][0]
    rest = hub_adapter.items_page(library, {"users/0": 2}, first["next"], 2)
    assert [i["key"] for i in rest["items"]] == ["K5"] and rest["next"] is None
    assert rest["versions"] == {"users/0": 5}


def test_items_is_open_to_peers_and_the_operator_only(tmp_path, monkeypatch):
    b = _bundle_at(tmp_path)
    monkeypatch.setattr(hub_adapter.sync, "held", lambda vault: b)
    for as_ in (None, "peer:altair"):
        assert asyncio.run(hub_adapter._h_items({}, as_))["total"] == 1
    with pytest.raises(PermissionError):
        asyncio.run(hub_adapter._h_items({}, "user:someone"))
