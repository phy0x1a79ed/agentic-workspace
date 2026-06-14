"""T2 — agentcore adoption + live agent_subscribe + scope→stdin delivery.

Covers the pieces that landed when the agents service moved onto agentcore:
  - record_event: persists a normalized AgentEvent + returns the wire act.
  - read_acts_after / DAO cursor: monotonic (ts, id) cursored backfill.
  - agent_bus: in-process live fan-out + lagged backpressure sentinel.
  - _is_own_author: the don't-self-deliver filter for scope→stdin delivery.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.agent, pytest.mark.smoke]

import awm.agents.agent_instances as ai_mod
import awm.agents.agent_transcript as transcript_mod
import awm.agents.agent_bus as bus_mod
from awm.agents.dao import AgentsDAO, init as dao_init
from awm.agents._time import now_ms


@pytest.fixture(autouse=True)
def _agents_db(tmp_path, monkeypatch):
    import awm.agents.dao as dao_mod
    import awm.persistence.databases as dbs_mod
    monkeypatch.setattr("awm.config.AWM_DIR", tmp_path)
    monkeypatch.setattr(dbs_mod, "SERVICES_DIR", tmp_path / "services")
    dao_mod._initialized = False
    ai_mod._dao = None
    dao_init()
    yield
    dao_mod._initialized = False
    ai_mod._dao = None


class _Ev:
    """Minimal agentcore AgentEvent stand-in (duck-typed)."""
    def __init__(self, kind, text=None, data=None, id="evid", ts=123):
        self.kind = kind
        self.text = text
        self.data = data
        self.id = id
        self.ts = ts


class _Sess:
    def __init__(self, project="p", scope="s", instance_id=1):
        self.project = project
        self.scope = scope
        self.id = instance_id


class _ReaderSess:
    """Fuller session stand-in for driving ``_reader_loop`` directly."""
    def __init__(self, project="p", scope="s", instance_id=1):
        self.project = project
        self.scope = scope
        self.id = instance_id
        self._partial_accum: dict = {}
        self._msg_accum: dict = {}
        self.cli_session_id = None
        self.model = None
        self.context_max = None
        self.context_used = 0
        self.claude_slash_commands: list = []


async def _aiter(events):
    for ev in events:
        yield ev


# ---------------------------------------------------------------------------
# record_event → wire act + persistence
# ---------------------------------------------------------------------------

class TestRecordEvent:
    def test_persists_and_returns_wire_act(self):
        act = transcript_mod.record_event(
            _Sess(), _Ev("message", text="hello", data={"x": 1}))
        assert act is not None
        assert act["kind"] == "message"
        assert act["body"] == "hello"
        assert "id" in act and act["id"]
        # The wire act id is the agent_transcript row id (the dedupe key).
        rows = transcript_mod.read_session("p", "s")
        assert len(rows) == 1
        assert rows[0]["id"] == act["id"]
        assert rows[0]["kind"] == "message"

    def test_preserves_event_kinds_verbatim(self):
        for kind in ("partial", "tool_use", "tool_result", "status"):
            transcript_mod.record_event(_Sess(), _Ev(kind, text="t"))
        kinds = [r["kind"] for r in transcript_mod.read_session("p", "s")]
        assert kinds == ["partial", "tool_use", "tool_result", "status"]

    def test_message_event_wakes_assistant_turn_subscriber(self):
        sess = _Sess(instance_id=99)
        q = transcript_mod.subscribe_assistant_turns(99)
        try:
            transcript_mod.record_event(sess, _Ev("message", text="done"))
            assert q.get_nowait() == "done"
        finally:
            transcript_mod.unsubscribe_assistant_turns(99, q)


# ---------------------------------------------------------------------------
# Cursored backfill (ts ms + id tiebreak)
# ---------------------------------------------------------------------------

class TestCursorReplay:
    def _insert(self, kind, body, ts, rid):
        dao = AgentsDAO()
        import json as _j
        dao.execute(
            "INSERT INTO agent_transcript (id, project, scope, kind, body, meta, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rid, "p", "s", kind, body, _j.dumps({}), ts),
        )

    def test_no_cursor_returns_all_ordered(self):
        self._insert("message", "a", 100, "id-a")
        self._insert("message", "b", 200, "id-b")
        acts = transcript_mod.read_acts_after("p", "s")
        assert [a["body"] for a in acts] == ["a", "b"]

    def test_cursor_excludes_seen_and_keeps_newer(self):
        self._insert("message", "a", 100, "id-a")
        self._insert("message", "b", 200, "id-b")
        self._insert("message", "c", 300, "id-c")
        acts = transcript_mod.read_acts_after("p", "s", after_ts=200, after_id="id-b")
        assert [a["body"] for a in acts] == ["c"]

    def test_same_ts_id_tiebreak(self):
        # Two acts at the same ts: the id tiebreak keeps ordering deterministic
        # and the cursor at (ts, id-a) excludes id-a but keeps id-b.
        self._insert("message", "a", 500, "id-a")
        self._insert("message", "b", 500, "id-b")
        acts = transcript_mod.read_acts_after("p", "s", after_ts=500, after_id="id-a")
        assert [a["body"] for a in acts] == ["b"]


# ---------------------------------------------------------------------------
# In-process act bus
# ---------------------------------------------------------------------------

class TestAgentBus:
    @pytest.mark.asyncio
    async def test_publish_reaches_live_subscriber(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        await bus_mod.attach_live("p", "s", q)
        try:
            bus_mod.publish_act("p", "s", {"id": "1", "kind": "message"})
            ev = q.get_nowait()
            assert ev["type"] == "act"
            assert ev["act"]["id"] == "1"
        finally:
            await bus_mod.detach_live("p", "s", q)

    @pytest.mark.asyncio
    async def test_full_queue_drops_without_raising(self):
        # Mirrors the scopes channel `lagged` backpressure pattern: a publish to
        # a full subscriber queue drops (best-effort lagged sentinel) rather
        # than block the reader loop or raise. The already-queued acts survive;
        # the live session writer (which sees the `lagged` sentinel under real
        # load) closes 1011 and the client reconnects with its cursor.
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        await bus_mod.attach_live("p", "s", q)
        try:
            bus_mod.publish_act("p", "s", {"id": "1", "kind": "partial"})
            bus_mod.publish_act("p", "s", {"id": "2", "kind": "partial"})
            # Full now — these must not raise (the reader loop must never be
            # stalled or crashed by a slow subscriber).
            bus_mod.publish_act("p", "s", {"id": "3", "kind": "partial"})
            bus_mod.publish_act("p", "s", {"id": "4", "kind": "message"})
            assert q.get_nowait()["act"]["id"] == "1"
            assert q.get_nowait()["act"]["id"] == "2"
            assert q.empty()
        finally:
            await bus_mod.detach_live("p", "s", q)

    @pytest.mark.asyncio
    async def test_detach_stops_delivery(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        await bus_mod.attach_live("p", "s", q)
        await bus_mod.detach_live("p", "s", q)
        bus_mod.publish_act("p", "s", {"id": "1", "kind": "message"})
        assert q.empty()


# ---------------------------------------------------------------------------
# scope → stdin: don't-self-deliver filter
# ---------------------------------------------------------------------------

class TestOwnAuthorFilter:
    def _sess(self):
        s = _Sess(project="awm", scope="dev")
        return s

    def test_own_agent_ref_is_self(self):
        assert ai_mod._is_own_author(self._sess(), "agent:awm/dev") is True
        assert ai_mod._is_own_author(self._sess(), "scope:awm/dev") is True
        assert ai_mod._is_own_author(self._sess(), "awm/dev") is True

    def test_other_authors_are_not_self(self):
        assert ai_mod._is_own_author(self._sess(), "user:alice") is False
        assert ai_mod._is_own_author(self._sess(), "agent:awm/other") is False
        assert ai_mod._is_own_author(self._sess(), "") is False


# ---------------------------------------------------------------------------
# Idempotent streaming dedupe — partials live-only, message upserts once
# ---------------------------------------------------------------------------

class TestStreamingDedupe:
    @pytest.mark.asyncio
    async def test_partials_live_only_message_upserts_once(self):
        sess = _ReaderSess()
        events = [
            _Ev("partial", text="An", data={"message_id": "m1"}, id="p1"),
            _Ev("partial", text="ytime! 👋", data={"message_id": "m1"}, id="p2"),
            _Ev("message", text="Anytime! 👋", data={"message_id": "m1"}, id="a1"),
            _Ev("result", text="Anytime! 👋",
                data={"usage": {}, "session_id": "s"}, id="r1"),
        ]
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        await bus_mod.attach_live(sess.project, sess.scope, q)
        try:
            await ai_mod._reader_loop(sess, _aiter(events))
        finally:
            await bus_mod.detach_live(sess.project, sess.scope, q)

        rows = transcript_mod.read_session(sess.project, sess.scope)
        kinds = [r["kind"] for r in rows]
        # No partial flood persisted; exactly one durable message row (upsert).
        assert kinds.count("partial") == 0
        assert kinds.count("message") == 1
        msg = next(r for r in rows if r["kind"] == "message")
        # The durable row id IS the message id (the stable dedupe/coalesce key).
        assert msg["id"] == "m1"
        assert msg["body"] == "Anytime! 👋"
        import json as _j
        assert _j.loads(msg["meta"])["message_id"] == "m1"
        # result is still persisted (kept for usage/session meta, hidden in UI).
        assert kinds.count("result") == 1

        # The live bus saw the growing partial bubble (accumulated text), each
        # tagged with the shared message id, then the finalized message — and
        # NO duplicate final row.
        published = []
        while not q.empty():
            published.append(q.get_nowait())
        partial_acts = [e["act"] for e in published
                        if e.get("type") == "act" and e["act"]["kind"] == "partial"]
        assert [a["body"] for a in partial_acts] == ["An", "Anytime! 👋"]
        assert all(a["meta"]["message_id"] == "m1" for a in partial_acts)
        msg_acts = [e["act"] for e in published
                    if e.get("type") == "act" and e["act"]["kind"] == "message"]
        assert [a["body"] for a in msg_acts] == ["Anytime! 👋"]
        assert msg_acts[0]["id"] == "m1"

        # Accumulators reset at the turn boundary (`result`).
        assert sess._partial_accum == {}
        assert sess._msg_accum == {}

    @pytest.mark.asyncio
    async def test_multi_block_message_accumulates_into_one_row(self):
        sess = _ReaderSess()
        events = [
            _Ev("message", text="first block", data={"message_id": "m2"}, id="a1"),
            _Ev("message", text="second block", data={"message_id": "m2"}, id="a2"),
        ]
        await ai_mod._reader_loop(sess, _aiter(events))
        rows = transcript_mod.read_session(sess.project, sess.scope)
        msgs = [r for r in rows if r["kind"] == "message"]
        assert len(msgs) == 1
        assert msgs[0]["id"] == "m2"
        assert msgs[0]["body"] == "first block\nsecond block"


# ---------------------------------------------------------------------------
# Scope → stdin: live subscription delivery (not a poll)
# ---------------------------------------------------------------------------

from awm.agents._time import ms_to_iso  # noqa: E402


class _DeliverSess:
    def __init__(self, project="awm", scope="dev"):
        self.project = project
        self.scope = scope
        self.scope_poll_after_ts = None
        self.input_queue: asyncio.Queue = asyncio.Queue(maxsize=128)


class TestScopeDelivery:
    def test_maybe_deliver_filters_and_cursors(self):
        s = _DeliverSess()
        # non-message ignored, cursor untouched
        ai_mod._maybe_deliver_post(s, {
            "kind": "journal", "ts": ms_to_iso(100),
            "author": "user:bob", "body": "x"})
        assert s.input_queue.empty()
        # own-author message skipped (but the cursor advances past it)
        ai_mod._maybe_deliver_post(s, {
            "kind": "message", "ts": ms_to_iso(100),
            "author": "agent:awm/dev", "body": "self"})
        assert s.input_queue.empty()
        # a fresh human message is delivered
        ai_mod._maybe_deliver_post(s, {
            "kind": "message", "ts": ms_to_iso(200),
            "author": "user:bob", "body": "hi"})
        assert s.input_queue.get_nowait() == ("user:bob", "hi")
        # a stale (≤ cursor) message never regresses / re-delivers
        ai_mod._maybe_deliver_post(s, {
            "kind": "message", "ts": ms_to_iso(150),
            "author": "user:bob", "body": "old"})
        assert s.input_queue.empty()

    @pytest.mark.asyncio
    async def test_resync_delivers_gap_oldest_first(self, monkeypatch):
        s = _DeliverSess()
        # scope_fetch(order='desc') returns newest-first.
        posts = [
            {"kind": "message", "ts": ms_to_iso(300), "author": "user:bob", "body": "c"},
            {"kind": "message", "ts": ms_to_iso(200), "author": "user:bob", "body": "b"},
            {"kind": "message", "ts": ms_to_iso(100), "author": "user:bob", "body": "a"},
        ]

        async def fake_call(service, fn, args, **kw):
            assert service == "scopes" and fn == "scope_fetch"
            return {"posts": posts}

        monkeypatch.setattr(ai_mod.gatewayclient, "call", fake_call)
        await ai_mod._resync_scope_posts(s)
        drained = []
        while not s.input_queue.empty():
            drained.append(s.input_queue.get_nowait())
        assert drained == [("user:bob", "a"), ("user:bob", "b"), ("user:bob", "c")]

    @pytest.mark.asyncio
    async def test_delivery_loop_subscribes_and_filters_scope(self, monkeypatch):
        s = _DeliverSess()

        async def fake_call(service, fn, args, **kw):
            return {"posts": []}  # empty seed + resync

        events = [
            {"project": "awm", "scope": "dev",
             "post": {"kind": "message", "ts": ms_to_iso(500),
                      "author": "user:bob", "body": "live!"}},
            {"project": "other", "scope": "x",
             "post": {"kind": "message", "ts": ms_to_iso(600),
                      "author": "user:bob", "body": "nope"}},
        ]

        async def fake_subscribe(service, topic, **kw):
            assert service == "scopes" and topic == "posts"
            for e in events:
                yield e
            await asyncio.sleep(3600)  # stay "live" so the loop doesn't respin

        monkeypatch.setattr(ai_mod.gatewayclient, "call", fake_call)
        monkeypatch.setattr(ai_mod.gatewayclient, "subscribe", fake_subscribe)

        task = asyncio.create_task(ai_mod._scope_delivery_loop(s))
        for _ in range(100):
            if not s.input_queue.empty():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        drained = []
        while not s.input_queue.empty():
            drained.append(s.input_queue.get_nowait())
        # Only this session's (project, scope) is routed; the other-scope event
        # is filtered out.
        assert drained == [("user:bob", "live!")]
