"""Unit tests for awm.services.hub.rpc.ControlChannel.

In-process only — no FastAPI, no real WS. Drives the state machine the way
the api/hub.py route handlers and proxy.py translators would, so a regression
in call/reply, sub/emit, or session lifecycle surfaces here before it reaches
the integration suite.
"""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.unit, pytest.mark.smoke]

import asyncio

import pytest

from awm.services.hub.rpc import (
    Bridge,
    ControlChannel,
    RpcError,
    _channels,
)


@pytest.fixture(autouse=True)
def _isolate_channels():
    """ControlChannel singleton table is module-global; clear before each
    test so cross-test leaks can't poison subscriber/session lookups."""
    _channels.clear()
    yield
    _channels.clear()


@pytest.fixture()
def ch():
    return ControlChannel("svc-test")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# RPC: call / reply / notify
# ---------------------------------------------------------------------------


class TestCallReply:
    def test_call_resolves_on_ok_reply(self, ch):
        async def go():
            task = asyncio.create_task(ch.call("echo", {"x": 1}))
            env = await ch.next_outbound()
            assert env["kind"] == "call"
            assert env["fn"] == "echo"
            assert env["args"] == {"x": 1}
            call_id = env["id"]
            ch.handle_reply({"kind": "reply", "id": call_id,
                             "ok": True, "result": {"out": 42}})
            assert await task == {"out": 42}
            assert ch._pending == {}
        _run(go())

    def test_call_raises_on_error_reply(self, ch):
        async def go():
            task = asyncio.create_task(ch.call("boom", None))
            env = await ch.next_outbound()
            ch.handle_reply({"kind": "reply", "id": env["id"],
                             "ok": False, "error": "kaboom"})
            with pytest.raises(RpcError, match="kaboom"):
                await task
            assert ch._pending == {}
        _run(go())

    def test_call_times_out(self, ch):
        async def go():
            with pytest.raises(asyncio.TimeoutError):
                await ch.call("slow", None, timeout=0.05)
            assert ch._pending == {}
        _run(go())

    def test_reply_for_unknown_id_is_dropped(self, ch):
        # Should NOT raise — late replies after timeout-cleanup are a normal
        # race; the channel logs and moves on.
        ch.handle_reply({"kind": "reply", "id": "no-such-id",
                         "ok": True, "result": 1})

    def test_notify_emits_envelope_without_pending(self, ch):
        async def go():
            ch.notify("ping", {"a": 1}, as_="user:alice")
            env = await ch.next_outbound()
            assert env == {"kind": "notify", "fn": "ping",
                           "args": {"a": 1}, "as": "user:alice"}
            assert ch._pending == {}
        _run(go())

    def test_call_carries_as_when_provided(self, ch):
        async def go():
            task = asyncio.create_task(ch.call("f", None, as_="user:bob"))
            env = await ch.next_outbound()
            assert env["as"] == "user:bob"
            ch.handle_reply({"kind": "reply", "id": env["id"],
                             "ok": True, "result": None})
            await task
        _run(go())


# ---------------------------------------------------------------------------
# API manifest helpers
# ---------------------------------------------------------------------------


class TestApiManifest:
    def test_set_api_marks_ready(self, ch):
        assert not ch.ready.is_set()
        ch.set_api({"functions": [{"name": "f"}]})
        assert ch.ready.is_set()
        assert ch.function_spec("f") == {"name": "f"}
        assert ch.function_spec("nope") is None

    def test_emitter_and_session_spec_lookup(self, ch):
        ch.set_api({
            "emitters": [{"topic": "tick", "transport": "direct"}],
            "sessions": [{"kind": "call", "transport": "direct"}],
        })
        assert ch.emitter_spec("tick") == {"topic": "tick",
                                            "transport": "direct"}
        assert ch.session_spec("call")["transport"] == "direct"
        assert ch.emitter_spec("missing") is None
        assert ch.session_spec("missing") is None

    def test_set_api_with_none_treated_as_empty(self, ch):
        ch.set_api(None)
        assert ch.api == {}
        assert ch.ready.is_set()


# ---------------------------------------------------------------------------
# Emitter pub/sub
# ---------------------------------------------------------------------------


