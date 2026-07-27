"""Behavioural tests for :class:`SupervisedSubscription`.

Driven against fake streams rather than sockets: every property under test is
a property of the *loop*, and a real WS would only add flakiness. The four
behaviours below are the four defects the hand-rolled consumer loops shared,
each of which contributed to the 2026-07-26 ``/approve`` outage.
"""

from __future__ import annotations

import asyncio

import pytest

from awm.gatewayclient.subscription import SupervisedSubscription


def _fast(sub_kwargs: dict | None = None) -> dict:
    """Timings squashed so the loop's behaviour is testable in milliseconds."""
    base = {
        "backoff_initial": 0.01,
        "backoff_max": 0.02,
        "stability_seconds": 0.05,
        "idle_timeout": 10.0,
        "idle_jitter": 0.0,
    }
    base.update(sub_kwargs or {})
    return base


async def _run_briefly(sub: SupervisedSubscription, seconds: float = 0.3) -> None:
    task = asyncio.create_task(sub.run())
    await asyncio.sleep(seconds)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestReconnect:
    async def test_reconnects_after_the_stream_ends(self):
        """A stream that ends must be re-opened — this is the whole fix. The
        gateway now closes a subscriber whose emitter went down; before, the
        socket just hung and the consumer was deaf until restarted."""
        opened = 0

        async def stream():
            nonlocal opened
            opened += 1
            yield {"n": opened}

        sub = SupervisedSubscription("t", stream, lambda ev: None, **_fast())
        await _run_briefly(sub, 0.2)
        assert opened > 2, f"only {opened} connection(s) — not reconnecting"
        assert sub.reconnects >= 2

    async def test_backoff_resets_on_a_connection_that_lasted(self):
        """Backoff must reset on a *durable* connection, not on a received
        event. ``social/command`` can be silent for days; the old loop reset
        inside the iteration body and so ratcheted to the 30 s cap even while
        connecting perfectly."""
        async def stream():
            await asyncio.sleep(0.08)   # exceeds stability_seconds
            return
            yield  # pragma: no cover — makes this an async generator

        sub = SupervisedSubscription("t", stream, lambda ev: None,
                                     **_fast({"stability_seconds": 0.05}))
        await _run_briefly(sub, 0.3)
        assert sub.consecutive_failures == 0
        assert sub.connects >= 2

    async def test_short_connections_count_as_failures(self):
        async def stream():
            raise RuntimeError("connect refused")
            yield  # pragma: no cover

        sub = SupervisedSubscription("t", stream, lambda ev: None, **_fast())
        await _run_briefly(sub, 0.2)
        assert sub.consecutive_failures >= 2
        assert not sub.healthy
        assert "connect refused" in (sub.last_error or "")


class TestIdleDeadline:
    async def test_a_silent_stream_is_resubscribed(self):
        """The bound on deafness. A stream that connects and then yields
        nothing forever — the exact shape of an orphaned subscriber — must be
        replaced, without anyone having to diagnose why it went quiet."""
        opened = 0

        async def stream():
            nonlocal opened
            opened += 1
            await asyncio.sleep(3600)
            yield  # pragma: no cover

        sub = SupervisedSubscription("t", stream, lambda ev: None,
                                     **_fast({"idle_timeout": 0.05}))
        await _run_briefly(sub, 0.3)
        assert opened > 2, "silent stream was never re-subscribed"

    async def test_resubscribe_is_break_before_make(self):
        """The old stream must be fully closed before the new one opens.

        Overlapping subscriptions would deliver ``/approve`` twice, and the
        recovery window is one-shot by design: a duplicate would re-open a
        consumed window and authorise a second Duo push.
        """
        live = 0
        max_live = 0

        async def stream():
            nonlocal live, max_live
            live += 1
            max_live = max(max_live, live)
            try:
                await asyncio.sleep(3600)
                yield  # pragma: no cover
            finally:
                live -= 1

        sub = SupervisedSubscription("t", stream, lambda ev: None,
                                     **_fast({"idle_timeout": 0.05}))
        await _run_briefly(sub, 0.3)
        assert max_live == 1, f"{max_live} overlapping subscriptions"


class TestHandlerIsolation:
    async def test_a_raising_handler_does_not_end_the_stream(self):
        delivered = []

        async def stream():
            for n in range(5):
                yield {"n": n}
            await asyncio.sleep(3600)

        def handler(ev):
            delivered.append(ev["n"])
            if ev["n"] == 1:
                raise ValueError("bad event")

        sub = SupervisedSubscription("t", stream, handler, **_fast())
        await _run_briefly(sub, 0.2)
        assert delivered == [0, 1, 2, 3, 4]
        assert sub.handler_errors == 1

    async def test_async_and_sync_handlers_both_work(self):
        seen = []

        async def stream():
            yield {"n": 1}
            await asyncio.sleep(3600)

        async def handler(ev):
            seen.append(ev)

        sub = SupervisedSubscription("t", stream, handler, **_fast())
        await _run_briefly(sub, 0.15)
        assert seen and seen[0] == {"n": 1}

    async def test_intercepted_events_never_reach_the_handler(self):
        """The self-test probe path: a synthetic event must be claimed before
        dispatch, so it can never open a window or arm anything."""
        seen = []

        async def stream():
            yield {"probe": True}
            yield {"probe": False}
            await asyncio.sleep(3600)

        sub = SupervisedSubscription(
            "t", stream, lambda ev: seen.append(ev),
            intercept=lambda ev: bool(ev.get("probe")), **_fast())
        await _run_briefly(sub, 0.15)
        assert seen == [{"probe": False}]


class TestHealth:
    async def test_health_block_is_reportable_and_never_raises(self):
        async def stream():
            yield {"n": 1}
            await asyncio.sleep(3600)

        sub = SupervisedSubscription("t", stream, lambda ev: None, **_fast())
        assert sub.health()["connected"] is False
        await _run_briefly(sub, 0.15)
        h = sub.health()
        assert h["name"] == "t"
        assert h["events"] == 1
        assert h["last_event_age_s"] is not None
        assert set(h) >= {"connected", "healthy", "reconnects",
                          "handler_errors", "last_error"}
