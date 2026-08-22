"""Telemetry substrate — append/seq/id, cursor reads, stream isolation, the
in-proc bus + lagged backpressure, and the backfill→live session round-trip."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from awm.telemetry import Telemetry, TelemetryBus, TelemetryStore

# asyncio_mode = "auto" (pyproject) runs the async tests; no per-test mark.


# ---------------------------------------------------------------------------
# Store: append ordering / ids, cursoring, stream isolation
# ---------------------------------------------------------------------------

class TestStore:
    def test_append_assigns_monotonic_seq_and_unique_id(self, tel_env):
        st = TelemetryStore("svc")
        a = st.append("s1", "k", "one")
        b = st.append("s1", "k", "two")
        c = st.append("s1", "k", "three")
        assert a["seq"] < b["seq"] < c["seq"]      # monotonic ordering key
        assert len({a["id"], b["id"], c["id"]}) == 3  # stable, unique ids
        assert a["ts"] and isinstance(a["ts"], int)

    def test_read_after_cursors_by_seq(self, tel_env):
        st = TelemetryStore("svc")
        a = st.append("s1", "k", "one")
        st.append("s1", "k", "two")
        st.append("s1", "k", "three")
        # No cursor → whole stream, in order.
        full = st.read_after("s1")
        assert [e["body"] for e in full] == ["one", "two", "three"]
        # After the first event's seq → only the later two.
        rest = st.read_after("s1", a["seq"])
        assert [e["body"] for e in rest] == ["two", "three"]
        # Limit caps the batch.
        assert len(st.read_after("s1", limit=2)) == 2

    def test_streams_are_isolated(self, tel_env):
        st = TelemetryStore("svc")
        st.append("a", "k", "x")
        st.append("b", "k", "y")
        assert [e["body"] for e in st.read_after("a")] == ["x"]
        assert [e["body"] for e in st.read_after("b")] == ["y"]

    def test_meta_round_trips(self, tel_env):
        st = TelemetryStore("svc")
        ev = st.append("s", "k", "", {"from": "ready", "to": "active"})
        got = st.read_after("s")[0]
        assert got["meta"] == {"from": "ready", "to": "active"}


# ---------------------------------------------------------------------------
# Bus: fan-out + lagged backpressure
# ---------------------------------------------------------------------------

class TestBus:
    async def test_publish_fans_out_to_subscribers(self):
        bus = TelemetryBus()
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        await bus.attach_live("s", q)
        bus.publish({"stream": "s", "id": "1", "seq": 1, "kind": "k",
                     "body": "x", "meta": {}, "ts": 1})
        frame = q.get_nowait()
        assert frame["type"] == "event" and frame["event"]["id"] == "1"

    async def test_other_stream_not_delivered(self):
        bus = TelemetryBus()
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        await bus.attach_live("s", q)
        bus.publish({"stream": "other", "id": "1", "seq": 1})
        assert q.empty()

    async def test_full_queue_yields_lagged_sentinel(self):
        bus = TelemetryBus()
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        await bus.attach_live("s", q)
        bus.publish({"stream": "s", "id": "1", "seq": 1})  # fills the queue
        bus.publish({"stream": "s", "id": "2", "seq": 2})  # no room → lagged
        frames = []
        while not q.empty():
            frames.append(q.get_nowait())
        # A lagged sentinel was delivered (the client will reconnect + replay).
        assert any(f["type"] == "lagged" for f in frames)

    async def test_detach_stops_delivery(self):
        bus = TelemetryBus()
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        await bus.attach_live("s", q)
        await bus.detach_live("s", q)
        bus.publish({"stream": "s", "id": "1", "seq": 1})
        assert q.empty()


# ---------------------------------------------------------------------------
# Session: backfill from cursor → live push (the reusable WS handler)
# ---------------------------------------------------------------------------

class _FakeBridge:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


class _FakeCtx:
    def __init__(self, init: dict, bridge: _FakeBridge) -> None:
        self.init = init
        self._bridge = bridge

    async def open_bridge(self) -> _FakeBridge:
        return self._bridge


async def _until(pred, *, tries: int = 200, delay: float = 0.01) -> None:
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(delay)
    raise AssertionError("condition not met in time")


class TestSession:
    async def test_backfill_then_live(self, tel_env):
        tel = Telemetry("svc")
        tel.emit("s1", "a", "one")
        tel.emit("s1", "b", "two")

        bridge = _FakeBridge()
        ctx = _FakeCtx({"stream": "s1"}, bridge)
        task = asyncio.create_task(tel.session_handler(ctx))
        try:
            # First frame is the cursored backfill.
            await _until(lambda: len(bridge.sent) >= 1)
            first = json.loads(bridge.sent[0])
            assert first["type"] == "backfill"
            assert [e["body"] for e in first["events"]] == ["one", "two"]

            # A subsequent emit streams live as an 'event' frame.
            tel.emit("s1", "c", "three")
            await _until(lambda: len(bridge.sent) >= 2)
            live = json.loads(bridge.sent[1])
            assert live["type"] == "event"
            assert live["event"]["body"] == "three"
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_backfill_honors_cursor(self, tel_env):
        tel = Telemetry("svc")
        a = tel.emit("s1", "a", "one")
        tel.emit("s1", "b", "two")

        bridge = _FakeBridge()
        ctx = _FakeCtx({"stream": "s1", "after_seq": a["seq"]}, bridge)
        task = asyncio.create_task(tel.session_handler(ctx))
        try:
            await _until(lambda: len(bridge.sent) >= 1)
            first = json.loads(bridge.sent[0])
            assert [e["body"] for e in first["events"]] == ["two"]
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_missing_stream_errors_and_closes(self, tel_env):
        tel = Telemetry("svc")
        bridge = _FakeBridge()
        ctx = _FakeCtx({}, bridge)
        await tel.session_handler(ctx)  # returns immediately
        assert json.loads(bridge.sent[0])["type"] == "error"
        assert bridge.closed
