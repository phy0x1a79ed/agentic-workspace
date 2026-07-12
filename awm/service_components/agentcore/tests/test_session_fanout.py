"""Tests for the AgentSession fan-out, partial-drop backpressure, and the
generic run_once drain — all with a fake in-memory backend (no subprocess)."""

from __future__ import annotations

import asyncio

import pytest

from awm.agentcore import AgentConfig, AgentEvent, run_once
from awm.agentcore.session import AgentSession, _drop_one_partial, _Subscriber


class FakeSession(AgentSession):
    """A backend that replays a scripted list of events, optionally one batch
    per ``send`` call (to exercise run_once)."""

    def __init__(self, scripted: list[AgentEvent], *, per_send=False):
        super().__init__(AgentConfig(harness="claude"))
        self._scripted = scripted
        self._per_send = per_send
        self._q: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.closed_flag = False
        self.started_flag = False

    async def _start(self):
        self.started_flag = True
        if not self._per_send:
            for e in self._scripted:
                self._q.put_nowait(e)
            self._q.put_nowait(None)

    async def send(self, text: str):
        if not self._started:
            await self.start()
        self.sent.append(text)
        if self._per_send:
            for e in self._scripted:
                self._q.put_nowait(e)

    async def _event_source(self):
        while True:
            e = await self._q.get()
            if e is None:
                return
            yield e

    async def close(self):
        self.closed_flag = True
        self._q.put_nowait(None)
        await self._stop_pump()


async def test_fanout_two_consumers_each_get_all():
    events = [AgentEvent(kind="message", text=f"m{i}") for i in range(3)]
    sess = FakeSession(events)
    # Subscribe both consumers BEFORE start() so the pump can't drain past them.
    s1 = sess.subscribe()
    s2 = sess.subscribe()
    await sess.start()

    async def drain(stream):
        got = []
        async for e in stream:
            got.append(e.text)
        return got

    a, b = await asyncio.gather(drain(s1), drain(s2))
    assert a == ["m0", "m1", "m2"]
    assert b == ["m0", "m1", "m2"]
    await sess.close()


def test_drop_one_partial_keeps_order_and_non_partials():
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(AgentEvent(kind="message", text="keep1"))
    q.put_nowait(AgentEvent(kind="partial", text="drop"))
    q.put_nowait(AgentEvent(kind="message", text="keep2"))
    dropped = _drop_one_partial(q)
    assert dropped is True
    remaining = []
    while not q.empty():
        remaining.append(q.get_nowait())
    texts = [e.text for e in remaining]
    assert texts == ["keep1", "keep2"]


def test_subscriber_drops_partials_under_pressure():
    sub = _Subscriber()
    # Fill with non-partials up to cap.
    from awm.agentcore.session import _SUB_QUEUE_MAX
    for i in range(_SUB_QUEUE_MAX):
        sub.offer(AgentEvent(kind="message", text=f"m{i}"))
    # A further partial under pressure should be dropped (queue stays at cap,
    # no exception).
    sub.offer(AgentEvent(kind="partial", text="late"))
    assert sub.queue.qsize() == _SUB_QUEUE_MAX


async def test_run_once_returns_final_text():
    scripted = [
        AgentEvent(kind="message", text="partial answer"),
        AgentEvent(kind="result", text="final answer"),
    ]

    sess = FakeSession(scripted, per_send=True)

    # Monkeypatch open_agent to return our fake.
    import awm.agentcore.api as api
    orig = api.open_agent
    api.open_agent = lambda cfg: sess
    try:
        out = await run_once(AgentConfig(harness="claude"), "do it")
    finally:
        api.open_agent = orig

    assert out == "final answer"
    assert sess.sent == ["do it"]
    assert sess.closed_flag is True


async def test_run_once_schema_parses_json():
    scripted = [
        AgentEvent(kind="result", text='Here you go: {"ok": true, "n": 5}'),
    ]
    sess = FakeSession(scripted, per_send=True)
    import awm.agentcore.api as api
    orig = api.open_agent
    api.open_agent = lambda cfg: sess
    try:
        out = await run_once(
            AgentConfig(harness="opencode"), "give json",
            schema={"type": "object"},
        )
    finally:
        api.open_agent = orig
    assert out == {"ok": True, "n": 5}
    # The schema instruction was appended to the sent prompt.
    assert "JSON Schema" in sess.sent[0]


async def test_run_once_error_raises():
    scripted = [AgentEvent(kind="error", text="boom")]
    sess = FakeSession(scripted, per_send=True)
    import awm.agentcore.api as api
    orig = api.open_agent
    api.open_agent = lambda cfg: sess
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await run_once(AgentConfig(harness="claude"), "x")
    finally:
        api.open_agent = orig
