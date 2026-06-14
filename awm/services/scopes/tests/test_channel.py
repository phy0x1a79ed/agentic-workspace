"""Scope-channel tests: live post/fetch/subscribe + the legacy seed fold.

A scope IS the channel. These cover the unified surface that replaced the old
rooms/messages/session_logs trio.
"""
from __future__ import annotations

import sqlite3

import pytest

pytestmark = [pytest.mark.scopes]


# ---------------------------------------------------------------------------
# Live channel ops
# ---------------------------------------------------------------------------

class TestChannelOps:
    def test_post_and_fetch(self, scopes_workspace):
        from awm.scopes import channel
        channel.post("awm", "dev", author="user:alice", body="hello", kind="message")
        channel.post("awm", "dev", author="agent:awm/dev", body="hi back", kind="message")
        posts = channel.fetch(project="awm", scope="dev")
        assert [p.body for p in posts] == ["hello", "hi back"]  # oldest→newest
        assert posts[0].author == "user:alice"

    def test_order_desc_gives_last_n(self, scopes_workspace):
        """order='desc' + limit pulls the most-recent N of a single channel,
        the 'last 5 session logs' path. Default stays oldest→newest."""
        from awm.scopes import channel
        for i in range(5):
            channel.post("awm", "dev", author="agent:awm/dev",
                         body=f"entry {i}", kind="journal")
        # default single-channel order is oldest→newest (unchanged)
        asc = channel.fetch(project="awm", scope="dev", kind="journal")
        assert [p.body for p in asc] == [f"entry {i}" for i in range(5)]
        # order='desc' + limit → the last 2, newest-first
        last2 = channel.fetch(project="awm", scope="dev", kind="journal",
                              limit=2, order="desc")
        assert [p.body for p in last2] == ["entry 4", "entry 3"]

    def test_journal_kind_filter(self, scopes_workspace):
        from awm.scopes import channel
        channel.post("awm", "dev", author="agent:awm/dev", body="a message", kind="message")
        channel.post("awm", "dev", author="agent:awm/dev", body="debrief", kind="journal",
                     meta={"title": "Did it", "outcome": "success"})
        journals = channel.fetch(project="awm", scope="dev", kind="journal")
        assert len(journals) == 1
        assert journals[0].kind == "journal"
        assert journals[0].meta["outcome"] == "success"

    def test_search(self, scopes_workspace):
        from awm.scopes import channel
        channel.post("awm", "dev", author="user:alice", body="the quick brown fox", kind="message")
        channel.post("awm", "dev", author="user:alice", body="unrelated", kind="message")
        hits = channel.fetch(project="awm", scope="dev", query="brown")
        assert any("brown" in p.body for p in hits)

    def test_get_post(self, scopes_workspace):
        from awm.scopes import channel
        p = channel.post("awm", "dev", author="user:alice", body="findme", kind="message")
        got = channel.get_post(p.id)
        assert got is not None and got.body == "findme"

    def test_subscribe_unsubscribe(self, scopes_workspace):
        from awm.scopes import channel
        channel.subscribe("awm", "dev", "awm/helper")
        channel.subscribe("awm", "dev", "user:bob")
        subs = {(s.guest_kind, s.guest_ref) for s in channel.list_subscribers("awm", "dev")}
        assert ("agent", "awm/helper") in subs
        assert ("user", "user:bob") in subs
        assert channel.unsubscribe("awm", "dev", "awm/helper") is True
        assert channel.unsubscribe("awm", "dev", "awm/helper") is False
        subs2 = {(s.guest_kind, s.guest_ref) for s in channel.list_subscribers("awm", "dev")}
        assert ("agent", "awm/helper") not in subs2

    def test_non_literal_channel(self, scopes_workspace):
        """Posting to a user/project inbox uses owner_project='' (non-literal)."""
        from awm.scopes import channel
        channel.post("", "user:alice", author="agent:awm/dev", body="for alice", kind="message")
        posts = channel.fetch(project="", scope="user:alice")
        assert [p.body for p in posts] == ["for alice"]

    def test_post_fires_emitter(self, scopes_workspace):
        """Every post fans out one cross-service `posts` emit (the live
        subscription the agents service rides instead of a poll)."""
        from awm.scopes import channel
        seen: list[dict] = []
        channel.set_emitter(lambda payload: seen.append(payload))
        try:
            channel.post("awm", "dev", author="user:alice",
                         body="ping", kind="message")
        finally:
            channel.set_emitter(None)
        assert len(seen) == 1
        ev = seen[0]
        assert ev["project"] == "awm"
        assert ev["scope"] == "dev"
        assert ev["post"]["body"] == "ping"
        assert ev["post"]["author"] == "user:alice"
        assert ev["post"]["kind"] == "message"

    def test_emitter_failure_does_not_break_post(self, scopes_workspace):
        """A throwing emitter never fails the post (best-effort signalling)."""
        from awm.scopes import channel

        def _boom(_payload):
            raise RuntimeError("emit down")

        channel.set_emitter(_boom)
        try:
            p = channel.post("awm", "dev", author="user:alice",
                             body="still saved", kind="message")
        finally:
            channel.set_emitter(None)
        assert channel.get_post(p.id) is not None


# ---------------------------------------------------------------------------
# Legacy seed fold
# ---------------------------------------------------------------------------

