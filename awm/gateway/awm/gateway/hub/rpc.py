"""Service RPC envelope layer.

Three families of frames travel on the per-service control WS:

* **rpc**  — ``call`` / ``reply`` / ``notify``. ``POST /svc/<name>/fn/<fn>``
  on the client side becomes a ``call`` (or ``notify`` if declared
  no-response); the hub awaits a matching ``reply`` and returns it as
  HTTP JSON.
* **emitter pub/sub** — ``sub`` / ``unsub`` / ``emit``. The hub fans
  ``emit`` frames out as JSON WS frames to every browser subscriber on
  the topic. Topics declared ``transport: "direct"`` skip this path and
  use a bridge instead.
* **session lifecycle** — ``session.open`` / ``session.opened`` /
  ``session.frame`` / ``session.close``. Non-direct sessions wrap each
  client WS frame in ``session.frame`` envelopes; direct sessions
  negotiate a bridge_id and the hub byte-relays raw frames between
  browser ↔ bridge.

The control channel state is in-memory only. On hub restart the supervisor
journals the registration so the service can reconnect; pending calls are
dropped (callers get a 503 on next attempt). The bridges are similarly
ephemeral — bridge ids are not durable.

One ControlChannel exists per registered service, keyed by service_id;
``get_control(service_id)`` returns it (or None if the service hasn't
opened its control WS yet).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("awm.hub.rpc")


# ---------------------------------------------------------------------------
# Per-service state
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    """One outstanding ``call`` awaiting a ``reply``."""

    future: asyncio.Future
    fn: str


@dataclass
class _Subscriber:
    """One browser-side subscriber on a non-direct emitter topic."""

    sub_id: str
    queue: asyncio.Queue[Any]


@dataclass
class _Session:
    """One open session.

    For a non-direct session the hub holds both the browser WS and a
    server-side queue; ``session.frame`` envelopes round-trip through
    ``service_inbound`` and ``client_outbound``. For a direct session
    the queues are unused — the hub bridges raw frames at the WS layer
    via ``Bridge`` instead.
    """

    session_id: str
    kind: str
    direct: bool
    # Client-bound inbound queue: frames from the service for this session.
    client_outbound: asyncio.Queue[dict[str, Any]] = field(
        default_factory=asyncio.Queue,
    )
    # Signals the session.opened reply.
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    open_ok: bool = False
    open_error: str | None = None
    bridge_id: str | None = None
    closed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class Bridge:
    """One direct-session bridge.

    The service opens an upstream WS at ``/hub/service/bridge/{sid}/{bid}``
    after seeing ``session.open`` carrying ``bridge_id``. The browser
    opens its WS at ``/svc/<name>/session/<session_id>``. The relay
    coroutine forwards frames in both directions verbatim — text as
    text, binary as binary, close codes propagated.
    """

    bridge_id: str
    session_id: str
    upstream_ready: asyncio.Event = field(default_factory=asyncio.Event)
    # Set by the upstream-WS handler once it accepts; consumed by the
    # client-WS handler that does the actual relay.
    upstream_obj: Any | None = None


class ControlChannel:
    """All hub-side state for one service's control WS."""

    def __init__(self, service_id: str) -> None:
        self.service_id = service_id
        self.api: dict[str, Any] = {}
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self._send_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pending: dict[str, _Pending] = {}
        # topic -> {sub_id -> _Subscriber}
        self._subs: dict[str, dict[str, _Subscriber]] = {}
        self._sessions: dict[str, _Session] = {}
        self._bridges: dict[str, Bridge] = {}

    # -- API manifest helpers ---------------------------------------------

    def set_api(self, api: dict[str, Any]) -> None:
        self.api = api or {}
        self.ready.set()

    def function_spec(self, name: str) -> dict[str, Any] | None:
        for fn in self.api.get("functions", []) or []:
            if isinstance(fn, dict) and fn.get("name") == name:
                return fn
        return None

    def emitter_spec(self, topic: str) -> dict[str, Any] | None:
        for em in self.api.get("emitters", []) or []:
            if isinstance(em, dict) and em.get("topic") == topic:
                return em
        return None

    def session_spec(self, kind: str) -> dict[str, Any] | None:
        for s in self.api.get("sessions", []) or []:
            if isinstance(s, dict) and s.get("kind") == kind:
                return s
        return None

    # -- Service-bound send -----------------------------------------------

    def enqueue(self, envelope: dict[str, Any]) -> None:
        """Schedule one envelope to be sent to the service."""
        self._send_queue.put_nowait(envelope)

    async def next_outbound(self) -> dict[str, Any]:
        return await self._send_queue.get()

    # -- RPC --------------------------------------------------------------

    async def call(
        self,
        fn: str,
        args: Any,
        *,
        as_: str | None = None,
        timeout: float = 30.0,
    ) -> Any:
        call_id = secrets.token_urlsafe(8)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = _Pending(future=fut, fn=fn)
        env: dict[str, Any] = {"kind": "call", "id": call_id, "fn": fn, "args": args}
        if as_ is not None:
            env["as"] = as_
        self.enqueue(env)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(call_id, None)

    def notify(
        self,
        fn: str,
        args: Any,
        *,
        as_: str | None = None,
    ) -> None:
        env: dict[str, Any] = {"kind": "notify", "fn": fn, "args": args}
        if as_ is not None:
            env["as"] = as_
        self.enqueue(env)

    def handle_reply(self, env: dict[str, Any]) -> None:
        call_id = env.get("id")
        pending = self._pending.pop(call_id, None) if call_id else None
        if pending is None:
            log.debug("dropped reply for unknown call_id=%s", call_id)
            return
        if env.get("ok"):
            pending.future.set_result(env.get("result"))
        else:
            err = env.get("error") or "service reported error"
            pending.future.set_exception(RpcError(str(err)))

    # -- Emitters ---------------------------------------------------------

    def add_subscriber(self, topic: str, *, as_: str | None = None) -> _Subscriber:
        sub_id = secrets.token_urlsafe(8)
        sub = _Subscriber(sub_id=sub_id, queue=asyncio.Queue(maxsize=256))
        self._subs.setdefault(topic, {})[sub_id] = sub
        env: dict[str, Any] = {"kind": "sub", "topic": topic, "sub_id": sub_id}
        if as_ is not None:
            env["as"] = as_
        self.enqueue(env)
        return sub

    def drop_subscriber(self, topic: str, sub_id: str) -> None:
        bucket = self._subs.get(topic)
        if not bucket or sub_id not in bucket:
            return
        del bucket[sub_id]
        if not bucket:
            del self._subs[topic]
        self.enqueue({"kind": "unsub", "topic": topic, "sub_id": sub_id})

    def handle_emit(self, env: dict[str, Any]) -> None:
        topic = env.get("topic") or ""
        payload = env.get("payload")
        bucket = self._subs.get(topic, {})
        for sub in list(bucket.values()):
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                log.warning("dropping emit on %s for slow subscriber %s",
                            topic, sub.sub_id)

    # -- Sessions ---------------------------------------------------------

    async def open_session(
        self,
        kind: str,
        init: Any,
        *,
        direct: bool,
        as_: str | None = None,
        timeout: float = 10.0,
    ) -> _Session:
        session_id = secrets.token_urlsafe(8)
        sess = _Session(session_id=session_id, kind=kind, direct=direct)
        if direct:
            sess.bridge_id = secrets.token_urlsafe(8)
            self._bridges[sess.bridge_id] = Bridge(
                bridge_id=sess.bridge_id, session_id=session_id,
            )
        self._sessions[session_id] = sess
        env: dict[str, Any] = {
            "kind": "session.open",
            "session_id": session_id,
            "session_kind": kind,
            "init": init,
        }
        if direct and sess.bridge_id is not None:
            env["bridge_id"] = sess.bridge_id
        if as_ is not None:
            env["as"] = as_
        self.enqueue(env)
        try:
            await asyncio.wait_for(sess.opened.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._sessions.pop(session_id, None)
            if sess.bridge_id is not None:
                self._bridges.pop(sess.bridge_id, None)
            raise
        if not sess.open_ok:
            self._sessions.pop(session_id, None)
            if sess.bridge_id is not None:
                self._bridges.pop(sess.bridge_id, None)
            raise RpcError(sess.open_error or "service refused session")
        return sess

    def get_session(self, session_id: str) -> _Session | None:
        return self._sessions.get(session_id)

    def get_bridge(self, bridge_id: str) -> Bridge | None:
        return self._bridges.get(bridge_id)

    def drop_session(self, session_id: str) -> None:
        sess = self._sessions.pop(session_id, None)
        if sess is None:
            return
        sess.closed.set()
        if sess.bridge_id is not None:
            self._bridges.pop(sess.bridge_id, None)

    def handle_session_opened(self, env: dict[str, Any]) -> None:
        session_id = env.get("session_id")
        sess = self._sessions.get(session_id) if session_id else None
        if sess is None:
            return
        sess.open_ok = bool(env.get("ok"))
        sess.open_error = env.get("error")
        sess.opened.set()

    def handle_session_frame(self, env: dict[str, Any]) -> None:
        session_id = env.get("session_id")
        sess = self._sessions.get(session_id) if session_id else None
        if sess is None or sess.direct:
            return
        try:
            sess.client_outbound.put_nowait(env)
        except asyncio.QueueFull:
            log.warning("dropping session.frame for slow client session=%s",
                        session_id)

    def handle_session_close(self, env: dict[str, Any]) -> None:
        session_id = env.get("session_id")
        if session_id:
            self.drop_session(session_id)

    # -- Lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Tear down all sessions, subscribers, and pending calls."""
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(
                    RpcError("service control WS closed mid-call")
                )
        self._pending.clear()
        self._subs.clear()
        for sess in list(self._sessions.values()):
            self.drop_session(sess.session_id)
        self._bridges.clear()
        self.closed.set()


class RpcError(Exception):
    """Service-side error or transport failure during a call/session."""


# ---------------------------------------------------------------------------
# Singleton registry of ControlChannels
# ---------------------------------------------------------------------------

_channels: dict[str, ControlChannel] = {}


def get_control(service_id: str) -> ControlChannel | None:
    return _channels.get(service_id)


def ensure_control(service_id: str) -> ControlChannel:
    ch = _channels.get(service_id)
    if ch is None:
        ch = ControlChannel(service_id)
        _channels[service_id] = ch
    return ch


def drop_control(service_id: str) -> None:
    ch = _channels.pop(service_id, None)
    if ch is not None:
        ch.close()


def _reset_for_tests() -> None:
    for ch in list(_channels.values()):
        ch.close()
    _channels.clear()
