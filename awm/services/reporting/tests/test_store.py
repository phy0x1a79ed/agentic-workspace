"""The queue: idempotent by card id, told once, watchers, and the cursor only moves forward."""

from __future__ import annotations

import sqlite3

import pytest

from awm.reporting import store
from awm.reporting.store import Queue

from stubs import make_card

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_a_card_is_queued_once_whatever_the_number_of_deliveries(queue):
    card = make_card()
    assert queue.enqueue(card) is True
    assert queue.enqueue(card) is False
    assert queue.enqueue({**card, "title": "renamed"}) is False
    got = queue.get(card["id"])
    assert (got["title"], got["status"], got["sender"]) == ("renamed", "proposed", "beta")
    assert queue.counts()["proposed"] == 1


def test_a_card_is_told_to_tony_until_it_is_marked_told(queue):
    card = make_card()
    queue.enqueue(card)
    assert queue.needs_dm(card["id"]) is True
    queue.mark_notified(card["id"])
    assert queue.needs_dm(card["id"]) is False
    assert queue.needs_dm("f" * 32) is False


def test_a_call_leaves_the_wal_unchecked_so_closing_its_connection_does_not_fsync(queue):
    queue.enqueue(make_card())
    assert queue.path.with_name(queue.path.name + "-wal").exists()


def test_a_quiet_card_is_recorded_as_already_told(queue):
    card = make_card()
    assert queue.enqueue(card, quiet=True) is True
    assert queue.needs_dm(card["id"]) is False


def test_a_redelivery_keeps_the_arrival_time_and_the_assignment(queue):
    card = make_card()
    queue.enqueue(card)
    first = queue.get(card["id"])
    queue.take(card, "agent-7", "job1")
    queue.enqueue(card)
    again = queue.get(card["id"])
    assert again["arrival"] == first["arrival"]
    assert (again["status"], again["assigned_to"], again["assigned_job"]) == ("assigned", "agent-7", "job1")


def test_the_queue_survives_a_restart(tmp_path):
    card = make_card()
    Queue(tmp_path / "reporting.db").enqueue(card)
    got = Queue(tmp_path / "reporting.db").get(card["id"])
    assert got["status"] == "proposed" and got["arrival_at"].endswith("Z")


def test_take_records_a_card_the_queue_never_held_and_is_not_news_again(queue):
    card = make_card()
    queue.take(card, "agent", None)
    got = queue.get(card["id"])
    assert (got["status"], got["assigned_job"]) == ("assigned", None)
    assert queue.needs_dm(card["id"]) is False


def test_release_puts_an_assigned_card_back_to_proposed(queue):
    card = make_card()
    queue.take(card, "agent", "job1")
    queue.release(card["id"])
    got = queue.get(card["id"])
    assert (got["status"], got["assigned_to"], got["assigned_job"]) == ("proposed", None, None)


def test_mark_finishes_a_card_and_only_a_known_one(queue):
    card = make_card()
    queue.enqueue(card)
    assert queue.mark(card["id"], "done") is True
    assert queue.mark(card["id"], "done") is False
    assert queue.mark("f" * 32, "rejected") is False
    assert queue.get(card["id"])["status"] == "done"
    with pytest.raises(ValueError):
        queue.mark(card["id"], "proposed")


def test_gone_applies_only_to_cards_still_open(queue):
    open_card, finished = make_card(), make_card()
    queue.enqueue(open_card)
    queue.enqueue(finished)
    queue.mark(finished["id"], "done")
    assert queue.mark(open_card["id"], "gone") is True
    assert queue.mark(finished["id"], "gone") is False
    assert queue.status_of(finished["id"]) == "done"


def test_reopen_puts_a_finished_or_assigned_card_back_and_tells_tony_again(queue):
    card = make_card()
    queue.enqueue(card)
    queue.mark_notified(card["id"])
    queue.take(card, "x", "job1")
    queue.mark(card["id"], "done")
    assert queue.enqueue(card) is False
    assert queue.enqueue(card, reopen=True) is True
    got = queue.get(card["id"])
    assert (got["status"], got["assigned_to"]) == ("proposed", None)
    assert queue.needs_dm(card["id"]) is True