def _build_legacy_db(path) -> None:
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, url TEXT, repo_path TEXT, created_at INTEGER);
        CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT);
        CREATE TABLE agents (id TEXT PRIMARY KEY, project_id TEXT, scope TEXT, parent_id TEXT, status TEXT,
            agent_cli TEXT, branch TEXT, worktree TEXT, display_name TEXT, is_vagrant INT, created_at INT, retired_at INT);
        CREATE TABLE session_logs (id INTEGER PRIMARY KEY, agent_id TEXT, created_at INT, file_path TEXT, git_commit TEXT,
            summary TEXT, metadata TEXT, content TEXT, skill_path TEXT, outcome TEXT, deviations TEXT, suggestions TEXT,
            skill_version TEXT, resolved_at INT, resolution TEXT, title TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, recipient_id TEXT, sender_id TEXT, msg_type TEXT, subject TEXT,
            body TEXT, metadata TEXT, status TEXT, created_at INT, read_at INT);
        CREATE TABLE rooms (id TEXT PRIMARY KEY, owner_agent_id TEXT, topic TEXT, status TEXT, created_at INT, closed_at INT);
        CREATE TABLE guest_list (room_id TEXT, guest_kind TEXT, guest_ref TEXT, display_name TEXT, subscriptions TEXT);
        CREATE TABLE room_transcripts (id TEXT PRIMARY KEY, room_id TEXT, author TEXT, kind TEXT, body TEXT, meta TEXT, ts INT);
        CREATE TABLE embeddings (source_type TEXT, source_id TEXT, chunk_text TEXT, embedding BLOB, updated_at INT);
    """)
    db.execute("INSERT INTO projects VALUES ('p1','awm',NULL,'/repo',1000)")
    db.execute("INSERT INTO users VALUES ('u1','alice')")
    db.execute("INSERT INTO agents VALUES ('a1','p1','dev',NULL,'active','claude','feat/dev','/wt/dev','dev',0,1000,NULL)")
    db.execute("INSERT INTO agents VALUES ('a2','p1','helper',NULL,'active','claude','feat/helper','/wt/helper','helper',0,1000,NULL)")
    db.execute("INSERT INTO session_logs (id,agent_id,created_at,summary,metadata,skill_path,outcome,title) "
               "VALUES (1,'a1',2000,'did the thing','{\"decisions\":[\"d1\"]}','awm/debrief.md','success','Did the thing')")
    db.execute("INSERT INTO messages (id,recipient_id,sender_id,msg_type,subject,body,status,created_at) "
               "VALUES (1,'a1','u1','notification','hi','hello dev','unread',2500)")
    db.execute("INSERT INTO messages (id,recipient_id,sender_id,msg_type,subject,body,status,created_at) "
               "VALUES (2,'u1','a1','notification','re','reply to alice','unread',2600)")
    # Two rooms owned by the SAME scope → must fold into one channel.
    db.execute("INSERT INTO rooms VALUES ('room-one','a1','first','open',1500,NULL)")
    db.execute("INSERT INTO rooms VALUES ('room-two','a1','second','closed',1600,1700)")
    db.execute("INSERT INTO guest_list VALUES ('room-one','agent','a2','helper','{}')")
    db.execute("INSERT INTO guest_list VALUES ('room-two','user','u1','alice','{}')")
    db.execute("INSERT INTO room_transcripts VALUES ('t1','room-one','a1','message','from room one','{}',1550)")
    db.execute("INSERT INTO room_transcripts VALUES ('t2','room-two','a2','message','from room two','{}',1650)")
    db.commit()
    db.close()


class TestSeedFold:
    def test_fold(self, scopes_workspace, tmp_path):
        from awm.scopes.seed import seed_from_legacy
        from awm.persistence.databases import get_connection

        legacy = tmp_path / "state.db"
        _build_legacy_db(legacy)
        counts = seed_from_legacy(legacy)

        assert counts["agents"] == 2
        # 1 journal + 1 dev message + 1 alice message + 2 transcripts
        assert counts["scope_posts"] == 5
        assert counts["scope_subscribers"] == 2

        conn = get_connection("scopes")
        conn.row_factory = sqlite3.Row
        try:
            dev = conn.execute(
                "SELECT kind, body, ts FROM scope_posts "
                "WHERE owner_project='awm' AND owner_scope='dev' ORDER BY ts"
            ).fetchall()
            # Both rooms' transcripts folded into awm/dev, ts-ordered.
            transcripts = [r["body"] for r in dev if r["ts"] in (1550, 1650)]
            assert transcripts == ["from room one", "from room two"]
            # One journal entry.
            assert sum(1 for r in dev if r["kind"] == "journal") == 1
            # Non-literal user channel.
            alice = conn.execute(
                "SELECT body FROM scope_posts WHERE owner_project='' AND owner_scope='user:alice'"
            ).fetchall()
            assert [r["body"] for r in alice] == ["reply to alice"]
            # Guests from both folded rooms landed on the awm/dev channel.
            subs = {(r["guest_kind"], r["guest_ref"]) for r in conn.execute(
                "SELECT guest_kind, guest_ref FROM scope_subscribers "
                "WHERE owner_project='awm' AND owner_scope='dev'"
            ).fetchall()}
            assert ("agent", "awm/helper") in subs
            assert ("user", "user:alice") in subs
        finally:
            conn.close()

    def test_idempotent(self, scopes_workspace, tmp_path):
        from awm.scopes.seed import seed_from_legacy
        from awm.persistence.databases import get_connection
        legacy = tmp_path / "state.db"
        _build_legacy_db(legacy)
        seed_from_legacy(legacy)
        seed_from_legacy(legacy)  # re-run must not duplicate
        conn = get_connection("scopes")
        try:
            n = conn.execute("SELECT COUNT(*) FROM scope_posts").fetchone()[0]
        finally:
            conn.close()
        assert n == 5
