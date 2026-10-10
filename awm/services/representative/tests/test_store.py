"""The queue: idempotent by card id, ordered, and the cursor only moves forward."""

from __future__ import annotations

import pytest

from awm.representative.store import Queue

from stubs import make_card

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_a_card_is_queued_once_whatever_the_number_of_deliveries(queue):
    card = make_card()
    assert queue.enqueue(card) is True
    assert queue.enqueue(card) is False
    assert queue.enqueue({**card, "title": "renamed"}) is False
    rows = queue.list()
    assert len(rows) == 1
    assert rows[0]["title"] == "renamed"
    assert rows[0]["status"] == "queued"
    assert rows[0]["sender"] == "beta"


def test_a_redelivery_keeps_the_arrival_time_and_the_assignment(queue):
    card = make_card()
    queue.enqueue(card)
    first = queue.get(card["id"])
    queue.assign(card["id"], "agent-7")
    queue.enqueue(card)
    again = queue.get(card["id"])
    assert again["arrival"] == first["arrival"]
    assert (again["status"], again["assigned_to"]) == ("assigned", "agent-7")


def test_the_queue_survives_a_restart(tmp_path):
    card = make_card(priority="urgent")
    Queue(tmp_path / "door.db").enqueue(card)
    reopened = Queue(tmp_path / "door.db")
    got = reopened.get(card["id"])
    assert (got["priority"], got["status"]) == ("urgent", "queued")
    assert got["arrival_at"].endswith("Z")


def test_list_puts_urgent_first_then_oldest(queue):
    low = make_card(priority="low")
    normal = make_card()
    urgent = make_card(priority="urgent")
    for card in (low, normal, urgent):
        queue.enqueue(card)
    assert [r["card_id"] for r in queue.list()] == [urgent["id"], normal["id"], low["id"]]


def test_list_filters_by_status_and_rejects_nonsense(queue):
    a, b = make_card(), make_card()
    queue.enqueue(a)
    queue.enqueue(b)
    queue.assign(a["id"], "x")
    assert [r["card_id"] for r in queue.list("assigned")] == [a["id"]]
    assert [r["card_id"] for r in queue.list("queued")] == [b["id"]]
    assert queue.counts() == {"claiming": 0, "queued": 1, "assigned": 1, "done": 0, "failed": 0, "gone": 0}
    with pytest.raises(ValueError):
        queue.list("posted")


def test_mark_finishes_a_card_and_only_a_known_one(queue):
    card = make_card()
    queue.enqueue(card)
    assert queue.mark(card["id"], "done") is True
    assert queue.mark(card["id"], "done") is False
    assert queue.mark("f" * 32, "failed") is False
    assert queue.get(card["id"])["status"] == "done"
    with pytest.raises(ValueError):
        queue.mark(card["id"], "queued")


def test_assign_refuses_a_missing_or_finished_card(queue):
    card = make_card()
    queue.enqueue(card)
    queue.mark(card["id"], "failed")
    assert queue.assign(card["id"], "x")[0] is None
    assert queue.assign("e" * 32, "x")[0] is None


def test_reopen_puts_a_finished_card_back(queue):
    card = make_card()
    queue.enqueue(card)
    queue.assign(card["id"], "x")
    queue.mark(card["id"], "done")
    assert queue.enqueue(card) is False
    assert queue.enqueue(card, reopen=True) is True
    got = queue.get(card["id"])
    assert (got["status"], got["assigned_to"]) == ("queued", None)


def test_the_cursor_moves_forward_only_unless_forced(queue):
    assert queue.cursor() is None
    queue.set_cursor(5)
    queue.set_cursor(3)
    assert queue.cursor() == 5
    queue.set_cursor(2, force=True)
    assert queue.cursor() == 2


