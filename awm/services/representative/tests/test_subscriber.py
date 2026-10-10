"""The board subscriber: claims, queueing, the cursor and the catch-up."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from awm.board.client import RESYNC, BoardClient, BoardError
from awm.representative.subscriber import Subscriber

from stubs import FakeBoard, make_card

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def build(queue, board=None, swarm="tony", **kw):
    board = board or FakeBoard(swarm)
    woke = []
    sub = Subscriber(queue, board, swarm, on_queued=lambda: woke.append(1), **kw)
    return sub, board, woke


async def test_a_posted_request_is_claimed_then_queued(queue):
    sub, board, woke = build(queue)
    card = board.add(make_card(priority="urgent"))
    board.post_event("card.posted", card)
    await sub.consume()
    assert board.claims == [card["id"]]
    row = queue.get(card["id"])
    assert (row["status"], row["priority"], row["sender"]) == ("queued", "urgent", "beta")
    assert woke == [1]
    assert queue.cursor() == 1


async def test_a_claim_conflict_skips_the_card_but_moves_the_cursor(queue):
    sub, board, woke = build(queue)
    card = board.add(make_card())
    board.conflicts.add(card["id"])
    board.post_event("card.posted", card)
    await sub.consume()
    assert queue.get(card["id"]) is None
    assert woke == []
    assert queue.cursor() == 1


async def test_a_message_is_queued_and_never_claimed(queue):
    sub, board, woke = build(queue)
    card = board.add(make_card(kind="message"))
    board.post_event("card.posted", card)
    await sub.consume()
    assert board.claims == []
    assert queue.get(card["id"])["kind"] == "message"
    assert woke == [1]


async def test_cards_for_another_recipient_are_ignored(queue):
    sub, board, _ = build(queue)
    mine = board.add(make_card())
    theirs = board.add(make_card(recipient="gamma"))
    open_card = board.add(make_card(recipient="open"))
    for card in (mine, theirs, open_card):
        board.post_event("card.posted", card)
    await sub.consume()
    assert board.claims == [mine["id"]]
    assert [r["card_id"] for r in queue.list()] == [mine["id"]]
    assert queue.cursor() == 3  # ignored events still advance the cursor


async def test_the_swarm_is_the_nodes_and_not_tony(queue):
    sub, board, _ = build(queue, swarm="mock")
    for_mock = board.add(make_card(recipient="mock"))
    for_tony = board.add(make_card(recipient="tony"))
    board.post_event("card.posted", for_tony)
    board.post_event("card.posted", for_mock)
    await sub.consume()
    assert board.claims == [for_mock["id"]]


@pytest.mark.parametrize("final", ["done", "failed"])
async def test_a_finished_card_marks_its_row(queue, final):
    sub, board, _ = build(queue)
    card = board.add(make_card())
    board.post_event("card.posted", card)
    board.post_event("card.completed" if final == "done" else "card.failed",
                     {**card, "status": final})
    await sub.consume()
    assert queue.get(card["id"])["status"] == final


async def test_a_finished_card_the_queue_never_held_is_ignored(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card(status="done"))
    board.post_event("card.completed", card)
    await sub.consume()
    assert queue.list() == []


async def test_the_cursor_is_saved_only_after_the_card_write_commits(queue, monkeypatch):
    sub, board, _ = build(queue)
    first = board.add(make_card())
    second = board.add(make_card())
    board.post_event("card.posted", first)
    board.post_event("card.posted", second)
    real = queue.enqueue
    calls = []

    def flaky(card, **kw):
        calls.append(card["id"])
        if len(calls) == 2:
            raise OSError("disk full")
        return real(card, **kw)

    monkeypatch.setattr(queue, "enqueue", flaky)
    with pytest.raises(OSError):
        await sub.consume()
    assert queue.cursor() == 1  # the second event never committed
    assert queue.status_of(second["id"]) == "claiming"  # only the mark made before the claim


async def test_a_failed_claim_leaves_the_cursor_behind_and_a_restart_retries(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card())
    board.post_event("card.posted", card)
    board.claim_error = BoardError(503, "board is down")
    with pytest.raises(BoardError):
        await sub.consume()
    assert queue.cursor() is None
    board.claim_error = None
    await sub.consume()
    assert queue.get(card["id"]) is not None
    assert queue.cursor() == 1


async def test_a_restart_resumes_the_stream_from_the_saved_cursor(queue):
    sub, board, _ = build(queue)
    board.post_event("card.posted", board.add(make_card()))
    await sub.consume()
    board.post_event("card.posted", board.add(make_card()))
    sub2, _, _ = build(queue, board)
    await sub2.consume()
    assert board.stream_starts == [None, 1]
    assert len(queue.list()) == 2


async def test_a_replayed_event_does_not_claim_or_queue_twice(queue):
    sub, board, woke = build(queue)
    card = board.add(make_card())
    board.post_event("card.posted", card)
    await sub.consume()
    queue.mark(card["id"], "done")
    board.cards[card["id"]]["status"] = "done"
    queue.set_cursor(0, force=True)  # a lost cursor write: the event replays
    await sub.consume()
    assert board.claims == [card["id"]]
    assert queue.get(card["id"])["status"] == "done"
    assert woke == [1]


async def test_a_card_dragged_back_to_posted_is_reclaimed_and_reopened(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card())
    board.post_event("card.posted", card)
    await sub.consume()
    queue.mark(card["id"], "done")
    board.cards[card["id"]]["status"] = "posted"
    board.post_event("card.moved", board.cards[card["id"]])
    await sub.consume()
    assert board.claims == [card["id"], card["id"]]
    assert queue.get(card["id"])["status"] == "queued"


async def test_a_resync_catches_up_from_the_board_then_moves_the_cursor(queue):
    sub, board, _ = build(queue)
    missed = board.add(make_card())
    board.post_event(RESYNC, {"latest": 40, "oldest": 30}, event_id=40)
    await sub.consume()
    assert board.claims == [missed["id"]]
    assert queue.get(missed["id"]) is not None
    assert queue.cursor() == 40


async def test_a_start_with_cards_posted_during_a_gap_queues_them(queue):
    sub, board, _ = build(queue)
    cards = [board.add(make_card()) for _ in range(3)]
    gone = board.add(make_card(status="in_progress", claimant="beta"))
    await sub.catch_up()
    assert sorted(r["card_id"] for r in queue.list()) == sorted(c["id"] for c in cards)
    assert gone["id"] not in board.claims


async def test_a_catch_up_adopts_only_a_claim_the_door_itself_attempted(queue):
    sub, board, _ = build(queue)
    attempted = board.add(make_card(status="in_progress", claimant="tony"))
    queue.begin_claim(attempted)  # the door wrote this before it claimed, then crashed
    by_another_agent = board.add(make_card(status="in_progress", claimant="tony"))
    by_beta = board.add(make_card(status="in_progress", claimant="beta"))
    await sub.catch_up()
    assert [r["card_id"] for r in queue.list()] == [attempted["id"]]
    assert queue.get(attempted["id"])["status"] == "queued"
    assert queue.get(by_another_agent["id"]) is None
    assert queue.get(by_beta["id"]) is None
    assert board.claims == []


async def test_an_event_adopts_only_on_the_claiming_mark(queue):
    sub, board, _ = build(queue)
    marked = board.add(make_card(status="in_progress", claimant="tony"))
    queue.begin_claim(marked)
    unmarked = board.add(make_card(status="in_progress", claimant="tony"))
    finished = board.add(make_card(status="in_progress", claimant="tony"))
    queue.enqueue(finished)
    queue.mark(finished["id"], "done")  # a replayed old `claimed` event must not reopen it
    for card in (marked, unmarked, finished):
        board.post_event("card.claimed", card)
    await sub.consume()
    assert queue.get(marked["id"])["status"] == "queued"
    assert queue.get(unmarked["id"]) is None
    assert queue.get(finished["id"])["status"] == "done"


async def test_a_finished_row_the_board_shows_in_progress_under_us_is_adopted_by_a_catch_up(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card())
    await sub.catch_up()
    queue.mark(card["id"], "done")
    board.cards[card["id"]].update(status="posted", claimant=None)
    await sub.catch_up()  # reopened on the board, claimed again
    assert queue.get(card["id"])["status"] == "queued"


async def test_a_claim_attempt_the_board_refuses_leaves_no_row_behind(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card())
    board.conflicts.add(card["id"])
    board.post_event("card.posted", card)
    await sub.consume()
    assert queue.get(card["id"]) is None


async def test_a_claim_that_dies_in_transit_leaves_the_mark_for_recovery(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card())
    board.claim_error = BoardError(503, "board is down")
    board.post_event("card.posted", card)
    with pytest.raises(BoardError):
        await sub.consume()
    assert queue.status_of(card["id"]) == "claiming"
    board.claim_error = None
    board.cards[card["id"]].update(status="in_progress", claimant="tony")  # the claim had landed
    await sub.catch_up()
    assert queue.status_of(card["id"]) == "queued"


async def test_catch_up_pages_so_cards_past_the_first_page_are_not_missed(queue, monkeypatch):
    monkeypatch.setattr("awm.representative.subscriber.PAGE", 3)
    sub, board, _ = build(queue)
    posted = [board.add(make_card()) for _ in range(8)]
    messages = [board.add(make_card(kind="message")) for _ in range(7)]
    for _ in range(4):
        board.add(make_card(status="in_progress", claimant="tony"))
    await sub.catch_up()
    queued = {r["card_id"] for r in queue.list(limit=200)}
    assert queued >= {c["id"] for c in posted} | {c["id"] for c in messages}
    assert sorted({c["offset"] for c in board.list_calls if c.get("status") == "posted"
                   and c.get("kind") == "request"}) == [0, 3, 6]


async def test_one_card_failing_in_a_catch_up_does_not_stop_the_others(queue):
    sub, board, _ = build(queue)
    bad = board.add(make_card())
    good = board.add(make_card())
    real = board.claim

    async def claim(card_id):
        if card_id == bad["id"]:
            raise BoardError(500, "boom")
        return await real(card_id)

    board.claim = claim
    assert await sub.catch_up() == 1
    assert queue.get(good["id"])["status"] == "queued"
    assert queue.status_of(bad["id"]) == "claiming"


async def test_a_failing_catch_up_still_lets_the_stream_start(queue, monkeypatch):
    monkeypatch.setattr("awm.representative.subscriber.RESTART_DELAY_S", 0.01)
    sub, board, _ = build(queue)
    card = board.add(make_card())
    board.post_event("card.posted", card)
    board.list_error = BoardError(503, "list is down")
    task = asyncio.create_task(sub.run())
    for _ in range(100):
        if queue.get(card["id"]):
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue.get(card["id"]) is not None  # the stream delivered it


async def test_repeated_failures_back_off_instead_of_spinning(queue, monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= 5:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr("awm.representative.subscriber.asyncio.sleep", fake_sleep)
    sub, board, _ = build(queue)
    board.list_error = BoardError(503, "down")

    async def broken(last_event_id=None, *, on_state=None):
        raise BoardError(502, "gateway")
        yield  # pragma: no cover

    board.stream = broken
    with pytest.raises(asyncio.CancelledError):
        await sub._stream_loop()
    assert sleeps == [2.0, 4.0, 8.0, 16.0, 32.0]


async def test_a_card_missing_from_the_board_becomes_gone(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card())
    await sub.catch_up()
    del board.cards[card["id"]]
    await sub.catch_up()
    assert queue.get(card["id"])["status"] == "gone"
    assert queue.active_ids() == []


async def test_a_card_re_addressed_away_becomes_gone_in_a_catch_up_and_on_an_event(queue):
    sub, board, _ = build(queue)
    a, b = board.add(make_card()), board.add(make_card())
    await sub.catch_up()
    board.cards[a["id"]]["recipient"] = "gamma"
    await sub.catch_up()
    assert queue.get(a["id"])["status"] == "gone"
    board.post_event("card.moved", {**board.cards[b["id"]], "recipient": "gamma"})
    await sub.consume()
    assert queue.get(b["id"])["status"] == "gone"


async def test_a_get_that_fails_for_another_reason_leaves_the_row_alone(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card())
    await sub.catch_up()
    board.get_errors[card["id"]] = BoardError(503, "down")
    assert await sub.catch_up() == 1
    assert queue.get(card["id"])["status"] == "queued"


async def test_the_subscriber_knows_whether_the_stream_is_attached(queue):
    sub, board, _ = build(queue)
    seen = []
    real = board.stream

    async def watching(last_event_id=None, *, on_state=None):
        async for event in real(last_event_id, on_state=on_state):
            seen.append(sub.attached)
            yield event

    board.stream = watching
    board.post_event("card.posted", board.add(make_card()))
    assert sub.attached is False
    await sub.consume()
    assert seen == [True]
    assert sub.attached is False  # the stream ended


async def test_a_catch_up_finishes_rows_whose_cards_finished_while_away(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card())
    await sub.catch_up()
    board.cards[card["id"]]["status"] = "done"
    await sub.catch_up()
    assert queue.get(card["id"])["status"] == "done"


async def test_a_catch_up_queues_only_recent_messages(queue):
    sub, board, _ = build(queue, message_backlog_s=86400.0, clock=lambda: 1_800_000_000.0)
    fresh = board.add(make_card(kind="message", created_at="2027-01-15T07:59:00+00:00"))
    stale = board.add(make_card(kind="message", created_at="2026-10-09T12:00:00+00:00"))
    await sub.catch_up()
    assert [r["card_id"] for r in queue.list()] == [fresh["id"]]
    assert stale["id"] not in [r["card_id"] for r in queue.list()]
    assert board.claims == []


async def test_run_survives_a_failing_stream_and_retries(queue, monkeypatch):
    monkeypatch.setattr("awm.representative.subscriber.RESTART_DELAY_S", 0.01)
    sub, board, _ = build(queue)
    card = board.add(make_card())
    board.post_event("card.posted", card)
    board.claim_error = BoardError(500, "boom")
    task = asyncio.create_task(sub.run())
    await asyncio.sleep(0.05)
    assert not task.done()
    assert sub.last_error
    board.claim_error = None
    for _ in range(100):
        if queue.get(card["id"]):
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue.get(card["id"]) is not None


async def test_the_real_client_drives_the_subscriber(queue):
    """BoardClient against a mock door: the filters and the claim path are the real ones."""
    card = make_card()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/board/stream":
            body = f"id: 1\nevent: card.posted\ndata: {json.dumps(card)}\n\n".encode()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        if path.endswith("/claim"):
            return httpx.Response(200, json={**card, "status": "in_progress", "claimant": "tony"})
        if path == "/board/cards":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"error": "nope"})

    client = BoardClient("http://board/", "tok", transport=httpx.MockTransport(handler),
                         backoff_start=0.01, backoff_max=0.02)
    sub = Subscriber(queue, client, "tony")
    task = asyncio.create_task(sub.consume())
    for _ in range(100):
        if queue.get(card["id"]):
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()
    assert queue.get(card["id"])["status"] == "queued"
    assert any(r.url.path == f"/board/cards/{card['id']}/claim" for r in seen)
    assert all(r.headers["authorization"] == "Bearer tok" for r in seen)
