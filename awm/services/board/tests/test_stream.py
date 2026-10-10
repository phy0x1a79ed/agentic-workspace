"""The SSE stream: replay from a cursor, tail, heartbeat, and the maintenance loop."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))

from door_stubs import Live, StubBoard, StubEvents, StubParties  # noqa: E402

from awm.board import http, stream  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

ALPHA = {"party_id": "pa", "swarm": "alpha", "principal": "p", "relation": "domestic", "revoked": False}


def card(n: int, sender: str = "alpha", recipient: str = "beta") -> dict:
    return {"id": f"card-{n}", "sender": {"swarm": sender, "principal": "p", "party": "x"},
            "recipient": recipient, "status": "posted"}


async def take(gen, n: int, timeout: float = 5.0) -> list[bytes]:
    out = []
    async def run():
        async for chunk in gen:
            out.append(chunk)
            if len(out) == n:
                return
    await asyncio.wait_for(run(), timeout)
    return out


def test_a_frame_is_one_event_with_its_id():
    raw = stream.frame(7, "card.posted", {"id": "c", "title": "line\nbreak"}).decode()
    assert raw.startswith("id: 7\nevent: card.posted\ndata: ")
    assert raw.endswith("\n\n")
    assert raw.count("\n") == 4  # the newline in the title is escaped, not emitted


def test_last_event_id_parsing():
    assert stream.parse_last_event_id("12") == 12
    assert stream.parse_last_event_id(" 3 ") == 3
    for bad in (None, "", "abc", "-4"):
        assert stream.parse_last_event_id(bad) is None


async def test_replay_sends_only_what_follows_the_cursor_and_what_the_party_may_see():
    events = StubEvents()
    events.append("card.posted", card(1, recipient="beta"))
    events.append("card.posted", card(2, sender="gamma", recipient="delta"))  # alpha may not see it
    events.append("card.claimed", card(3, recipient="open"))
    events.append("card.posted", card(4, sender="alpha", recipient="delta"))  # alpha sent it

    gen = stream.event_stream(events, ALPHA, 1, heartbeat_s=60)
    chunks = await take(gen, 3)
    await gen.aclose()

    assert chunks[0] == stream.PREAMBLE
    ids = [c.decode().split("\n")[0] for c in chunks[1:]]
    assert ids == ["id: 3", "id: 4"]


async def test_replay_from_zero_sends_everything_visible():
    events = StubEvents()
    for n in range(1, 4):
        events.append("card.posted", card(n))
    gen = stream.event_stream(events, ALPHA, 0, heartbeat_s=60)
    chunks = await take(gen, 4)
    await gen.aclose()
    assert [c.decode().split("\n")[0] for c in chunks[1:]] == ["id: 1", "id: 2", "id: 3"]


async def test_no_cursor_means_live_events_only():
    events = StubEvents()
    events.append("card.posted", card(1))
    wakeup = stream.Wakeup()
    gen = stream.event_stream(events, ALPHA, None, wakeup=wakeup, heartbeat_s=60, poll_s=0.05)

    async def post_later():
        await asyncio.sleep(0.1)
        events.append("card.posted", card(2))
        wakeup.notify()

    poster = asyncio.create_task(post_later())
    chunks = await take(gen, 2)
    await gen.aclose()
    await poster
    assert chunks[1].decode().startswith("id: 2\n")


async def test_the_tail_delivers_an_event_appended_after_the_replay():
    events = StubEvents()
    wakeup = stream.Wakeup()
    notifying = stream.NotifyingEvents(events, wakeup)
    gen = stream.event_stream(notifying, ALPHA, 0, wakeup=wakeup, heartbeat_s=60, poll_s=30)

    async def post_later():
        await asyncio.sleep(0.1)
        await asyncio.to_thread(notifying.append, "card.posted", card(1))

    poster = asyncio.create_task(post_later())
    # poll_s is 30 s, so only the wakeup can make this arrive inside the timeout.
    chunks = await take(gen, 2, timeout=3)
    await gen.aclose()
    await poster
    assert chunks[1].decode().startswith("id: 1\n")


async def test_an_idle_stream_sends_a_heartbeat_comment():
    gen = stream.event_stream(StubEvents(), ALPHA, 0, heartbeat_s=0.1, poll_s=0.05)
    chunks = await take(gen, 3, timeout=3)
    await gen.aclose()
    assert chunks[1] == stream.HEARTBEAT
    assert chunks[2] == stream.HEARTBEAT


async def test_a_revoked_party_loses_the_stream_at_the_next_heartbeat():
    alive = {"yes": True}
    gen = stream.event_stream(StubEvents(), ALPHA, 0, heartbeat_s=0.1, poll_s=0.05,
                              still_valid=lambda: alive["yes"])
    first = await take(gen, 2, timeout=3)
    assert first[1] == stream.HEARTBEAT
    alive["yes"] = False
    rest = [chunk async for chunk in gen]
    assert rest == []


async def test_maintain_sweeps_repeatedly_and_prunes_on_its_own_schedule():
    events = StubEvents()
    pruned = []
    events.prune = lambda days=30: pruned.append(days) or 0
    board = StubBoard(events)
    task = asyncio.create_task(stream.maintain(board, events, sweep_s=0.02, prune_s=3600))
    await asyncio.sleep(0.2)
    task.cancel()
    assert board.sweeps >= 3
    assert pruned == [30]  # once, at the start, then not again for an hour


async def test_maintain_survives_a_failing_sweep():
    events = StubEvents()
    board = StubBoard(events)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("vault down")
        return []

    board.sweep = flaky
    task = asyncio.create_task(stream.maintain(board, events, sweep_s=0.02))
    await asyncio.sleep(0.15)
    task.cancel()
    assert len(calls) >= 2


# -- over a real socket ---------------------------------------------------------


@pytest.fixture
async def live():
    server = Live(heartbeat_s=0.2, poll_s=0.05)
    await server.start()
    yield server
    await server.stop()


async def sse_events(lines, n: int, *, ids_only: bool = False) -> list[tuple[str, str]]:
    """The next ``n`` (kind, text) pairs from one line iterator: ('id', '3'), ('comment', 'keepalive')."""
    seen: list[tuple[str, str]] = []
    async for line in lines:
        if line.startswith(":") and not ids_only:
            seen.append(("comment", line[1:].strip()))
        elif line.startswith("id:"):
            seen.append(("id", line[3:].strip()))
        if len(seen) >= n:
            break
    return seen


async def test_the_endpoint_replays_after_last_event_id_and_then_tails(live):
    _, token = live.parties.add("alpha", "p", "domestic")
    live.events.append("card.posted", card(1))
    live.events.append("card.posted", card(2))
    live.events.append("card.claimed", card(3))
    headers = {"Authorization": f"Bearer {token}", "Last-Event-ID": "1"}

    async with httpx.AsyncClient(timeout=5) as client:
        async with client.stream("GET", live.url + "/board/stream", headers=headers) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = response.aiter_lines()
            assert (await sse_events(lines, 2, ids_only=True)) == [("id", "2"), ("id", "3")]
            live.events.append("card.completed", card(4))
            live.wakeup.notify()
            assert (await sse_events(lines, 1, ids_only=True)) == [("id", "4")]


async def test_the_endpoint_flushes_a_heartbeat_while_idle(live):
    _, token = live.parties.add("alpha", "p", "domestic")
    headers = {"Authorization": f"Bearer {token}", "Last-Event-ID": "0"}

    async with httpx.AsyncClient(timeout=5) as client:
        async with client.stream("GET", live.url + "/board/stream", headers=headers) as response:
            lines = response.aiter_lines()
            beats = await sse_events(lines, 2)
            assert beats == [("comment", "keepalive"), ("comment", "keepalive")]


async def test_the_endpoint_refuses_a_bad_token_with_404(live):
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(live.url + "/board/stream", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 404
