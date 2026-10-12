"""Scopes-side channel tests: the legacy seed fold, the schema migration, and the history hand-off."""
from __future__ import annotations

import sqlite3

import pytest

pytestmark = [pytest.mark.scopes]


def _build_legacy_db(path) -> None:
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, url TEXT, repo_path TEXT, created_at INTEGER);
        CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT);
        CREATE TABLE agents (id TEXT PRIMARY KEY, project_id TEXT, scope TEXT, status TEXT,
            agent_cli TEXT, branch TEXT, worktree TEXT, display_name TEXT, is_vagrant INT, created_at INT, retired_at INT);
        CREATE TABLE session_logs (id INTEGER PRIMARY KEY, agent_id TEXT, created_at INT, file_path TEXT, git_commit TEXT,
            summary TEXT, metadata TEXT, content TEXT, skill_path TEXT, outcome TEXT, deviations TEXT, suggestions TEXT,
            skill_version TEXT, resolved_at INT, resolution TEXT, title TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, recipient_id TEXT, sender_id TEXT, msg_type TEXT, subject TEXT,
            body TEXT, metadata TEXT, status TEXT, created_at INT, read_at INT);
        CREATE TABLE embeddings (source_type TEXT, source_id TEXT, chunk_text TEXT, embedding BLOB, updated_at INT);
    """)
    db.execute("INSERT INTO projects VALUES ('p1','awm',NULL,'/repo',1000)")
    db.execute("INSERT INTO users VALUES ('u1','alice')")
    db.execute("INSERT INTO agents VALUES ('a1','p1','dev','active','claude','feat/dev','/wt/dev','dev',0,1000,NULL)")
    db.execute("INSERT INTO agents VALUES ('a2','p1','helper','active','claude','feat/helper','/wt/helper','helper',0,1000,NULL)")
    db.execute("INSERT INTO session_logs (id,agent_id,created_at,summary,metadata,skill_path,outcome,title) "
               "VALUES (1,'a1',2000,'did the thing','{\"decisions\":[\"d1\"]}','awm/debrief.md','success','Did the thing')")
    db.execute("INSERT INTO messages (id,recipient_id,sender_id,msg_type,subject,body,status,created_at) "
               "VALUES (1,'a1','u1','notification','hi','hello dev','unread',2500)")
    db.execute("INSERT INTO messages (id,recipient_id,sender_id,msg_type,subject,body,status,created_at) "
               "VALUES (2,'u1','a1','notification','re','reply to alice','unread',2600)")
    db.commit()
    db.close()


class TestSeedFold:
    """The legacy seed folds the non-rooms comms pair (session_logs + messages)
    into the scope channel. Rooms folding was removed with the rooms subsystem."""

    def test_fold(self, scopes_workspace, tmp_path):
        from awm.scopes.seed import seed_from_legacy
        from awm.persistence.databases import get_connection

        legacy = tmp_path / "state.db"
        _build_legacy_db(legacy)
        counts = seed_from_legacy(legacy)

        assert counts["agents"] == 2
        # 1 journal + 1 dev message + 1 alice message
        assert counts["scope_posts"] == 3

        conn = get_connection("scopes")
        conn.row_factory = sqlite3.Row
        try:
            dev = conn.execute(
                "SELECT kind, body, ts FROM scope_posts "
                "WHERE owner_project='awm' AND owner_scope='dev' ORDER BY ts"
            ).fetchall()
            # One journal entry + the message addressed to dev.
            assert sum(1 for r in dev if r["kind"] == "journal") == 1
            assert "hello dev" in [r["body"] for r in dev]
            # Non-literal user channel.
            alice = conn.execute(
                "SELECT body FROM scope_posts WHERE owner_project='' AND owner_scope='user:alice'"
            ).fetchall()
            assert [r["body"] for r in alice] == ["reply to alice"]
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
        assert n == 3


class TestSchemaMigrationV3:
    """The v2→v3 migration drops the rooms-era ``agents.parent_id`` (and its
    dependent index) while preserving every agent row."""

    def test_drops_parent_id_and_preserves_rows(self, tmp_path):
        from awm.scopes.dao import MIGRATIONS

        db = sqlite3.connect(tmp_path / "v2.db")
        db.executescript(
            "CREATE TABLE agents (id TEXT PRIMARY KEY, project_id TEXT, "
            "scope TEXT, parent_id TEXT REFERENCES agents(id), status TEXT, "
            "agent_cli TEXT, branch TEXT, worktree TEXT, display_name TEXT, "
            "is_vagrant INT, created_at INT, retired_at INT);\n"
            "CREATE INDEX idx_agents_parent ON agents(parent_id);\n"
            "INSERT INTO agents VALUES "
            "('a1','p1','dev','par','active','claude','b','w','dev',0,1,NULL);\n"
        )
        db.commit()
        # Apply the real migration SQL the way the migration loop does (FKs off).
        db.executescript("PRAGMA foreign_keys=OFF;\n" + MIGRATIONS[(2, 3)])

        cols = [r[1] for r in db.execute("PRAGMA table_info(agents)").fetchall()]
        assert "parent_id" not in cols
        # Row + its surviving data preserved.
        assert db.execute(
            "SELECT id, scope, status FROM agents"
        ).fetchone() == ("a1", "dev", "active")
        # The dependent index is gone too.
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_agents_parent'"
        ).fetchone() is None
        db.close()


class TestHistoryHandoff:
    def test_siblings_use_the_live_status_pair(self, scopes_workspace, monkeypatch):
        """Scopes are born 'allocated' and nothing promotes them to 'active'.

        Filtering on 'active' alone named 1 sibling in 23 on the real workspace.
        Every other live-scope predicate in the service uses the pair.
        """
        from awm.scopes import scopes as sc
        from awm.scopes.identity import ensure_agent
        sent = {}

        def fake_call_sync(service, fn, args=None, **kw):
            sent.update(service=service, fn=fn, args=args)
            return {"content": "rendered"}

        monkeypatch.setattr("awm.gatewayclient.call_sync", fake_call_sync)
        sc._ensure_project_row("awm")
        ensure_agent("awm", "sib-allocated", status="allocated")
        assert sc._generate_history_md("awm", "mine") == "rendered"
        assert (sent["service"], sent["fn"]) == ("journal", "render")
        assert sent["args"]["siblings"] == ["sib-allocated"]
        assert sent["args"]["write"] is False
