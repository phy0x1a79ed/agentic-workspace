"""The board subscriber: intake into Proposed, one message to Tony per card, the cursor and the catch-up."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from awm.board.client import RESYNC, BoardClient, BoardError
from awm.reporting.subscriber import Subscriber

from stubs import FakeBoard, make_card

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class Told:
    """Tony's direct messages, and a switch that makes sending fail."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.error: Exception | None = None

    async def __call__(self, text: str) -> None:
        if self.error is not None:
            raise self.error
        self.texts.append(text)


def build(queue, board=None, swarm="tony", **kw):
    board = board or FakeBoard(swarm)
    told = Told()
    sub = Subscriber(queue, board, swarm, dm=told, **kw)
    return sub, board, told


async def test_a_posted_request_is_queued_as_proposed_and_never_claimed(queue):
    sub, board, told = build(queue)
    card = board.add(make_card(title="Rig the thing", body="please do it", note_id="NOTE1234abcd"))
    board.post_event("card.posted", card)
    await sub.consume()
    row = queue.get(card["id"])
    assert (row["status"], row["sender"], row["title"]) == ("proposed", "beta", "Rig the thing")
    assert not hasattr(board, "claims")
    assert queue.cursor() == 1
    assert len(told.texts) == 1
    text = told.texts[0]
    assert "Rig the thing" in text and "From: beta/op" in text and "To: tony" in text
    assert "please do it" in text and f"claim task {card['id'][:8]}" in text


async def test_a_reply_card_is_left_to_its_thread_message(queue):
    sub, board, told = build(queue)
    card = board.add(make_card(kind="message", reply_to="a" * 32))
    board.post_event("card.posted", card)
    await sub.consume()
    await sub.catch_up()
    assert queue.get(card["id"]) is None and told.texts == []


async def test_the_message_carries_the_trilium_link_the_agent_and_the_project(queue, monkeypatch):
    monkeypatch.setenv("AWM_REPORTING_TRILIUM_URL", "https://vault.example/trilium")
    sub, board, told = build(queue)
    card = board.add(make_card(note_id="AbCdEfGhIjKl", agent="muse-writer", project="factorio"))
    board.post_event("card.posted", card)
    await sub.consume()
    text = told.texts[0]
    assert "From: beta/op (muse-writer)" in text and "Project: factorio" in text
    assert text.splitlines()[-1] == f"claim task {card['id'][:8]} https://vault.example/trilium/#root/AbCdEfGhIjKl"


async def test_the_excerpt_is_cut_to_about_three_hundred_characters(queue):
    sub, board, told = build(queue)
    board.post_event("card.posted", board.add(make_card(body="word " * 400)))
    await sub.consume()
    body_line = told.texts[0].split("\n\n")[1]
    assert 290 <= len(body_line) <= 305 and body_line.endswith("…")


async def test_a_replayed_event_tells_tony_once(queue):
    sub, board, told = build(queue)
    card = board.add(make_card())
    board.post_event("card.posted", card)
    await sub.consume()
    queue.set_cursor(0, force=True)  # a lost cursor write: the event replays
    await sub.consume()
    await sub.catch_up()
    assert len(told.texts) == 1


async def test_a_message_that_does_not_send_is_sent_by_the_next_catch_up(queue):
    sub, board, told = build(queue)
    card = board.add(make_card())
    board.post_event("card.posted", card)
    told.error = OSError("discord is down")
    await sub.consume()
    assert queue.status_of(card["id"]) == "proposed" and queue.needs_dm(card["id"])
    assert queue.cursor() == 1 and "discord is down" in sub.last_error
    told.error = None
    await sub.catch_up()
    assert len(told.texts) == 1 and not queue.needs_dm(card["id"])


async def test_a_message_card_is_queued_like_a_request_and_says_so(queue):
    sub, board, told = build(queue)
    card = board.add(make_card(kind="message", title="fyi"))
    board.post_event("card.posted", card)
    await sub.consume()
    assert queue.get(card["id"])["kind"] == "message"
    assert "[message]" in told.texts[0]


async def test_a_card_to_open_and_a_card_this_swarm_posted_are_told_too(queue):
    sub, board, told = build(queue)
    to_open = board.add(make_card(recipient="open"))
    ours = board.add(make_card(recipient="gamma", sender={"swarm": "tony", "principal": "agent", "party": "p"},
                               agent="opal"))
    for card in (to_open, ours):
        board.post_event("card.posted", card)
    await sub.consume()
    assert len(told.texts) == 2
    assert f"claim task {to_open['id'][:8]}" in told.texts[0]
    assert "claim task" not in told.texts[1] and f"ref {ours['id'][:8]}" in told.texts[1]
    assert "From: tony/agent (opal)" in told.texts[1]


async def test_cards_between_other_swarms_are_ignored(queue):
    sub, board, told = build(queue)
    mine = board.add(make_card())
    theirs = board.add(make_card(recipient="gamma"))
    for card in (mine, theirs):
        board.post_event("card.posted", card)
    await sub.consume()
    assert queue.active_ids() == [mine["id"]]
    assert queue.cursor() == 2  # ignored events still advance the cursor