class TestEmitters:
    def test_sub_enqueues_envelope(self, ch):
        async def go():
            sub = ch.add_subscriber("tick", as_="user:carol")
            env = await ch.next_outbound()
            assert env["kind"] == "sub"
            assert env["topic"] == "tick"
            assert env["sub_id"] == sub.sub_id
            assert env["as"] == "user:carol"
        _run(go())

    def test_emit_reaches_all_subscribers(self, ch):
        async def go():
            sub_a = ch.add_subscriber("tick")
            sub_b = ch.add_subscriber("tick")
            # Drain the two sub envelopes from the outbound queue.
            await ch.next_outbound()
            await ch.next_outbound()
            ch.handle_emit({"topic": "tick", "payload": {"n": 1}})
            assert sub_a.queue.get_nowait() == {"n": 1}
            assert sub_b.queue.get_nowait() == {"n": 1}
        _run(go())

    def test_emit_on_unknown_topic_is_silent(self, ch):
        # No subscribers — nothing crashes, no envelope queued.
        ch.handle_emit({"topic": "nobody", "payload": "x"})

    def test_queue_full_does_not_raise(self, ch):
        async def go():
            sub = ch.add_subscriber("tick")
            await ch.next_outbound()  # drain sub envelope
            # _Subscriber.queue has maxsize=256; fill it past that.
            for i in range(260):
                ch.handle_emit({"topic": "tick", "payload": i})
            # First 256 made it; the rest were dropped silently.
            assert sub.queue.qsize() == 256
        _run(go())

    def test_drop_subscriber_unsubscribes_and_no_further_delivery(self, ch):
        async def go():
            sub = ch.add_subscriber("tick")
            await ch.next_outbound()  # sub envelope
            ch.drop_subscriber("tick", sub.sub_id)
            unsub = await ch.next_outbound()
            assert unsub == {"kind": "unsub", "topic": "tick",
                             "sub_id": sub.sub_id}
            # Topic bucket gone — emit reaches no one.
            ch.handle_emit({"topic": "tick", "payload": "x"})
            assert sub.queue.qsize() == 0
            # Idempotent: dropping again is a no-op.
            ch.drop_subscriber("tick", sub.sub_id)
        _run(go())


# ---------------------------------------------------------------------------
# Sessions: open (direct + non-direct), frame routing, drop
# ---------------------------------------------------------------------------


