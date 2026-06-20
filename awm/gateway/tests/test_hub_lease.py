"""Unit tests for the LeaseManager eviction-notice primitives.

``signal_evicted`` stages a (reason, evictor) notice and wakes the holder's
disconnect Event (so its WS handler unwinds via the normal ``hold`` path), and
``take_eviction`` pops the staged notice exactly once. These back the "last
connect wins, evicted shadow gets a who/why message" behavior.
"""

from __future__ import annotations

import asyncio

import pytest
pytestmark = [pytest.mark.unit, pytest.mark.smoke]

from awm.gateway.hub.lease import LeaseManager
from awm.gateway.hub.registry import Registry


def _lm() -> LeaseManager:
    return LeaseManager(Registry())


def test_signal_evicted_sets_holder_event():
    async def go():
        lm = _lm()
        ev = asyncio.Event()
        lm._holders["sid-1"] = ev
        lm.signal_evicted("sid-1", "a newer shadow connected", "ov @ wt-b")
        assert ev.is_set()
        # The notice is staged for the handler to read once.
        notice = lm.take_eviction("sid-1")
        assert notice == ("a newer shadow connected", "ov @ wt-b")
        # Second read is empty (popped).
        assert lm.take_eviction("sid-1") is None

    asyncio.run(go())


def test_signal_evicted_without_holder_still_stages_notice():
    # Eviction can be staged before/after the holder registers; staging never
    # raises if no holder is present yet.
    lm = _lm()
    lm.signal_evicted("sid-2", "a newer shadow connected", "ov @ wt-c")
    assert lm.take_eviction("sid-2") == ("a newer shadow connected", "ov @ wt-c")


def test_take_eviction_absent_returns_none():
    assert _lm().take_eviction("nope") is None
