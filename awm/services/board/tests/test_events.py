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
    sender = {"swarm": "tony", "principal": "agent", "party": "p"}
    assert migrated.message_add("x", at="t", sender=sender, origin="api", body="hi")["seq"] == 1


def test_message_sequences_count_per_card(events):
    sender = {"swarm": "tony", "principal": "agent", "party": "p"}
    seqs = [events.message_add(c, at="t", sender=sender, origin="api", body=c)["seq"]
            for c in ("a", "a", "b", "a")]
    assert seqs == [1, 2, 1, 3]
    assert [m["seq"] for m in events.messages("a", after=1)] == [2, 3]
    page, cursor = events.message_page(0, 3)
    assert len(page) == 3 and cursor
    rest, end = events.message_page(cursor, 3)
    assert [m["card_id"] for m in rest] == ["a"] and end == 0


def test_a_version_3_log_gains_the_agent_column_and_the_folded_table(tmp_path):
    from awm.board.events import Events

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (3);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
            card_id TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
            card TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE last_seen (card_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            recipient TEXT, claimant TEXT);
        CREATE TABLE messages (card_id TEXT NOT NULL, seq INTEGER NOT NULL, at TEXT NOT NULL,
            swarm TEXT NOT NULL, principal TEXT NOT NULL, party TEXT NOT NULL,
            origin TEXT NOT NULL, body TEXT NOT NULL, note_id TEXT, PRIMARY KEY (card_id, seq));
        INSERT INTO messages VALUES ('x', 1, 't', 'tony', 'p', 'q', 'api', 'old', NULL);
    """)
    conn.commit()
    conn.close()
    upgraded = Events(path)
    assert upgraded.messages("x")[0]["agent"] is None
    sender = {"swarm": "tony", "principal": "p", "party": "q"}
    assert upgraded.message_add("x", at="t", sender=sender, origin="api", body="new",
                                agent="worker")["agent"] == "worker"
    assert upgraded.folded_ids(["r"]) == set()


def test_a_card_history_is_filtered_like_the_stream_and_pages_by_event_id(events):
    mine = events.append("card.posted", card("a", sender="tony", recipient="collins"))
    events.append("card.posted", card("b", sender="tony", recipient="collins"))
    later = events.append("card.claimed", card("a", sender="tony", recipient="collins"))
    hidden = events.append("card.moved", card("a", sender="tony", recipient="collins"))
    assert [e["id"] for e in events.for_card("a", {"swarm": "collins"})] == [mine, later, hidden]
    assert [e["id"] for e in events.for_card("a", {"swarm": "collins"}, since=mine)] == [later, hidden]
    assert [e["id"] for e in events.for_card("a", {"swarm": "collins"}, limit=1)] == [mine]
    assert events.for_card("a", {"swarm": "mock"}) == []


def test_backfill_puts_history_before_the_thread_and_never_repeats(events):
    sender = {"swarm": "tony", "principal": "p", "party": "q"}
    for body in ("live one", "live two"):
        events.message_add("root", at="t", sender=sender, origin="api", body=body)
    history = [{"reply_id": f"r{i}", "at": f"2026-01-0{i}", "sender": sender, "body": f"old {i}"}
               for i in (1, 2, 3)]
    thread = events.messages_backfill("root", history)
    assert [(m["seq"], m["body"]) for m in thread] == [
        (1, "old 1"), (2, "old 2"), (3, "old 3"), (4, "live one"), (5, "live two")]
    assert thread[0]["origin"] == "migrated" and thread[0]["at"] == "2026-01-01"
    assert events.folded_ids(["r1", "r2", "r9"]) == {"r1", "r2"}
    assert [m["seq"] for m in events.messages("root")] == [1, 2, 3, 4, 5]


def test_a_version_4_log_gains_the_node_column_and_opens_again_unchanged(tmp_path):
    from awm.board.events import Events

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (4);
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
            card_id TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
            card TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE last_seen (card_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            recipient TEXT, claimant TEXT);
        CREATE TABLE messages (card_id TEXT NOT NULL, seq INTEGER NOT NULL, at TEXT NOT NULL,
            swarm TEXT NOT NULL, principal TEXT NOT NULL, party TEXT NOT NULL,
            origin TEXT NOT NULL, body TEXT NOT NULL, note_id TEXT, agent TEXT,
            PRIMARY KEY (card_id, seq));
        INSERT INTO messages VALUES ('x', 1, 't', 'tony', 'p', 'q', 'api', 'old', NULL, 'w');
    """)
    conn.commit()
    conn.close()
    upgraded = Events(path)
    assert upgraded.messages("x")[0]["node"] is None
    sender = {"swarm": "tony", "principal": "p", "party": "q"}
    added = upgraded.message_add("x", at="t", sender=sender, origin="api", body="new",
                                 agent="w", node="deneb")
    assert (added["agent"], added["node"]) == ("w", "deneb")
    assert Events(path).messages("x", after=1)[0]["node"] == "deneb"


def test_a_fresh_log_has_the_node_column(events):
    sender = {"swarm": "tony", "principal": "p", "party": "q"}
    assert events.message_add("x", at="t", sender=sender, origin="api", body="b",
                              node="mira")["node"] == "mira"
