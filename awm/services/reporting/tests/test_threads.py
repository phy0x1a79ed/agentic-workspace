"""Thread messages: the card's session hears of them, or the representative does."""

from __future__ import annotations

import pytest

from awm.persistence import databases
from awm.reporting import hub_adapter, store
from awm.reporting.subscriber import Subscriber, thread_text

from stubs import FakeBoard, make_card, session_row
from test_adapter import runtime  # noqa: F401 — the fixture

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class Lines:
    """The PTY route: records each line, and fails the next ``fail`` sends."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = 0

    def __call__(self, job: str, text: str) -> None:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("job is waiting on a dialog")
        self.sent.append((job, text))


def build(queue, swarm="tony"):
    board, lines, woke = FakeBoard(swarm), Lines(), []
    sub = Subscriber(queue, board, swarm, on_queued=lambda: woke.append(1), send=lines)
    return sub, board, lines, woke


def assigned(queue, board, job="j1", **over):
    card = board.add(make_card(**over))
    queue.enqueue(card)
    queue.assign(card["id"], "worker", job)
    return card


async def test_a_message_wakes_the_session_the_card_was_assigned_to(queue):
    sub, board, lines, woke = build(queue)
    card = assigned(queue, board)
    board.post_event("card.message", board.say(card))
    await sub.consume()
    assert lines.sent == [("j1", thread_text(card["id"]))]
    assert card["id"] in lines.sent[0][1] and "hello" not in lines.sent[0][1]
    assert queue.thread_seq(card["id"]) == 1
    assert woke == [] and queue.get(card["id"])["status"] == "assigned"


async def test_a_message_from_this_swarm_and_a_replay_wake_nobody(queue):
    sub, board, lines, _ = build(queue)
    card = assigned(queue, board)
    board.post_event("card.message", board.say(card, swarm="tony"))
    theirs = board.say(card)
    board.post_event("card.message", theirs)
    board.post_event("card.message", theirs)
    await sub.consume()
    assert len(lines.sent) == 1 and queue.thread_seq(card["id"]) == 2


async def test_a_message_on_a_card_with_no_session_goes_back_to_the_representative(queue):
    sub, board, lines, woke = build(queue)
    card = board.add(make_card(kind="message"))
    queue.enqueue(card)
    queue.assign(card["id"], "by hand")
    card["status"] = "done"
    board.post_event("card.message", board.say(card))
    await sub.consume()
    row = queue.get(card["id"])
    assert (row["status"], row["thread_open"], row["assigned_to"]) == ("queued", 1, None)
    assert woke == [1] and lines.sent == []
    await sub.catch_up()
    board.post_event("card.moved", card)
    await sub.consume()
    assert queue.get(card["id"])["status"] == "queued", "the finished card stays open for its thread"
    queue.assign(card["id"], "worker", "j2")
    assert (queue.get(card["id"])["status"], queue.get(card["id"])["thread_open"]) == ("done", 0)


async def test_a_message_on_a_card_this_swarm_sent_reaches_the_representative(queue):
    sub, board, _, woke = build(queue)
    card = board.add(make_card(recipient="beta", sender={"swarm": "tony", "principal": "t"}))
    board.post_event("card.message", board.say(card))
    await sub.consume()
    assert (queue.get(card["id"])["status"], queue.get(card["id"])["sender"]) == ("queued", "tony")
    assert woke == [1]
    card["status"] = "done"
    board.post_event("card.moved", card)
    await sub.consume()
    await sub.catch_up()
    assert queue.get(card["id"])["status"] == "queued", "a card we sent is not made gone while open"


async def test_a_message_on_someone_elses_card_is_ignored(queue):
    sub, board, lines, woke = build(queue)
    card = board.add(make_card(recipient="gamma"))
    board.post_event("card.message", board.say(card))
    await sub.consume()
    assert queue.get(card["id"]) is None and lines.sent == [] and woke == []
    assert queue.thread_seq(card["id"]) == 0


async def test_a_missed_line_is_retried_once_by_the_catch_up_it_asks_for(queue):
    sub, board, lines, woke = build(queue)
    card = assigned(queue, board)
    lines.fail = 1
    board.post_event("card.message", board.say(card))
    await sub.consume()
    assert queue.thread_seq(card["id"]) == 0 and sub._kick.is_set()
    await sub.catch_up()
    assert lines.sent == [("j1", thread_text(card["id"]))]
    assert queue.thread_seq(card["id"]) == 1 and woke == []


async def test_a_session_that_misses_twice_hands_the_card_back(queue):
    sub, board, lines, woke = build(queue)
    card = assigned(queue, board)
    lines.fail = 2
    board.post_event("card.message", board.say(card))
    await sub.consume()
    await sub.catch_up()
    row = queue.get(card["id"])
    assert (row["status"], row["assigned_job"]) == ("queued", None)
    assert woke == [1] and queue.thread_seq(card["id"]) == 1


async def test_a_catch_up_routes_what_the_stream_missed_once(queue):
    sub, board, lines, _ = build(queue)
    card = assigned(queue, board)
    board.say(card)
    board.say(card, swarm="tony")
    await sub.catch_up()
    await sub.catch_up()
    assert lines.sent == [("j1", thread_text(card["id"]))]
    assert queue.thread_seq(card["id"]) == 2


async def test_a_catch_up_over_only_our_own_messages_moves_the_cursor_silently(queue):
    sub, board, lines, woke = build(queue)
    card = assigned(queue, board)
    board.say(card, swarm="tony")
    await sub.catch_up()
    assert lines.sent == [] and woke == [] and queue.thread_seq(card["id"]) == 1


async def test_a_board_without_threads_costs_no_message_reads(queue):
    sub, board, _, _ = build(queue)
    assigned(queue, board)
    await sub.catch_up()
    assert board.message_reads == []


def test_a_reopened_card_forgets_its_session(queue):
    card = make_card()
    queue.enqueue(card)
    queue.assign(card["id"], "worker", "j1")
    queue.mark(card["id"], "done")
    assert queue.get(card["id"])["assigned_job"] == "j1", "the session outlives the status"
    queue.enqueue(card, reopen=True)
    assert queue.get(card["id"])["assigned_job"] is None


def test_the_thread_cursor_only_moves_forward(queue):
    queue.set_thread_seq("c", 3)
    queue.set_thread_seq("c", 2)
    assert queue.thread_seq("c") == 3 and queue.thread_seqs() == {"c": 3}


def test_a_v4_queue_gains_the_thread_columns(tmp_path):
    path = tmp_path / "old.db"
    databases.init_db_at(path, store._QUEUE_SCHEMA_V4 + store._TASKS_SCHEMA, schema_version=4)
    conn = databases.get_connection_at(path)
    conn.execute("INSERT INTO cards (card_id, kind, title, status, arrival, updated)"
                 " VALUES ('c', 'request', 't', 'queued', 1.0, 1.0)")
    conn.commit()
    conn.close()
    queue = store.Queue(path)
    assert queue.get("c")["assigned_job"] is None and queue.get("c")["thread_open"] == 0
    queue.set_thread_seq("c", 1)
    assert queue.thread_seq("c") == 1


# -- queue_assign with a job ---------------------------------------------------


async def test_assign_records_a_job_the_representative_started(runtime, queue):  # noqa: F811
    runtime.loop.cx.rows.append(session_row("delegate", job="d1", parent="r1"))
    card = make_card()
    queue.enqueue(card)
    out = await hub_adapter.assign({"card_id": card["id"], "agent": "worker", "job": "d1"})
    assert out["ok"] is True and out["card"]["assigned_job"] == "d1"


@pytest.mark.parametrize("row,why", [
    (None, "not a live session"),
    (session_row("delegate", job="d1", parent="r1", state="gone"), "not a live session"),
    (session_row("delegate", job="d1", parent="someone"), "not started by the representative"),
])
async def test_assign_refuses_a_job_the_representative_did_not_start(runtime, queue, row, why):  # noqa: F811
    if row:
        runtime.loop.cx.rows.append(row)
    card = make_card()
    queue.enqueue(card)
    out = await hub_adapter.assign({"card_id": card["id"], "agent": "worker", "job": "d1"})
    assert out["ok"] is False and why in out["error"]
    assert queue.get(card["id"])["status"] == "queued"


async def test_assign_refuses_a_malformed_job_and_an_unreachable_cx(runtime, queue):  # noqa: F811
    card = make_card()
    queue.enqueue(card)
    out = await hub_adapter.assign({"card_id": card["id"], "agent": "w", "job": "../x"})
    assert out["ok"] is False and "job id" in out["error"]
    runtime.loop.cx.unavailable = True
    out = await hub_adapter.assign({"card_id": card["id"], "agent": "w", "job": "d1"})
    assert out["ok"] is False
