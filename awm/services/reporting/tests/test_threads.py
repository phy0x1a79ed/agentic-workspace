"""Thread messages: the card's session hears of them, or Tony does. Watchers hear of every event."""

from __future__ import annotations

import pytest

from awm.reporting.intake import wake_text
from awm.reporting.subscriber import Subscriber

from stubs import FakeBoard, make_card

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


class Lines:
    """The PTY route: records each line, and fails the next ``fail`` sends to a job."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail: dict[str, int] = {}

    def __call__(self, job: str, text: str) -> None:
        if self.fail.get(job):
            self.fail[job] -= 1
            raise RuntimeError("job is waiting on a dialog")
        self.sent.append((job, text))


class Told:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.error: Exception | None = None

    async def __call__(self, text: str) -> None:
        if self.error is not None:
            raise self.error
        self.texts.append(text)


def build(queue, swarm="tony"):
    board, lines, told = FakeBoard(swarm), Lines(), Told()
    sub = Subscriber(queue, board, swarm, dm=told, send=lines)
    return sub, board, lines, told


def assigned(queue, board, job="j1", **over):
    card = board.add(make_card(**over))
    queue.enqueue(card, quiet=True)
    queue.take(card, "worker", job)
    return card


def proposed(queue, board, **over):
    card = board.add(make_card(**over))
    queue.enqueue(card, quiet=True)
    return card


async def test_a_message_wakes_the_session_the_card_was_assigned_to(queue):
    sub, board, lines, told = build(queue)
    card = assigned(queue, board)
    board.post_event("card.message", board.say(card, "secret text", agent="muse"))
    await sub.consume()
    assert lines.sent == [("j1", wake_text(card["id"], {"sender": {"swarm": "beta", "principal": "op"},
                                                       "agent": "muse"}))]
    line = lines.sent[0][1]
    assert card["id"] in line and "beta/op (muse)" in line and "secret text" not in line
    assert queue.thread_seq(card["id"]) == 1 and told.texts == []


async def test_a_message_before_a_claim_tells_nobody(queue):
    sub, board, lines, told = build(queue)
    card = proposed(queue, board, title="Rig the thing")
    board.post_event("card.message", board.say(card, "private words", agent="muse"))
    await sub.consume()
    assert lines.sent == [] and told.texts == []
    assert queue.thread_seq(card["id"]) == 1 and queue.get(card["id"])["status"] == "proposed"


async def test_a_message_from_this_swarm_and_a_replay_tell_nobody(queue):
    sub, board, lines, told = build(queue)
    card = assigned(queue, board)
    board.post_event("card.message", board.say(card, swarm="tony"))
    theirs = board.say(card)
    board.post_event("card.message", theirs)
    board.post_event("card.message", theirs)
    await sub.consume()
    assert len(lines.sent) == 1 and told.texts == [] and queue.thread_seq(card["id"]) == 2


async def test_a_message_on_a_card_this_swarm_sent_tells_nobody(queue):
    sub, board, lines, told = build(queue)
    card = board.add(make_card(recipient="beta", sender={"swarm": "tony", "principal": "t", "party": "p"}))
    board.post_event("card.message", board.say(card, swarm="beta"))
    await sub.consume()
    assert told.texts == [] and queue.thread_seq(card["id"]) == 1


async def test_a_message_on_a_card_to_open_tells_nobody(queue):
    sub, board, lines, told = build(queue)
    card = board.add(make_card(recipient="open"))
    board.post_event("card.message", board.say(card))
    await sub.consume()
    assert told.texts == [] and queue.thread_seq(card["id"]) == 1


async def test_a_message_on_someone_elses_card_is_ignored(queue):
    sub, board, lines, told = build(queue)
    card = board.add(make_card(recipient="gamma"))
    board.post_event("card.message", board.say(card))
    await sub.consume()
    assert lines.sent == [] and told.texts == [] and queue.thread_seq(card["id"]) == 0


async def test_a_missed_line_is_retried_once_by_the_catch_up_it_asks_for(queue):
    sub, board, lines, told = build(queue)
    card = assigned(queue, board)
    lines.fail["j1"] = 1
    board.post_event("card.message", board.say(card))
    await sub.consume()
    assert queue.thread_seq(card["id"]) == 0 and sub._kick.is_set() and told.texts == []
    await sub.catch_up()
    assert [j for j, _ in lines.sent] == ["j1"]
    assert queue.thread_seq(card["id"]) == 1 and told.texts == []


async def test_a_session_that_misses_twice_leaves_the_message_on_the_thread(queue):
    sub, board, lines, told = build(queue)
    card = assigned(queue, board)
    lines.fail["j1"] = 2
    board.post_event("card.message", board.say(card))
    await sub.consume()
    await sub.catch_up()
    assert lines.sent == [] and told.texts == []
    assert queue.thread_seq(card["id"]) == 1


async def test_a_claim_by_a_session_with_no_job_tells_nobody(queue):
    sub, board, lines, told = build(queue)
    card = board.add(make_card())
    queue.take(card, "terminal-agent", None)
    board.post_event("card.message", board.say(card))
    await sub.consume()
    assert lines.sent == [] and told.texts == [] and queue.thread_seq(card["id"]) == 1


async def test_a_catch_up_routes_what_the_stream_missed_once(queue):
    sub, board, lines, told = build(queue)
    card = assigned(queue, board)
    board.say(card)
    board.say(card, swarm="tony")
    await sub.catch_up()
    await sub.catch_up()
    assert [j for j, _ in lines.sent] == ["j1"]
    assert queue.thread_seq(card["id"]) == 2


async def test_a_catch_up_over_only_our_own_messages_moves_the_cursor_silently(queue):
    sub, board, lines, told = build(queue)
    card = assigned(queue, board)
    board.say(card, swarm="tony")
    await sub.catch_up()
    assert lines.sent == [] and told.texts == [] and queue.thread_seq(card["id"]) == 1


async def test_a_catch_up_treats_an_old_thread_as_history(queue):
    sub, board, lines, told = build(queue, swarm="tony")
    sub._backlog_s = 86400.0
    sub._clock = lambda: 1_800_000_000.0
    old = board.add(make_card(created_at="2026-01-01T00:00:00+00:00"))
    board.say(old)
    finished = board.add(make_card(state="done"))
    board.say(finished)
    await sub.catch_up()
    assert told.texts == [] and lines.sent == []
    assert queue.thread_seq(old["id"]) == 1 and queue.thread_seq(finished["id"]) == 1


async def test_a_board_without_threads_costs_no_message_reads(queue):
    sub, board, _, _ = build(queue)
    assigned(queue, board)
    await sub.catch_up()
    assert board.message_reads == []


# -- watchers --------------------------------------------------------------------


async def test_a_watcher_gets_one_line_per_event_naming_the_card_and_who_acted(queue):
    sub, board, lines, told = build(queue)
    card = board.add(make_card(sender={"swarm": "tony", "principal": "a", "party": "p"}, recipient="beta"))
    queue.watch("w1", card["id"])
    board.post_event("card.claimed", {**card, "state": "planned", "claimant": "beta", "owner": "beta/shaula/jo"})
    board.post_event("card.moved", {**card, "state": "active", "claimant": "beta"})
    board.post_event("card.message", board.say(card, "the actual words", agent="jo", swarm="beta"))
    board.post_event("card.completed", {**card, "state": "done", "claimant": "beta"})
    await sub.consume()
    texts = [t for _, t in lines.sent]
    assert all(job == "w1" for job, _ in lines.sent)
    assert texts[0] == f"Board card {card['id']} claimed by beta/shaula/jo"
    assert texts[1] == f"Board card {card['id']} moved to active"
    assert texts[2].startswith(f"Board card {card['id']} has a new message from beta/op (jo)")
    assert texts[3] == f"Board card {card['id']} is done"
    assert not any("actual words" in t for t in texts)


async def test_the_plan_messages_the_door_writes_read_as_plan_events(queue):
    sub, board, lines, told = build(queue)
    card = board.add(make_card(sender={"swarm": "tony", "principal": "a", "party": "p"}, recipient="beta"))
    queue.watch("w1", card["id"])
    board.post_event("card.message", board.say(card, "plan submitted", swarm="beta", agent="jo"))
    board.post_event("card.message", board.say(card, "plan approved", swarm="beta"))
    await sub.consume()
    assert [t.split(" by ")[0] for _, t in lines.sent] == [
        f"Board card {card['id']} plan submitted", f"Board card {card['id']} plan approved"]


async def test_posted_by_me_watches_every_card_an_agent_posted(queue):
    sub, board, lines, _ = build(queue)
    mine = board.add(make_card(recipient="beta", agent="muse",
                               sender={"swarm": "tony", "principal": "a", "party": "p"}))
    other = board.add(make_card(recipient="beta", agent="someone-else",
                                sender={"swarm": "tony", "principal": "a", "party": "p"}))
    queue.watch("w1", "posted:muse")
    for card in (mine, other):
        board.post_event("card.moved", {**card, "state": "active", "claimant": "beta"})
    await sub.consume()
    assert [t for _, t in lines.sent] == [f"Board card {mine['id']} moved to active"]


async def test_the_assigned_job_is_not_told_twice_of_a_message_it_watches(queue):
    sub, board, lines, _ = build(queue)
    card = assigned(queue, board)
    queue.watch("j1", card["id"])
    queue.watch("w2", card["id"])
    board.post_event("card.message", board.say(card))
    await sub.consume()
    assert sorted(j for j, _ in lines.sent) == ["j1", "w2"]


async def test_a_watcher_that_cannot_take_a_line_does_not_stop_the_others(queue):
    sub, board, lines, _ = build(queue)
    card = board.add(make_card())
    queue.watch("a", card["id"])
    queue.watch("b", card["id"])
    lines.fail["a"] = 5
    board.post_event("card.moved", {**card, "state": "active", "claimant": "tony"})
    await sub.consume()
    assert [j for j, _ in lines.sent] == ["b"]
    assert queue.cursor() == 1


async def test_a_card_dragged_to_rejected_tells_the_session_that_holds_it(queue):
    sub, board, lines, told = build(queue)
    card = assigned(queue, board)
    board.post_event("card.moved", {**card, "state": "rejected"})
    await sub.consume()
    assert queue.get(card["id"])["status"] == "rejected"
    assert lines.sent == [("j1", f"Board card {card['id']} is rejected")]


async def test_a_new_card_in_a_watched_project_or_from_a_watched_swarm_is_announced(queue):
    sub, board, lines, _ = build(queue)
    queue.watch("w1", "project:avatar")
    queue.watch("w2", "swarm:beta")
    card = board.add(make_card(project="avatar", title="the actual words"))
    board.post_event("card.posted", card)
    await sub.consume()
    assert sorted(j for j, _ in lines.sent) == ["w1", "w2"]
    assert all(t == f"Board card {card['id']} posted by beta/op to tony in project avatar, "
                    "run operations board_get" for _, t in lines.sent)


async def test_a_card_this_swarm_sent_is_followed_by_its_recipient_swarm(queue):
    sub, board, lines, _ = build(queue)
    queue.watch("w1", "swarm:beta")
    card = board.add(make_card(recipient="beta", sender={"swarm": "tony", "principal": "a", "party": "p"}))
    board.post_event("card.message", board.say(card, "hi", swarm="beta"))
    await sub.consume()
    assert [j for j, _ in lines.sent] == ["w1"]


async def test_the_poster_is_not_told_of_its_own_post(queue):
    sub, board, lines, _ = build(queue)
    queue.watch("w1", "posted:muse")
    card = board.add(make_card(recipient="beta", agent="muse",
                               sender={"swarm": "tony", "principal": "a", "party": "p"}))
    board.post_event("card.posted", card)
    await sub.consume()
    assert lines.sent == []


async def test_a_watch_only_subscriber_types_lines_and_queues_nothing(queue):
    board, lines, told = FakeBoard("tony"), Lines(), Told()
    sub = Subscriber(queue, board, "tony", dm=told, send=lines, intake=False)
    card = board.add(make_card(project="avatar"))
    queue.watch("w1", card["id"])
    queue.watch("w2", "project:avatar")
    board.post_event("card.posted", card)
    board.post_event("card.message", board.say(card, "the actual words"))
    board.post_event("card.message", board.say(card, "again"))
    await sub.consume()
    assert queue.get(card["id"]) is None and told.texts == []
    texts = [t for j, t in lines.sent if j == "w1"]
    assert texts[0].startswith(f"Board card {card['id']} posted by beta/op")
    assert [t.split(",")[0] for t in texts[1:]] == [
        f"Board card {card['id']} has a new message from beta/op"] * 2
    assert len([j for j, _ in lines.sent if j == "w2"]) == 3
    assert not any("actual words" in t for _, t in lines.sent)
    assert queue.thread_seq(card["id"]) == 2 and queue.cursor() == 3
    assert await sub.catch_up() == 0 and board.message_reads == []


async def test_a_watch_only_subscriber_ignores_a_replayed_message(queue):
    board, lines = FakeBoard("tony"), Lines()
    sub = Subscriber(queue, board, "tony", dm=Told(), send=lines, intake=False)
    card = board.add(make_card())
    queue.watch("w1", card["id"])
    event = board.say(card, "hello")
    board.post_event("card.message", event)
    board.post_event("card.message", event)
    await sub.consume()
    assert len(lines.sent) == 1
