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
    events.seen_set("a", "posted")
    events.seen_set("a", "done")
    events.seen_set("b", "posted")
    assert events.seen_all() == {"a": "done", "b": "posted"}
    events.seen_drop(["a"])
    assert events.seen_all() == {"b": "posted"}
