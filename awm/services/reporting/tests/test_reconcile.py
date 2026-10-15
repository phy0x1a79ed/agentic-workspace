"""The sweep ticker: one lock per node, a tick that never raises, and nothing swept when cx is silent."""

from __future__ import annotations

import asyncio

import pytest

from awm.reporting import reconcile

from stubs import FakeCx

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class Sweeps:
    def __init__(self) -> None:
        self.rows: list[list[dict]] = []
        self.error: Exception | None = None

    async def __call__(self, rows):
        if self.error:
            raise self.error
        self.rows.append(rows)


async def test_a_tick_hands_the_cx_rows_to_the_sweep():
    sweeps, cx = Sweeps(), FakeCx([{"job": "j1", "state": "idle"}])
    loop = reconcile.Loop(cx, on_rows=sweeps)
    await loop.tick()
    assert sweeps.rows == [[{"job": "j1", "state": "idle"}]]
    assert loop.status()["holds_lock"] is True and loop.status()["last_tick_s_ago"] is not None


async def test_a_tick_sweeps_nothing_when_cx_cannot_answer():
    sweeps, cx = Sweeps(), FakeCx()
    cx.unavailable = True
    loop = reconcile.Loop(cx, on_rows=sweeps)
    await loop.tick()
    assert sweeps.rows == [] and loop.status()["last_tick_s_ago"] is None


async def test_a_failing_sweep_does_not_end_the_tick():
    sweeps = Sweeps()
    sweeps.error = RuntimeError("boom")
    loop = reconcile.Loop(FakeCx(), on_rows=sweeps)
    await loop.tick()
    assert loop.status()["last_tick_s_ago"] is not None


async def test_a_second_copy_of_the_service_does_not_sweep_the_same_node():
    first, second = Sweeps(), Sweeps()
    one = reconcile.Loop(FakeCx(), on_rows=first)
    two = reconcile.Loop(FakeCx(), on_rows=second)
    await one.tick()
    await two.tick()
    assert len(first.rows) == 1 and second.rows == [] and two.status()["holds_lock"] is False


async def test_run_ticks_until_cancelled_and_survives_a_bad_tick(monkeypatch):
    sweeps = Sweeps()
    loop = reconcile.Loop(FakeCx(), interval_s=0.01, on_rows=sweeps)
    real = loop.tick
    calls = []

    async def tick():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first tick fails")
        await real()

    loop.tick = tick
    task = asyncio.create_task(loop.run())
    for _ in range(100):
        if sweeps.rows:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sweeps.rows and loop.status()["running"] is True