def test_announcements_cover_queued_cards_once(queue):
    a, b = make_card(), make_card(priority="urgent")
    queue.enqueue(a)
    queue.enqueue(b)
    assert {c for c, _, _ in queue.unannounced()} == {a["id"], b["id"]}
    queue.mark_announced([a["id"]])
    assert [c for c, _, _ in queue.unannounced()] == [b["id"]]
    queue.reset_announced()
    assert len(queue.unannounced()) == 2
    queue.assign(a["id"], "x")  # a handed-off card is no longer news
    assert [c for c, _, _ in queue.unannounced()] == [b["id"]]


def test_reply_to_is_kept_and_returned(queue):
    card = make_card(kind="message", reply_to="a" * 32)
    queue.enqueue(card)
    assert queue.get(card["id"])["reply_to"] == "a" * 32
    assert queue.list()[0]["reply_to"] == "a" * 32
    plain = make_card()
    queue.enqueue(plain)
    assert queue.get(plain["id"])["reply_to"] is None


def test_handing_off_a_message_finishes_it_but_a_request_stays_assigned(queue):
    message, request = make_card(kind="message"), make_card()
    queue.enqueue(message)
    queue.enqueue(request)
    done, _ = queue.assign(message["id"], "agent-1")
    assigned, _ = queue.assign(request["id"], "agent-1")
    assert (done["status"], done["assigned_to"]) == ("done", "agent-1")
    assert assigned["status"] == "assigned"
    assert queue.active_ids() == [request["id"]]


def test_a_claiming_row_is_not_active_or_announced_until_the_card_is_queued(queue):
    card = make_card()
    assert queue.begin_claim(card) is True
    assert queue.begin_claim(card) is False
    assert queue.status_of(card["id"]) == "claiming"
    assert queue.active_ids() == [] and queue.unannounced() == []
    assert queue.assign(card["id"], "x")[0] is None  # not open for hand-off yet
    assert queue.enqueue(card) is True  # the claim landed
    assert queue.status_of(card["id"]) == "queued"
    assert [c for c, _, _ in queue.unannounced()] == [card["id"]]


def test_a_refused_claim_attempt_leaves_no_row(queue):
    card = make_card()
    queue.begin_claim(card)
    queue.drop_claim(card["id"])
    assert queue.get(card["id"]) is None
    queue.enqueue(card)
    queue.drop_claim(card["id"])  # a queued card is never dropped
    assert queue.get(card["id"]) is not None


def test_gone_applies_only_to_cards_still_in_the_doors_hands(queue):
    open_card, finished = make_card(), make_card()
    queue.enqueue(open_card)
    queue.enqueue(finished)
    queue.mark(finished["id"], "done")
    assert queue.mark(open_card["id"], "gone") is True
    assert queue.mark(finished["id"], "gone") is False
    assert queue.status_of(finished["id"]) == "done"


def test_a_queued_card_is_announced_again_after_the_stale_window(queue):
    card = make_card()
    queue.enqueue(card)
    queue.mark_announced([card["id"]], now=1000.0)
    assert queue.unannounced() == []
    assert queue.unannounced(stale_after_s=600, now=1500.0) == []
    assert queue.unannounced(stale_after_s=600, now=1700.0) == [(card["id"], "normal", False)]
    queue.mark_announced([card["id"]], now=1700.0)
    assert queue.unannounced(stale_after_s=600, now=1800.0) == []


def test_assigned_cards_are_not_announced_again(queue):
    card = make_card()
    queue.enqueue(card)
    queue.mark_announced([card["id"]], now=0.0)
    queue.assign(card["id"], "x")
    assert queue.unannounced(stale_after_s=1, now=10_000.0) == []


def test_the_sessions_the_door_started_are_recorded_per_role(tmp_path):
    q = Queue(tmp_path / "door.db")
    q.record_session("representative", "aaaa1111")
    q.record_session("representative", "aaaa1111")
    q.record_session("secretary", "bbbb2222")
    assert Queue(tmp_path / "door.db").session_jobs("representative") == {"aaaa1111"}
    assert q.session_jobs("secretary") == {"bbbb2222"}
    assert q.session_jobs("other") == set()
