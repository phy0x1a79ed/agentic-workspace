"""A slow commit must not hold the event loop, which answers the gateway's keepalive pings."""

from __future__ import annotations

import asyncio
import time

import pytest

from awm.reporting import hub_adapter
from awm.reporting.notify import Notifier
from awm.reporting.store import Db, Offloaded
from awm.reporting.subscriber import Subscriber

from stubs import make_card
from test_tasks import Env

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

STALL_S = 0.15
#: A loop that was free answers a 5 ms ticker well inside this; one that waited on a stall cannot.
MAX_GAP_S = 0.1


@pytest.fixture
def slow_disk(monkeypatch):
    real = Db._conn

    def slow(self):
        time.sleep(STALL_S)
        return real(self)

    monkeypatch.setattr(Db, "_conn", slow)


async def worst_gap_during(work) -> float:
    """The longest the loop went without running a 5 ms ticker while ``work`` ran."""
    worst, last, done = 0.0, time.monotonic(), False

    async def tick():
        nonlocal worst, last
        while not done:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            worst, last = max(worst, now - last), now

    ticker = asyncio.create_task(tick())
    try:
        await work
        await asyncio.sleep(0.02)  # let the ticker report a stall that ended with the work
    finally:
        done = True
        await ticker
    return worst


async def test_offloaded_calls_leave_the_loop_free(queue, slow_disk):
    db = Offloaded(queue)

    async def work():
        await db.enqueue(make_card())
        assert await db.counts()

    assert await worst_gap_during(work()) < MAX_GAP_S


async def test_a_task_verb_and_a_sweep_leave_the_loop_free(tmp_path, slow_disk):
    env = Env(tmp_path)

    async def work():
        assert (await env.call("hook_plan", plan_text="# Plan\nbody"))["ok"]
        await env.tasks.sweep([{"job": "job00001", "state": "running"}])

    assert await worst_gap_during(work()) < MAX_GAP_S
    assert env.task()["state"] == "planned"


async def test_the_notifier_leaves_the_loop_free(queue, slow_disk):
    queue.enqueue(make_card())
    sent = []

    async def find():
        return "r1"

    notifier = Notifier(queue, find, send=lambda job, text: sent.append(text))
    assert await worst_gap_during(notifier.flush()) < MAX_GAP_S
    assert sent


async def test_the_subscriber_leaves_the_loop_free(queue, slow_disk):
    class Board:
        async def claim(self, card_id):
            return {}

    sub = Subscriber(queue, Board(), "tony")
    card = make_card()
    assert await worst_gap_during(sub._intake(card, claim_known=False)) < MAX_GAP_S
    assert queue.has(card["id"])


async def test_opening_the_database_at_start_leaves_the_loop_free(monkeypatch, slow_disk):
    monkeypatch.setattr(hub_adapter, "RUNTIME", None)
    monkeypatch.setattr(hub_adapter, "spawn_supervised", lambda *a, **k: None)
    assert await worst_gap_during(hub_adapter._on_start()) < MAX_GAP_S
    assert hub_adapter.RUNTIME is not None
