"""The tick, and the guards that stop a second copy from seeding beside it."""

from __future__ import annotations

import pytest


async def test_a_tick_seeds_before_it_removes(box, monkeypatch):
    """What makes "create the new one, then delete the old one" true.

    Nothing sequences the two. Seeding runs first in the tick and an aged
    session has already stopped counting as claimable, so the replacement
    exists before removal collects what it replaced.
    """
    from awm.cx import reconcile, remove, seed

    order: list[str] = []
    monkeypatch.setenv("AWM_CX_ROTATE_AGE_S", "1")

    async def fake_seed():
        order.append("seed")
        return "new"

    async def fake_apply(now=None):
        order.append("remove")
        return []

    monkeypatch.setattr(seed, "seed_one", fake_seed)
    monkeypatch.setattr(remove, "apply", fake_apply)
    await reconcile.Loop().tick()
    assert order == ["seed", "remove"]


async def test_a_tick_does_not_seed_when_the_pool_is_full(box, monkeypatch):
    from awm.cx import reconcile, remove, seed

    seeded = []
    monkeypatch.setenv("AWM_CX_ROTATE_AGE_S", "999999")
    monkeypatch.setattr(seed, "seed_one", lambda: seeded.append(1))
    monkeypatch.setattr(remove, "apply", _nothing)
    await reconcile.Loop().tick()
    assert seeded == []


async def test_a_refusal_does_not_end_the_loop(box, monkeypatch):
    from awm.cx import reconcile, remove, seed

    monkeypatch.setenv("AWM_CX_ROTATE_AGE_S", "1")
    monkeypatch.setattr(remove, "apply", _nothing)

    async def refuse():
        raise seed.Refused("no daemon")

    monkeypatch.setattr(seed, "seed_one", refuse)
    loop = reconcile.Loop()
    await loop.tick()
    assert loop.status()["last_tick"] is not None


async def test_only_one_copy_holds_the_pool(box, monkeypatch):
    """One mechanism for the overlay, the sandbox, and a stray manual run."""
    from awm.cx import reconcile, remove, seed

    monkeypatch.setenv("AWM_CX_ROTATE_AGE_S", "1")
    seeded = []

    async def count():
        seeded.append(1)
        return "new"

    monkeypatch.setattr(seed, "seed_one", count)
    monkeypatch.setattr(remove, "apply", _nothing)
    first, second = reconcile.Loop(), reconcile.Loop()
    await first.tick()
    await second.tick()
    assert first.status()["holds_lock"]
    assert not second.status()["holds_lock"]
    assert len(seeded) == 1


async def test_an_overlay_leaves_the_pool_to_the_base(box, monkeypatch):
    from awm.cx import reconcile

    monkeypatch.setenv("AWM_SERVICE_OVERLAY", "1")
    loop = reconcile.Loop()
    assert "overlay" in (loop.enabled() or "")
    await loop.tick()
    assert not loop.status()["holds_lock"]


async def test_the_loop_can_be_switched_off(box, monkeypatch):
    from awm.cx import reconcile

    monkeypatch.setenv("AWM_CX_LOOP", "0")
    assert "switched off" in (reconcile.Loop().enabled() or "")


async def _nothing(now=None):
    return []
