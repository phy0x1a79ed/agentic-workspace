"""Tests for the auto-resume + transcript + synthetic /compact pipeline.

Hermetic by design: no subprocess spawn. The actual end-to-end happy-path
verification ("SIGTERM awm-exposed, watch agents come back") is a manual
smoke documented in the plan. These tests cover everything below that —
DB schema, transcript writes, reconciler classification, replay query
logic, intent tracking, queue scrubbing, and compact-gating.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

from awm.db import get_connection
import awm.services.agent_instances as ai_mod
import awm.services.agent_transcript as transcript_mod
import awm.services.rooms as rooms_svc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _t_offset(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _insert_session(conn, *, project: str = "p", scope: str = "s",
                    status: str = "running", pid: int = 1,
                    agent_cli: str = "claude",
                    claude_session_id: str | None = "cid-1",
                    intent: str = "live") -> int:
    cur = conn.execute(
        "INSERT INTO agent_sessions "
        "(project, scope, pid, status, agent_cli, started_at, log_path, "
        " claude_session_id, intent) "
        "VALUES (?, ?, ?, ?, ?, ?, '/tmp/log', ?, ?)",
        (project, scope, pid, status, agent_cli, _now(), claude_session_id, intent),
    )
    conn.commit()
    return cur.lastrowid


class FakeSession:
    """Minimal duck-typed AgentInstance for transcript-write tests."""
    def __init__(self, sid: int, project: str = "p", scope: str = "s",
                 agent_cli: str = "claude",
                 claude_session_id: str | None = "cid-1"):
        self.id = sid
        self.project = project
        self.scope = scope
        self.agent_cli = agent_cli
        self.claude_session_id = claude_session_id


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

class TestSchema:
    def test_intent_column_exists(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        cols = {r["name"]
                for r in conn.execute("PRAGMA table_info(agent_sessions)")}
        conn.close()
        assert "intent" in cols

    def test_intent_defaults_to_live(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = conn.execute(
            "INSERT INTO agent_sessions "
            "(project, scope, pid, status, agent_cli, started_at, log_path) "
            "VALUES ('p', 's', 1, 'running', 'claude', ?, '/tmp/log')",
            (_now(),),
        ).lastrowid
        conn.commit()
        row = conn.execute(
            "SELECT intent FROM agent_sessions WHERE id=?", (sid,)
        ).fetchone()
        conn.close()
        assert row["intent"] == "live"

    def test_agent_events_table_exists(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        cols = {r["name"]
                for r in conn.execute("PRAGMA table_info(agent_events)")}
        conn.close()
        # Spot-check the columns named in the plan.
        for c in ("session_id", "seq", "direction", "event_type", "body",
                  "claude_session_id", "ts", "project", "scope", "agent_cli"):
            assert c in cols, f"missing {c}"

    def test_agent_resume_queue_table_exists(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        cols = {r["name"]
                for r in conn.execute(
                    "PRAGMA table_info(agent_resume_queue)")}
        conn.close()
        for c in ("session_id", "scheduled_at", "attempts",
                  "prior_exited_at", "primer_text"):
            assert c in cols, f"missing {c}"


# ---------------------------------------------------------------------------
# Transcript writes + classification
# ---------------------------------------------------------------------------

class TestTranscriptWrites:
    def test_record_in_persists_user_event(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        transcript_mod.record_in(sess, "hello world", injection=False)
        rows = transcript_mod.read_session(sid)
        assert len(rows) == 1
        r = rows[0]
        assert r["direction"] == "in"
        assert r["event_type"] == "user"
        body = json.loads(r["body"])
        assert body["content"] == "hello world"
        assert body["injection"] is False

    def test_record_in_with_injection_flag(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        transcript_mod.record_in(sess, "/compact primer", injection=True)
        rows = transcript_mod.read_session(sid)
        body = json.loads(rows[0]["body"])
        assert body["injection"] is True

    def test_record_out_classifies_assistant(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        evt = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
            },
        }
        transcript_mod.record_out(sess, evt)
        rows = transcript_mod.read_session(sid)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "assistant"
        assert rows[0]["direction"] == "out"

    def test_record_out_classifies_init(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        transcript_mod.record_out(sess, {
            "type": "system", "subtype": "init", "session_id": "x",
        })
        rows = transcript_mod.read_session(sid)
        assert rows[0]["event_type"] == "init"

    def test_record_out_classifies_tool_result(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        transcript_mod.record_out(sess, {
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "ok"},
            ]},
        })
        rows = transcript_mod.read_session(sid)
        assert rows[0]["event_type"] == "tool_result"

    def test_record_out_classifies_unknown_as_partial(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        transcript_mod.record_out(sess, {"type": "stream_event"})
        rows = transcript_mod.read_session(sid)
        assert rows[0]["event_type"] == "partial"

    def test_record_raw_out(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        transcript_mod.record_raw_out(sess, "not-json-output")
        rows = transcript_mod.read_session(sid)
        assert rows[0]["event_type"] == "raw"
        assert json.loads(rows[0]["body"])["raw"] == "not-json-output"

    def test_seq_is_monotonic_per_session(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        for body in ("a", "b", "c"):
            transcript_mod.record_in(sess, body)
        rows = transcript_mod.read_session(sid)
        assert [r["seq"] for r in rows] == [1, 2, 3]


class TestTranscriptReads:
    def test_read_recent_assistant_text(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        # Two assistant events; the most recent should win.
        for text in ("first", "second"):
            transcript_mod.record_out(sess, {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                },
            })
        assert transcript_mod.read_recent_assistant_text(sid) == "second"

    def test_has_unmatched_tool_use_true(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        transcript_mod.record_out(sess, {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Bash", "id": "x", "input": {}}],
                "stop_reason": "tool_use",
            },
        })
        assert transcript_mod.has_unmatched_tool_use(sid) is True

    def test_has_unmatched_tool_use_false_after_result(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        transcript_mod.record_out(sess, {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Bash", "id": "x", "input": {}}],
                "stop_reason": "tool_use",
            },
        })
        # tool_result lands → tool is no longer in flight.
        transcript_mod.record_out(sess, {
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "ok"},
            ]},
        })
        assert transcript_mod.has_unmatched_tool_use(sid) is False


class TestAssistantTurnSubscriber:
    @pytest.mark.asyncio
    async def test_end_of_turn_assistant_wakes_subscriber(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        q = transcript_mod.subscribe_assistant_turns(sid)
        try:
            transcript_mod.record_out(sess, {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "the summary"}],
                    "stop_reason": "end_turn",
                },
            })
            got = await asyncio.wait_for(q.get(), timeout=1.0)
            assert got == "the summary"
        finally:
            transcript_mod.unsubscribe_assistant_turns(sid, q)

    @pytest.mark.asyncio
    async def test_partial_assistant_does_not_wake(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess = FakeSession(sid)
        q = transcript_mod.subscribe_assistant_turns(sid)
        try:
            transcript_mod.record_out(sess, {
                "type": "assistant", "partial": True,
                "message": {"content": [{"type": "text", "text": "wip"}]},
            })
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.2)
        finally:
            transcript_mod.unsubscribe_assistant_turns(sid, q)


# ---------------------------------------------------------------------------
# Reconciler: who gets queued, who doesn't
# ---------------------------------------------------------------------------

class TestReconciler:
    def test_dead_claude_with_session_id_is_queued(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(
            conn, project="p", scope="s", pid=999999,  # almost certainly dead
            claude_session_id="cid-A", intent="live",
        )
        conn.close()
        ai_mod.reconcile_on_startup()
        conn = get_connection(awm_workspace["db_path"])
        try:
            queued = conn.execute(
                "SELECT * FROM agent_resume_queue WHERE session_id=?", (sid,)
            ).fetchone()
            sess = conn.execute(
                "SELECT status FROM agent_sessions WHERE id=?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert queued is not None
        assert sess["status"] == "exited"

    def test_dead_claude_with_no_session_id_is_not_queued(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(
            conn, pid=999999, claude_session_id=None, intent="live",
        )
        conn.close()
        ai_mod.reconcile_on_startup()
        conn = get_connection(awm_workspace["db_path"])
        try:
            queued = conn.execute(
                "SELECT * FROM agent_resume_queue WHERE session_id=?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert queued is None

    def test_stopped_intent_skips_queue(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(
            conn, pid=999999, claude_session_id="cid", intent="stopped",
        )
        conn.close()
        ai_mod.reconcile_on_startup()
        conn = get_connection(awm_workspace["db_path"])
        try:
            queued = conn.execute(
                "SELECT * FROM agent_resume_queue WHERE session_id=?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert queued is None

    def test_opencode_is_not_queued(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(
            conn, pid=999999, agent_cli="opencode",
            claude_session_id="oc-cid", intent="live",
        )
        conn.close()
        ai_mod.reconcile_on_startup()
        conn = get_connection(awm_workspace["db_path"])
        try:
            queued = conn.execute(
                "SELECT * FROM agent_resume_queue WHERE session_id=?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert queued is None

    def test_vagrant_rows_are_deleted_not_queued(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(
            conn, project="_vagrant", scope="u-1", pid=999999,
            claude_session_id="cid", intent="live",
        )
        conn.close()
        ai_mod.reconcile_on_startup()
        conn = get_connection(awm_workspace["db_path"])
        try:
            sess = conn.execute(
                "SELECT * FROM agent_sessions WHERE id=?", (sid,)
            ).fetchone()
            queued = conn.execute(
                "SELECT * FROM agent_resume_queue WHERE session_id=?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert sess is None  # vagrant rows deleted outright
        assert queued is None


# ---------------------------------------------------------------------------
# Intent tracking + queue scrubbing
# ---------------------------------------------------------------------------

class TestIntentAndQueueScrub:
    def test_set_intent_persists(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn, intent="live")
        conn.close()
        ai_mod._set_intent(sid, "stopped")
        conn = get_connection(awm_workspace["db_path"])
        try:
            row = conn.execute(
                "SELECT intent FROM agent_sessions WHERE id=?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row["intent"] == "stopped"

    def test_scrub_resume_queue_removes_pending_rows(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn, project="p", scope="s")
        conn.execute(
            "INSERT INTO agent_resume_queue "
            "(session_id, scheduled_at, attempts, prior_exited_at) "
            "VALUES (?, ?, 0, ?)",
            (sid, _now(), _now()),
        )
        conn.commit()
        conn.close()
        n = ai_mod.scrub_resume_queue_for_scope("p", "s")
        assert n == 1
        conn = get_connection(awm_workspace["db_path"])
        try:
            row = conn.execute(
                "SELECT * FROM agent_resume_queue WHERE session_id=?", (sid,)
            ).fetchone()
        finally:
            conn.close()
        assert row is None

    def test_scrub_other_scope_unaffected(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        sid_keep = _insert_session(conn, project="p", scope="keep")
        sid_drop = _insert_session(conn, project="p", scope="drop")
        for s in (sid_keep, sid_drop):
            conn.execute(
                "INSERT INTO agent_resume_queue "
                "(session_id, scheduled_at, attempts, prior_exited_at) "
                "VALUES (?, ?, 0, ?)",
                (s, _now(), _now()),
            )
        conn.commit()
        conn.close()
        ai_mod.scrub_resume_queue_for_scope("p", "drop")
        conn = get_connection(awm_workspace["db_path"])
        try:
            kept = conn.execute(
                "SELECT * FROM agent_resume_queue WHERE session_id=?",
                (sid_keep,),
            ).fetchone()
            dropped = conn.execute(
                "SELECT * FROM agent_resume_queue WHERE session_id=?",
                (sid_drop,),
            ).fetchone()
        finally:
            conn.close()
        assert kept is not None
        assert dropped is None


# ---------------------------------------------------------------------------
# Missed-post replay query semantics
# ---------------------------------------------------------------------------

class TestReplayQuery:
    """Direct tests of the SELECT used by _replay_missed_posts. Avoids
    spawning a session — we're verifying that the filters (kind, author,
    ts) do what the plan promises."""

    SQL = (
        "SELECT * FROM room_posts "
        "WHERE room_id=? AND ts > ? AND author != ? AND kind != 'slash' "
        "ORDER BY ts ASC LIMIT ?"
    )

    def _seed(self, conn, room_id: str, rows: list[dict]) -> None:
        # Ensure the room itself exists (FK).
        conn.execute(
            "INSERT OR IGNORE INTO rooms "
            "(id, host_peer_id, created_at, status) "
            "VALUES (?, '', ?, 'active')",
            (room_id, _now()),
        )
        for r in rows:
            from awm.services.replication.schema import new_uuid
            conn.execute(
                "INSERT INTO room_posts "
                "(uuid, legacy_id, origin_peer, room_id, author, body, kind, ts) "
                "VALUES (?, ?, '', ?, ?, ?, ?, ?)",
                (new_uuid(), r.get("legacy_id", 0), room_id,
                 r["author"], r["body"], r.get("kind", "text"), r["ts"]),
            )
        conn.commit()

    def test_filters_out_slash_kind(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        cutoff = _t_offset(-10)
        self._seed(conn, "r1", [
            {"author": "user", "body": "hi", "ts": _t_offset(0), "kind": "text"},
            {"author": "user", "body": "/yolo", "ts": _t_offset(1), "kind": "slash"},
        ])
        rows = conn.execute(
            self.SQL, ("r1", cutoff, "agent:p/s", 200)
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["kind"] == "text"

    def test_filters_out_self_authored(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        cutoff = _t_offset(-10)
        self._seed(conn, "r1", [
            {"author": "user", "body": "hi", "ts": _t_offset(0)},
            {"author": "agent:p/s", "body": "(my reply)", "ts": _t_offset(1)},
        ])
        rows = conn.execute(
            self.SQL, ("r1", cutoff, "agent:p/s", 200)
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["author"] == "user"

    def test_orders_chronologically(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        cutoff = _t_offset(-10)
        self._seed(conn, "r1", [
            {"author": "user", "body": "third", "ts": _t_offset(3)},
            {"author": "user", "body": "first", "ts": _t_offset(1)},
            {"author": "user", "body": "second", "ts": _t_offset(2)},
        ])
        rows = conn.execute(
            self.SQL, ("r1", cutoff, "agent:p/s", 200)
        ).fetchall()
        conn.close()
        assert [r["body"] for r in rows] == ["first", "second", "third"]

    def test_respects_cutoff(self, awm_workspace):
        conn = get_connection(awm_workspace["db_path"])
        # Cutoff is +5s; everything earlier is invisible.
        cutoff = _t_offset(5)
        self._seed(conn, "r1", [
            {"author": "user", "body": "old", "ts": _t_offset(1)},
            {"author": "user", "body": "new", "ts": _t_offset(10)},
        ])
        rows = conn.execute(
            self.SQL, ("r1", cutoff, "agent:p/s", 200)
        ).fetchall()
        conn.close()
        assert [r["body"] for r in rows] == ["new"]


# ---------------------------------------------------------------------------
# Compact gating
# ---------------------------------------------------------------------------

class TestCompactGating:
    @pytest.mark.asyncio
    async def test_no_session_raises(self, awm_workspace):
        # Empty registry — compact_session must raise NoSessionError.
        with pytest.raises(ai_mod.NoSessionError):
            await ai_mod.compact_session("nonexistent/scope")

    @pytest.mark.asyncio
    async def test_opencode_is_gated(self, awm_workspace, monkeypatch):
        # Fake an active opencode session in the in-memory registry.
        class StubSession:
            id = 1
            agent_cli = "opencode"
            compacting = False
        monkeypatch.setitem(ai_mod._by_scope, "p/oc", StubSession())
        msg = await ai_mod.compact_session("p/oc")
        assert "gated" in msg
        assert "opencode" in msg

    @pytest.mark.asyncio
    async def test_reentry_blocked(self, awm_workspace, monkeypatch):
        class StubSession:
            id = 1
            agent_cli = "claude"
            compacting = True  # already in flight
        monkeypatch.setitem(ai_mod._by_scope, "p/c", StubSession())
        msg = await ai_mod.compact_session("p/c")
        assert "already in flight" in msg

    @pytest.mark.asyncio
    async def test_mid_tool_call_refused(self, awm_workspace, monkeypatch):
        # Insert a session row + a transcript event that looks like a
        # tool_use without a matching tool_result.
        conn = get_connection(awm_workspace["db_path"])
        sid = _insert_session(conn)
        conn.close()
        sess_for_xcript = FakeSession(sid)
        transcript_mod.record_out(sess_for_xcript, {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Bash",
                             "id": "x", "input": {}}],
                "stop_reason": "tool_use",
            },
        })

        class StubSession:
            agent_cli = "claude"
            compacting = False
        stub = StubSession()
        stub.id = sid  # so has_unmatched_tool_use looks at our transcript
        monkeypatch.setitem(ai_mod._by_scope, "p/c", stub)
        msg = await ai_mod.compact_session("p/c")
        assert "refused" in msg
        assert "tool" in msg.lower()