class TestSessions:
    def test_open_non_direct_session_succeeds(self, ch):
        async def go():
            task = asyncio.create_task(
                ch.open_session("call", {"init": True}, direct=False),
            )
            env = await ch.next_outbound()
            assert env["kind"] == "session.open"
            assert env["session_kind"] == "call"
            assert env["init"] == {"init": True}
            assert "bridge_id" not in env
            sid = env["session_id"]
            ch.handle_session_opened({"session_id": sid, "ok": True})
            sess = await task
            assert sess.session_id == sid
            assert sess.direct is False
            assert sess.bridge_id is None
        _run(go())

    def test_open_direct_session_allocates_bridge(self, ch):
        async def go():
            task = asyncio.create_task(
                ch.open_session("stream", None, direct=True),
            )
            env = await ch.next_outbound()
            assert "bridge_id" in env
            bid = env["bridge_id"]
            sid = env["session_id"]
            assert isinstance(ch.get_bridge(bid), Bridge)
            ch.handle_session_opened({"session_id": sid, "ok": True})
            sess = await task
            assert sess.direct is True
            assert sess.bridge_id == bid
        _run(go())

    def test_open_session_failure_drops_state(self, ch):
        async def go():
            task = asyncio.create_task(
                ch.open_session("call", None, direct=False),
            )
            env = await ch.next_outbound()
            sid = env["session_id"]
            ch.handle_session_opened({"session_id": sid, "ok": False,
                                       "error": "no capacity"})
            with pytest.raises(RpcError, match="no capacity"):
                await task
            assert ch.get_session(sid) is None
        _run(go())

    def test_open_session_timeout_cleans_up_bridge(self, ch):
        async def go():
            with pytest.raises(asyncio.TimeoutError):
                await ch.open_session("stream", None, direct=True,
                                      timeout=0.05)
            # No leftover sessions/bridges after timeout.
            assert ch._sessions == {}
            assert ch._bridges == {}
        _run(go())

    def test_non_direct_frame_routes_to_client_outbound(self, ch):
        async def go():
            task = asyncio.create_task(
                ch.open_session("call", None, direct=False),
            )
            env = await ch.next_outbound()
            sid = env["session_id"]
            ch.handle_session_opened({"session_id": sid, "ok": True})
            sess = await task
            ch.handle_session_frame({"session_id": sid,
                                      "payload": {"json": {"n": 1}}})
            received = sess.client_outbound.get_nowait()
            assert received["payload"] == {"json": {"n": 1}}
        _run(go())

    def test_direct_session_frame_is_dropped(self, ch):
        """Direct sessions use the bridge byte-relay, not session.frame.
        Any frame envelope that arrives is ignored — proxy.py owns the
        relay path."""
        async def go():
            task = asyncio.create_task(
                ch.open_session("stream", None, direct=True),
            )
            env = await ch.next_outbound()
            sid = env["session_id"]
            ch.handle_session_opened({"session_id": sid, "ok": True})
            sess = await task
            ch.handle_session_frame({"session_id": sid,
                                      "payload": {"json": {"n": 1}}})
            assert sess.client_outbound.qsize() == 0
        _run(go())

    def test_frame_for_unknown_session_is_dropped(self, ch):
        # No exception, no state mutation.
        ch.handle_session_frame({"session_id": "nope", "payload": {}})

    def test_drop_session_closes_event_and_clears_bridge(self, ch):
        async def go():
            task = asyncio.create_task(
                ch.open_session("stream", None, direct=True),
            )
            env = await ch.next_outbound()
            sid = env["session_id"]
            bid = env["bridge_id"]
            ch.handle_session_opened({"session_id": sid, "ok": True})
            sess = await task
            ch.drop_session(sid)
            assert sess.closed.is_set()
            assert ch.get_session(sid) is None
            assert ch.get_bridge(bid) is None
        _run(go())

    def test_handle_session_close_calls_drop(self, ch):
        async def go():
            task = asyncio.create_task(
                ch.open_session("call", None, direct=False),
            )
            env = await ch.next_outbound()
            sid = env["session_id"]
            ch.handle_session_opened({"session_id": sid, "ok": True})
            sess = await task
            ch.handle_session_close({"session_id": sid})
            assert sess.closed.is_set()
            assert ch.get_session(sid) is None
        _run(go())


# ---------------------------------------------------------------------------
# Lifecycle: close()
# ---------------------------------------------------------------------------


class TestClose:
    def test_close_fails_pending_calls(self, ch):
        async def go():
            task = asyncio.create_task(ch.call("f", None))
            await ch.next_outbound()  # drain the call envelope
            ch.close()
            with pytest.raises(RpcError, match="control WS closed"):
                await task
        _run(go())

    def test_close_drops_sessions_and_subscribers(self, ch):
        async def go():
            sub = ch.add_subscriber("tick")
            await ch.next_outbound()  # sub envelope
            task = asyncio.create_task(
                ch.open_session("stream", None, direct=True),
            )
            env = await ch.next_outbound()
            sid = env["session_id"]
            bid = env["bridge_id"]
            ch.handle_session_opened({"session_id": sid, "ok": True})
            sess = await task

            ch.close()
            assert ch._subs == {}
            assert ch.get_session(sid) is None
            assert ch.get_bridge(bid) is None
            assert sess.closed.is_set()
            assert ch.closed.is_set()
            # Subscriber's queue object is intact, just orphaned.
            assert sub.queue.qsize() == 0
        _run(go())


# ---------------------------------------------------------------------------
# Singleton registry helpers
# ---------------------------------------------------------------------------


class TestSingletonTable:
    def test_ensure_control_is_idempotent(self):
        from awm.services.hub.rpc import ensure_control, get_control
        first = ensure_control("svc-A")
        second = ensure_control("svc-A")
        assert first is second
        assert get_control("svc-A") is first
        assert get_control("svc-missing") is None

    def test_drop_control_closes_channel(self):
        from awm.services.hub.rpc import drop_control, ensure_control, get_control
        ch = ensure_control("svc-B")
        assert not ch.closed.is_set()
        drop_control("svc-B")
        assert ch.closed.is_set()
        assert get_control("svc-B") is None
        # Idempotent.
        drop_control("svc-B")