async def test_the_swarm_is_the_nodes_and_not_tony(queue):
    sub, board, told = build(queue, swarm="mock")
    for_mock = board.add(make_card(recipient="mock"))
    for_tony = board.add(make_card(recipient="tony"))
    board.post_event("card.posted", for_tony)
    board.post_event("card.posted", for_mock)
    await sub.consume()
    assert queue.active_ids() == [for_mock["id"]]


@pytest.mark.parametrize("event,state,final", [("card.completed", "done", "done"),
                                               ("card.moved", "rejected", "rejected"),
                                               ("card.failed", "rejected", "rejected")])
async def test_a_finished_card_marks_its_row_and_tells_the_task_hook(queue, event, state, final):
    closed = []

    async def on_closed(card_id, st):
        closed.append((card_id, st))

    sub, board, _ = build(queue, on_closed=on_closed)
    card = board.add(make_card())
    board.post_event("card.posted", card)
    board.post_event(event, {**card, "state": state})
    await sub.consume()
    assert queue.get(card["id"])["status"] == final
    assert closed == [(card["id"], state)]


async def test_an_old_release_event_with_only_a_status_still_finishes_the_card(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card())
    board.post_event("card.posted", card)
    old = {k: v for k, v in card.items() if k != "state"}
    board.post_event("card.completed", {**old, "status": "done"})
    await sub.consume()
    assert queue.get(card["id"])["status"] == "done"


async def test_a_finished_card_the_queue_never_held_is_ignored(queue):
    sub, board, _ = build(queue)
    board.post_event("card.completed", board.add(make_card(state="done", status="done")))
    await sub.consume()
    assert queue.counts()["done"] == 0


async def test_an_open_card_claimed_by_another_swarm_is_no_longer_ours(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card(recipient="open"))
    board.post_event("card.posted", card)
    board.post_event("card.claimed", {**card, "state": "planned", "claimant": "beta"})
    await sub.consume()
    assert queue.get(card["id"])["status"] == "gone"


async def test_a_claim_by_this_swarm_leaves_the_row_alone(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card(recipient="open"))
    board.post_event("card.posted", card)
    await sub.consume()
    queue.take(card, "agent", "job1")
    board.post_event("card.claimed", {**card, "state": "planned", "claimant": "tony"})
    await sub.consume()
    assert queue.get(card["id"])["status"] == "assigned"


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
    assert queue.status_of(second["id"]) is None


async def test_a_restart_resumes_the_stream_from_the_saved_cursor(queue):
    sub, board, _ = build(queue)
    board.post_event("card.posted", board.add(make_card()))
    await sub.consume()
    board.post_event("card.posted", board.add(make_card()))
    sub2, _, _ = build(queue, board)
    await sub2.consume()
    assert board.stream_starts == [None, 1]
    assert len(queue.active_ids()) == 2


async def test_a_card_dragged_back_to_proposed_is_reopened_and_told_again(queue):
    sub, board, told = build(queue)
    card = board.add(make_card())
    board.post_event("card.posted", card)
    await sub.consume()
    queue.mark(card["id"], "done")
    board.post_event("card.moved", {**card, "state": "proposed"})
    await sub.consume()
    assert queue.get(card["id"])["status"] == "proposed"
    assert len(told.texts) == 2


async def test_a_resync_catches_up_from_the_board_then_moves_the_cursor(queue):
    sub, board, told = build(queue)
    missed = board.add(make_card())
    board.post_event(RESYNC, {"latest": 40, "oldest": 30}, event_id=40)
    await sub.consume()
    assert queue.get(missed["id"]) is not None and len(told.texts) == 1
    assert queue.cursor() == 40


async def test_a_catch_up_reads_proposed_cards_to_us_to_open_and_from_us(queue):
    sub, board, told = build(queue)
    cards = [board.add(make_card()) for _ in range(2)]
    cards.append(board.add(make_card(recipient="open")))
    cards.append(board.add(make_card(recipient="gamma", sender={"swarm": "tony", "principal": "a", "party": "p"})))
    held = board.add(make_card(state="planned", claimant="beta"))
    await sub.catch_up()
    assert sorted(queue.active_ids()) == sorted(c["id"] for c in cards)
    assert queue.get(held["id"]) is None and len(told.texts) == 4


async def test_a_catch_up_does_not_tell_tony_about_old_cards(queue):
    sub, board, told = build(queue, message_backlog_s=86400.0, clock=lambda: 1_800_000_000.0)
    fresh = board.add(make_card(created_at="2027-01-15T07:59:00+00:00"))
    stale = board.add(make_card(created_at="2026-10-09T12:00:00+00:00"))
    await sub.catch_up()
    assert queue.has(stale["id"]) and not queue.needs_dm(stale["id"])
    assert len(told.texts) == 1 and fresh["id"][:8] in told.texts[0]


