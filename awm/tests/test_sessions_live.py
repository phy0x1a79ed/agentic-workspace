"""Tests for awm.services.sessions_live pure helpers.

The LiveSession lifecycle is integration-heavy (spawns claude); these tests
cover the stream-json parsing in _extract_renderable plus the DB-level
persistence of claude_session_id that lets re-invite-after-death still
resume the same claude conversation.
"""

from __future__ import annotations

import sqlite3

from awm.db import get_connection
from awm.services.sessions_live import _extract_renderable


def test_assistant_text_block_emits_text():
    evt = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
        },
    }
    assert _extract_renderable(evt) == [("text", "hello")]


def test_assistant_tool_use_block_emits_tool_use():
    evt = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Read", "id": "x", "input": {}}],
        },
    }
    assert _extract_renderable(evt) == [("tool_use", "[tool_use: Read]")]


def test_user_tool_result_block_emits_tool_result():
    evt = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "file body"}
            ],
        },
    }
    assert _extract_renderable(evt) == [("tool_result", "file body")]


def test_successful_result_event_is_silent():
    # The success "result" event echoes the final assistant text; the
    # assistant event already posted that, so result must be skipped to
    # avoid duplicate transcript entries.
    evt = {"type": "result", "subtype": "success", "result": "hello", "is_error": False}
    assert _extract_renderable(evt) == []


def test_error_result_event_surfaces_as_system():
    evt = {
        "type": "result",
        "subtype": "error_max_tokens",
        "result": "hit the cap",
        "is_error": True,
    }
    assert _extract_renderable(evt) == [("system", "[error] hit the cap")]


def test_unknown_event_type_is_ignored():
    assert _extract_renderable({"type": "system"}) == []
    assert _extract_renderable({"type": "stream_event", "event": {}}) == []


# ---------------------------------------------------------------------------
# claude_session_id persistence + resume-after-reap
# ---------------------------------------------------------------------------


def _insert_session_row(conn: sqlite3.Connection, *,
                        project: str, scope: str, status: str,
                        claude_session_id: str | None,
                        started_at: str = "2026-05-21T00:00:00+00:00") -> int:
    cur = conn.execute(
        "INSERT INTO agent_sessions "
        "(project, scope, pid, status, agent_cli, started_at, log_path, "
        " claude_session_id) "
        "VALUES (?, ?, 0, ?, 'claude', ?, '/tmp/log', ?)",
        (project, scope, status, started_at, claude_session_id),
    )
    conn.commit()
    return cur.lastrowid


class TestClaudeSessionIdColumn:
    def test_column_exists_after_init(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        cols = {r["name"]
                for r in conn.execute("PRAGMA table_info(agent_sessions)")}
        conn.close()
        assert "claude_session_id" in cols

    def test_can_store_and_read_id(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session_row(
            conn, project="p", scope="s", status="exited",
            claude_session_id="abc-123",
        )
        row = conn.execute(
            "SELECT claude_session_id FROM agent_sessions WHERE id=?", (sid,),
        ).fetchone()
        conn.close()
        assert row["claude_session_id"] == "abc-123"


class TestResumeLookup:
    """The SQL the create_session path runs to recover a resume id when no
    in-memory predecessor exists. Asserting the query directly (rather than
    driving it through create_session, which would spawn claude) keeps the
    test hermetic."""

    LOOKUP_SQL = (
        "SELECT claude_session_id FROM agent_sessions "
        "WHERE project=? AND scope=? AND claude_session_id IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    )

    def test_returns_most_recent_id(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        _insert_session_row(conn, project="p", scope="s", status="exited",
                            claude_session_id="old-id")
        _insert_session_row(conn, project="p", scope="s", status="exited",
                            claude_session_id="new-id")
        row = conn.execute(self.LOOKUP_SQL, ("p", "s")).fetchone()
        conn.close()
        assert row is not None
        assert row["claude_session_id"] == "new-id"

    def test_skips_rows_with_no_id(self, awm_workspace):
        # Re-invite after the agent died before it ever emitted an init
        # event — there's a row but no captured id. Lookup must skip it.
        conn = get_connection(awm_workspace["db_path"])
        _insert_session_row(conn, project="p", scope="s", status="exited",
                            claude_session_id="real-id")
        _insert_session_row(conn, project="p", scope="s", status="exited",
                            claude_session_id=None)
        row = conn.execute(self.LOOKUP_SQL, ("p", "s")).fetchone()
        conn.close()
        assert row is not None
        assert row["claude_session_id"] == "real-id"

    def test_no_history_returns_none(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        row = conn.execute(self.LOOKUP_SQL, ("p", "s")).fetchone()
        conn.close()
        assert row is None

    def test_other_scope_does_not_leak(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        _insert_session_row(conn, project="p", scope="other", status="exited",
                            claude_session_id="other-id")
        row = conn.execute(self.LOOKUP_SQL, ("p", "s")).fetchone()
        conn.close()
        assert row is None


class TestMigrationFromV23:
    def test_v23_db_gets_column(self, tmp_path, monkeypatch):
        """A pre-existing v23 DB (without the column) should pick up
        claude_session_id when init_db migrates it forward."""
        from awm.db import init_db, SCHEMA_VERSION
        assert SCHEMA_VERSION >= 24, "this test guards the v23→v24 migration"

        db_path = tmp_path / "v23.db"
        monkeypatch.setattr("awm.db.AWM_DIR", tmp_path)

        # Build a v23-shaped agent_sessions (no claude_session_id) and
        # claim schema_version=23 so _migrate runs (23, 24).
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            CREATE TABLE agent_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project TEXT NOT NULL,
                scope TEXT NOT NULL,
                pid INTEGER NOT NULL,
                status TEXT NOT NULL,
                agent_cli TEXT NOT NULL,
                started_at TEXT NOT NULL,
                exited_at TEXT,
                exit_code INTEGER,
                log_path TEXT NOT NULL
            );
            INSERT INTO schema_version (version) VALUES (23);
            INSERT INTO agent_sessions
                (project, scope, pid, status, agent_cli, started_at, log_path)
                VALUES ('p', 's', 0, 'exited', 'claude',
                        '2026-05-21T00:00:00+00:00', '/tmp/log');
        """)
        conn.commit()
        conn.close()

        init_db(db_path)

        conn = get_connection(db_path)
        try:
            cols = {r["name"]
                    for r in conn.execute("PRAGMA table_info(agent_sessions)")}
            assert "claude_session_id" in cols
            row = conn.execute(
                "SELECT claude_session_id FROM agent_sessions WHERE project='p'"
            ).fetchone()
            assert row["claude_session_id"] is None
            _insert_session_row(conn, project="p", scope="s2",
                                status="exited", claude_session_id="x")
            row = conn.execute(
                "SELECT claude_session_id FROM agent_sessions WHERE scope='s2'"
            ).fetchone()
            assert row["claude_session_id"] == "x"
        finally:
            conn.close()
