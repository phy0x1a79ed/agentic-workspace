import sqlite3
import time

import pytest


def card(cid="c1", sender="tony", recipient="collins"):
    return {"id": cid, "sender": {"swarm": sender, "principal": "x", "party": "p"},
            "recipient": recipient, "status": "posted"}


def test_ids_increase_monotonically(events):
    ids = [events.append("card.posted", card(f"c{i}")) for i in range(5)]
    assert ids == sorted(ids) and len(set(ids)) == 5
    assert events.latest_id() == ids[-1]


def test_replay_from_an_id_returns_only_later_events(events):
    tony = {"swarm": "tony"}
    ids = [events.append("card.posted", card(f"c{i}", recipient="tony")) for i in range(4)]
    replay = events.since(ids[1], tony)
    assert [e["id"] for e in replay] == ids[2:]
    assert replay[0]["type"] == "card.posted"
    assert replay[0]["card"]["id"] == "c2"
    assert events.since(0, tony)[0]["id"] == ids[0]
    assert events.since(ids[-1], tony) == []


def test_only_events_the_party_may_see(events):
    events.append("card.posted", card("to-tony", sender="collins", recipient="tony"))
    events.append("card.posted", card("to-mock", sender="collins", recipient="mock"))
    events.append("card.posted", card("open", sender="collins", recipient="open"))
    events.append("card.claimed", card("sent-by-tony", sender="tony", recipient="mock"))
    seen = [e["card"]["id"] for e in events.since(0, {"swarm": "tony"})]
    assert seen == ["to-tony", "open", "sent-by-tony"]
    assert [e["card"]["id"] for e in events.since(0, {"swarm": "mock"})] == [
        "to-mock", "open", "sent-by-tony"]
    assert [e["card"]["id"] for e in events.since(0, {"swarm": "shaula"})] == ["open"]


def test_unknown_event_type_is_refused(events):
    with pytest.raises(ValueError):
        events.append("card.exploded", card())


def test_prune_drops_only_old_events_and_keeps_ids_rising(events, tmp_path):
    old = events.append("card.posted", card("old"))
    recent = events.append("card.posted", card("new"))
    raw = sqlite3.connect(tmp_path / "events.db")
    raw.execute("UPDATE events SET created_at=? WHERE id=?", (time.time() - 40 * 86400, old))
    raw.commit()
    assert events.prune(days=30) == 1
    assert [e["id"] for e in events.since(0, {"swarm": "tony"})] == [recent]
    assert events.append("card.posted", card("later")) > recent


def test_event_cards_are_snapshots(events):
    c = card("snap")
    events.append("card.posted", c)
    c["status"] = "done"
    assert events.since(0, {"swarm": "tony"})[0]["card"]["status"] == "posted"


def test_last_seen_map_roundtrips(events):
    events.seen_set("a", "posted", "tony", "")
    events.seen_set("a", "done", "tony", "collins")
    events.seen_set("b", "posted", "open", "")
    assert events.seen_all() == {
        "a": {"status": "done", "recipient": "tony", "claimant": "collins"},
        "b": {"status": "posted", "recipient": "open", "claimant": ""}}
    events.seen_drop(["a"])
    assert list(events.seen_all()) == ["b"]


def test_oldest_id_tracks_the_prune_window(events, tmp_path):
    assert events.oldest_id() == 0
    first = events.append("card.posted", card("a"))
    second = events.append("card.posted", card("b"))
    assert events.oldest_id() == first
    raw = sqlite3.connect(tmp_path / "events.db")
    raw.execute("UPDATE events SET created_at=? WHERE id=?", (time.time() - 40 * 86400, first))
    raw.commit()
    events.prune(days=30)
    assert events.oldest_id() == second
    events.prune(days=0)
    assert events.oldest_id() == 0


def test_since_filters_in_sql_and_limits(events):
    tony = {"swarm": "tony"}
    for i in range(30):
        events.append("card.posted", card(f"other{i}", sender="collins", recipient="mock"))
    mine = [events.append("card.posted", card(f"mine{i}", sender="collins", recipient="tony"))
            for i in range(5)]
    assert [e["id"] for e in events.since(0, tony)] == mine
    assert [e["id"] for e in events.since(0, tony, limit=2)] == mine[:2]
    assert [e["id"] for e in events.since(mine[1], tony, limit=2)] == mine[2:4]
    assert len(events.since(0, {"swarm": "mock"}, limit=10)) == 10


def test_since_defaults_to_500(events):
    for i in range(520):
        events.append("card.posted", card(f"c{i}", recipient="tony"))
    assert len(events.since(0, {"swarm": "tony"})) == 500


def test_the_columns_the_filter_uses_are_indexed(events, tmp_path):
    raw = sqlite3.connect(tmp_path / "events.db")
    names = {r[1] for r in raw.execute("PRAGMA index_list(events)")}
    assert {"events_sender", "events_recipient"} <= names


def test_a_version_1_log_is_migrated(tmp_path):
    from awm.board.events import Events
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
            card_id TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
            card TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE last_seen (card_id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1);
        INSERT INTO last_seen VALUES ('x', 'posted');
    """)
    raw.commit()
    raw.close()
    migrated = Events(path)
    assert migrated.seen_all()["x"] == {"status": "posted", "recipient": None, "claimant": None}
    migrated.seen_set("x", "done", "tony", "")
    assert migrated.seen_all()["x"]["status"] == "done"
    names = {r[1] for r in sqlite3.connect(path).execute("PRAGMA index_list(events)")}
    assert "events_sender" in names