async def test_catch_up_pages_so_cards_past_the_first_page_are_not_missed(queue, monkeypatch):
    monkeypatch.setattr("awm.reporting.subscriber.PAGE", 3)
    sub, board, _ = build(queue)
    posted = [board.add(make_card()) for _ in range(8)]
    for _ in range(4):
        board.add(make_card(state="planned", claimant="tony"))
    await sub.catch_up()
    assert set(queue.active_ids()) == {c["id"] for c in posted}
    assert sorted({c["offset"] for c in board.list_calls
                   if c.get("state") == "proposed" and c.get("recipient") == "tony"}) == [0, 3, 6]


async def test_one_card_failing_in_a_catch_up_does_not_stop_the_others(queue, monkeypatch):
    sub, board, _ = build(queue)
    bad = board.add(make_card())
    good = board.add(make_card())
    real = sub._intake

    async def intake(card, **kw):
        if card["id"] == bad["id"]:
            raise BoardError(500, "boom")
        await real(card, **kw)

    monkeypatch.setattr(sub, "_intake", intake)
    assert await sub.catch_up() == 1
    assert queue.has(good["id"]) and not queue.has(bad["id"])


async def test_a_failing_catch_up_still_lets_the_stream_start(queue, monkeypatch):
    monkeypatch.setattr("awm.reporting.subscriber.RESTART_DELAY_S", 0.01)
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

    monkeypatch.setattr("awm.reporting.subscriber.asyncio.sleep", fake_sleep)
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


async def test_a_card_this_swarm_posted_stays_after_the_recipient_changes(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card(recipient="gamma", sender={"swarm": "tony", "principal": "a", "party": "p"}))
    await sub.catch_up()
    board.cards[card["id"]]["recipient"] = "delta"
    await sub.catch_up()
    assert queue.get(card["id"])["status"] == "proposed"


async def test_a_get_that_fails_for_another_reason_leaves_the_row_alone(queue):
    sub, board, _ = build(queue)
    card = board.add(make_card())
    await sub.catch_up()
    board.get_errors[card["id"]] = BoardError(503, "down")
    assert await sub.catch_up() == 1
    assert queue.get(card["id"])["status"] == "proposed"


async def test_a_catch_up_finishes_rows_whose_cards_finished_while_away(queue):
    closed = []

    async def on_closed(card_id, state):
        closed.append(state)

    sub, board, _ = build(queue, on_closed=on_closed)
    card = board.add(make_card())
    await sub.catch_up()
    board.cards[card["id"]].update(state="rejected", status="failed")
    await sub.catch_up()
    assert queue.get(card["id"])["status"] == "rejected" and closed == ["rejected"]


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


async def test_a_board_error_on_the_stream_is_left_for_the_catch_up_and_the_cursor_advances(queue, monkeypatch):
    sub, board, _ = build(queue)
    card = board.add(make_card())
    board.post_event("card.posted", card)

    async def boom(*a, **k):
        raise BoardError(503, "board is down")

    real = sub._intake
    monkeypatch.setattr(sub, "_intake", boom)
    await sub.consume()  # does not raise: the event is not retried for ever
    assert queue.cursor() == 1 and "503" in sub.last_error
    monkeypatch.setattr(sub, "_intake", real)
    await sub.catch_up()
    assert queue.status_of(card["id"]) == "proposed"


async def test_a_network_error_on_an_event_is_handled_the_same_way(queue, monkeypatch):
    sub, board, _ = build(queue)
    board.post_event("card.posted", board.add(make_card()))

    async def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(sub, "_intake", boom)
    await sub.consume()
    assert queue.cursor() == 1


async def test_a_failed_event_asks_for_a_prompt_catch_up(queue, monkeypatch):
    monkeypatch.setattr("awm.reporting.subscriber.KICK_DELAY_S", 0.01)
    sub, board, told = build(queue, catchup_s=3600.0)
    card = board.add(make_card())
    board.post_event("card.posted", card)
    told.error = OSError("down")
    await sub.consume()
    told.error = None
    task = asyncio.create_task(sub._periodic())
    for _ in range(100):
        if told.texts:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(told.texts) == 1


async def test_the_real_client_drives_the_subscriber(queue):
    """BoardClient against a mock door: the filters are the real ones, and nothing is claimed."""
    card = make_card()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/board/stream":
            body = f"id: 1\nevent: card.posted\ndata: {json.dumps(card)}\n\n".encode()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        if path == "/board/cards":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"error": "nope"})

    client = BoardClient("http://board/", "tok", transport=httpx.MockTransport(handler),
                         backoff_start=0.01, backoff_max=0.02)
    sub, _, told = build(queue, board=client)
    task = asyncio.create_task(sub.consume())
    for _ in range(100):
        if queue.get(card["id"]):
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()
    assert queue.get(card["id"])["status"] == "proposed" and len(told.texts) == 1
    assert not any(r.url.path.endswith("/claim") for r in seen)
    assert all(r.headers["authorization"] == "Bearer tok" for r in seen)