def test_active_ids_are_the_open_cards_oldest_first(queue):
    a, b, c = make_card(), make_card(), make_card()
    for card in (a, b, c):
        queue.enqueue(card)
    queue.mark(b["id"], "rejected")
    assert queue.active_ids() == [a["id"], c["id"]]


def test_the_cursor_moves_forward_only_unless_forced(queue):
    assert queue.cursor() is None
    queue.set_cursor(5)
    queue.set_cursor(3)
    assert queue.cursor() == 5
    queue.set_cursor(2, force=True)
    assert queue.cursor() == 2


def test_thread_cursors_never_move_back(queue):
    queue.set_thread_seq("c", 3)
    queue.set_thread_seq("c", 1)
    assert queue.thread_seq("c") == 3 and queue.thread_seqs() == {"c": 3}


def test_watchers_follow_a_card_or_an_agents_posts(queue):
    assert queue.watch("j1", "card-a") is True
    assert queue.watch("j1", "card-a") is False
    queue.watch("j2", "posted:muse")
    queue.watch("j3", "card-b")
    assert queue.watchers_of("card-a") == ["j1"]
    assert queue.watchers_of("card-a", posted_by="muse") == ["j1", "j2"]
    assert queue.watchers_of("card-b", posted_by="other") == ["j3"]
    assert queue.watched_by("j1") == ["card-a"]


def test_unwatch_one_target_or_everything(queue):
    queue.watch("j1", "a")
    queue.watch("j1", "b")
    assert queue.unwatch("j1", "a") == 1
    assert queue.watched_by("j1") == ["b"]
    assert queue.unwatch("j1") == 1
    assert queue.watched_by("j1") == []


def test_pruning_drops_the_watchers_whose_job_ended(queue):
    queue.watch("alive", "a")
    queue.watch("dead", "a")
    queue.watch("dead", "b")
    assert queue.prune_watchers({"alive"}) == 1
    assert queue.watchers_of("a") == ["alive"] and queue.watchers_of("b") == []


def test_a_v5_database_becomes_the_intake_queue(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (5);
        CREATE TABLE cards (card_id TEXT PRIMARY KEY, kind TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal',
            title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL DEFAULT '', reply_to TEXT,
            status TEXT NOT NULL, assigned_to TEXT, assigned_job TEXT, thread_open INTEGER NOT NULL DEFAULT 0,
            arrival REAL NOT NULL, updated REAL NOT NULL, notified INTEGER NOT NULL DEFAULT 0, announced_at REAL);
        CREATE TABLE sessions (role TEXT NOT NULL, job TEXT NOT NULL, started REAL NOT NULL, spec_hash TEXT,
            PRIMARY KEY (role, job));
        CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, node TEXT NOT NULL, key TEXT NOT NULL,
            kind TEXT NOT NULL, state TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('cursor', '41');
    """)
    for card_id, status in (("q", "queued"), ("c", "claiming"), ("a", "assigned"), ("f", "failed"), ("d", "done")):
        conn.execute("INSERT INTO cards (card_id, kind, title, status, arrival, updated, notified)"
                     " VALUES (?, 'request', 't', ?, 1, 1, 1)", (card_id, status))
    conn.commit()
    conn.close()
    queue = Queue(path)
    assert queue.cursor() == 41
    status = {c: queue.status_of(c) for c in "qcafd"}
    assert status == {"q": "proposed", "c": "proposed", "a": "assigned", "f": "rejected", "d": "done"}
    assert queue.needs_dm("q") and queue.needs_dm("c") and not queue.needs_dm("a")
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
    task_columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    conn.close()
    assert "sessions" not in tables and "watchers" in tables
    assert not columns & {"body", "priority", "reply_to", "thread_open", "announced_at"}
    assert {"card_id", "fed_state"} <= task_columns
    assert store.SCHEMA_VERSION == 6
