"""Service-originated calls to other plugins' verbs, via the gateway.

A service calls another tool's verb the way every other caller does: ``POST
/invoke`` with the flat name ``<tool>_<verb>``. It names itself with the token
the supervisor minted for this spawn (``AWM_HUB_TOKEN_FILE``), sent as
``X-Awm-Service-Token``, so the gateway knows which plugin calls and can hold
it to the verbs its manifest declares. ``X-Awm-As`` carries a forwarded
principal and ``X-Awm-Caller-Pid`` the session a call acts for.

Public API
----------
``call(tool, verb, args, *, as_=None, timeout=30.0)`` — async; one call.
``call_sync(tool, verb, args, *, as_=None, timeout=30.0)`` — sync variant.
``peer=<node>`` on either runs the verb on that node: the local gateway carries it over
    the ssh edge.
``subscribe(service, topic, *, peer=None, as_=None)`` — async generator over a topic.
``acquire_lease(service, host, *, kind="lease", peer=None)`` — hold a direct-session slot.
``GatewayCallError`` — raised on a non-2xx reply, carrying ``status`` + ``body``.
``RefCache`` — short-TTL, positive-only cache wrapping ``call`` for the
    validate-by-calling hot path.

A result comes back decoded from the JSON text ``/invoke`` returns, a string
that is not JSON stays a string, and a null result reads as ``{}``.

A service calling ITSELF must use its own local DAO — do NOT round-trip
through the gateway to reach your own functions. This client is for
cross-service references only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from awm.gatewayclient.adapter import ServiceAdapter, SessionContext
from awm.gatewayclient.subscription import (
    SupervisedSubscription,
    spawn_supervised,
)

log = logging.getLogger("awm.gatewayclient")

__all__ = [
    "call",
    "call_sync",
    "subscribe",
    "peer_env",
    "SupervisedSubscription",
    "spawn_supervised",
    "acquire_lease",
    "Lease",
    "GatewayCallError",
    "RefCache",
    "hub_base_url",
    "ServiceAdapter",
    "SessionContext",
]

# Keepalive for every subscription socket, pinned rather than inherited from
# the websockets library's defaults. This is the ONLY detector of a half-open
# TCP connection (one where the peer vanished without a FIN), so a future
# change to the library's defaults must not be able to silently remove it.
# ~40 s to notice a dead socket.
_PING_KWARGS = {"ping_interval": 20.0, "ping_timeout": 20.0}

# Default only used when AWM_HUB_URL is unset. Prefer the env var: the hub
# injects it into every service process and it carries the correct per-sandbox
# port (prod 7819, dev sandboxes 7821/7831/...).
_DEFAULT_HUB_URL = "http://127.0.0.1:7819/"


class GatewayCallError(Exception):
    """A gateway call returned a non-2xx response.

    Carries the HTTP ``status`` and the (text) ``body`` the gateway sent, so
    callers can branch on, e.g., 503 (service control channel not open),
    502 (service reported an error), or 504 (service did not reply in time)
    without regex-parsing the message.
    """

    def __init__(self, status: int, body: str, *, service: str = "",
                 fn: str = "") -> None:
        self.status = status
        self.body = body
        self.service = service
        self.fn = fn
        where = f"{service}/{fn}" if service or fn else "<tool>/<verb>"
        super().__init__(f"gateway call {where} failed: HTTP {status}: {body}")


def hub_base_url() -> str:
    """The gateway base URL, normalized without a trailing slash.

    Read from ``AWM_HUB_URL`` (injected into every service process); falls
    back to the prod loopback default when unset.
    """
    return (os.environ.get("AWM_HUB_URL") or _DEFAULT_HUB_URL).rstrip("/")


def service_token() -> str | None:
    """This service's token, from the file the supervisor names, or None."""
    path = os.environ.get("AWM_HUB_TOKEN_FILE")
    if not path:
        return None
    try:
        return Path(path).read_text().strip() or None
    except OSError:
        return None


def _headers(as_: str | None, caller_pid: int | None = None) -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if token := service_token():
        h["X-Awm-Service-Token"] = token
    if as_ is not None:
        h["X-Awm-As"] = as_
    if caller_pid is not None:
        h["X-Awm-Caller-Pid"] = str(int(caller_pid))
    return h


def _parse_reply(resp: httpx.Response, service: str, fn: str) -> Any:
    if resp.status_code < 200 or resp.status_code >= 300:
        raise GatewayCallError(resp.status_code, resp.text,
                               service=service, fn=fn)
    if not resp.content:
        return None
    try:
        return resp.json()
    except json.JSONDecodeError:
        return resp.text


def _invoke_body(tool: str, verb: str, args: dict | None, peer: str | None = None) -> str:
    if peer := _remote(peer):
        return json.dumps({"name": tool, "args": {"verb": verb, "args": args or {}, "peer": peer}})
    return json.dumps({"name": f"{tool}_{verb}", "args": args or {}})


def _result(resp: httpx.Response, tool: str, verb: str) -> Any:
    """The decoded ``result`` of an ``/invoke`` reply. Non-2xx raises."""
    body = _parse_reply(resp, tool, verb)
    result = body.get("result") if isinstance(body, dict) else body
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return result
    return {} if result is None else result


async def call(
    tool: str,
    verb: str,
    args: dict | None = None,
    *,
    peer: str | None = None,
    as_: str | None = None,
    caller_pid: int | None = None,
    timeout: float = 30.0,
) -> Any:
    """Call ``tool``'s ``verb`` through the gateway's ``/invoke`` and return the result.

    ``peer`` names the node the verb runs on; unset or ``local`` runs it here.
    ``caller_pid`` names the session the call acts for: the gateway stamps that
    session's trust and mode, as it does for the session's own calls. Raises
    ``GatewayCallError`` on any non-2xx response (status + body attached).
    """
    async with httpx.AsyncClient(timeout=timeout) as cli:
        resp = await cli.post(f"{hub_base_url()}/invoke", content=_invoke_body(tool, verb, args, peer),
                              headers=_headers(as_, caller_pid))
    return _result(resp, tool, verb)


def call_sync(
    tool: str,
    verb: str,
    args: dict | None = None,
    *,
    peer: str | None = None,
    as_: str | None = None,
    timeout: float = 30.0,
) -> Any:
    """Synchronous variant of :func:`call`, for non-async service code.

    Do not call this from inside the event loop of an async service — use
    :func:`call` there.
    """
    with httpx.Client(timeout=timeout) as cli:
        resp = cli.post(f"{hub_base_url()}/invoke", content=_invoke_body(tool, verb, args, peer),
                        headers=_headers(as_))
    return _result(resp, tool, verb)


def _ws_base() -> str:
    return hub_base_url().replace("https://", "wss://").replace("http://", "ws://")


def _ws_headers(as_: str | None) -> list[tuple[str, str]] | None:
    extra: list[tuple[str, str]] = []
    if token := service_token():
        extra.append(("X-Awm-Service-Token", token))
    if as_ is not None:
        extra.append(("X-Awm-As", as_))
    return extra or None


def _peer_query(peer: str | None) -> str:
    return f"?peer={quote(peer)}" if (peer := _remote(peer)) else ""


async def subscribe(
    service: str,
    topic: str,
    *,
    peer: str | None = None,
    as_: str | None = None,
    on_connect: Any = None,
) -> AsyncIterator[Any]:
    """Async generator over a service's emitter topic, via the gateway.

    Opens a WebSocket to ``/hub/emit/{service}/{topic}``; a ``peer`` naming
    another node subscribes on that node over the edge. The gateway fans each
    ``emit`` payload out as one JSON text frame. Yields the decoded payload per
    frame; exits when the gateway closes the socket.

    ``on_connect``, if given, is called with no arguments once the socket is
    open — so a supervisor can observe "connected" without this function
    growing a reconnect loop it must not have.
    """
    import websockets  # local import: WS isn't needed on the call() hot path

    ws_url = f"{_ws_base()}/hub/emit/{quote(service)}/{quote(topic)}{_peer_query(peer)}"
    async with websockets.connect(
        ws_url,
        additional_headers=_ws_headers(as_),
        max_size=None,
        open_timeout=10,
        **_PING_KWARGS,
    ) as ws:
        if on_connect is not None:
            on_connect()
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                # Emit fan-out is always JSON text; ignore binary.
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                yield raw


# ---------------------------------------------------------------------------
# Cross-node calls. A call, stream or lease names its ``peer``, and the local
# gateway carries it over the ssh edge (fleet.md R92, R93).
# ---------------------------------------------------------------------------


def _remote(peer: str | None) -> str | None:
    """``peer`` when it names another node, else ``None``."""
    peer = (peer or "").strip()
    return peer if peer and peer != "local" else None


def peer_env(var: str) -> str | None:
    """Read a peer-selector env var; empty/unset → ``None`` (local).

    The convention for singleton re-homing: a node borrowing a singleton exports
    e.g. ``AWM_SOCIAL_PEER=mira`` / ``AWM_TWOFA_PEER=mira``; the node that owns
    the singleton leaves it unset so calls stay local. Read fresh each time so a
    reconnect loop picks up a change without a restart.
    """
    v = (os.environ.get(var) or "").strip()
    return v or None


# ---------------------------------------------------------------------------
# Direct-session leases — hold a service slot for as long as a WS stays open.
# The open socket IS the lease (ZooKeeper-ephemeral / etcd-keepalive style): the
# holder keeps it open for the guarded work, then reports a verdict; a drop is
# observed by the owning service at once. One WebSocket to
# /hub/session/<svc>/<kind> carries the init as its first frame.
# ---------------------------------------------------------------------------


class Lease:
    """A held direct-session slot. The OPEN WebSocket is the lease: hold it for
    the duration of the guarded work, then :meth:`verdict` (clean release) or let
    it close/drop — the owning service treats an unreported drop as a failure.

    Use as an async context manager so any exit path still drops the socket::

        async with await acquire_lease("ssh", host, peer=peer) as lease:
            if not lease.granted:
                ...  # busy / locked / error — do not proceed
            else:
                ...  # do the guarded work while holding the slot
                await lease.verdict(ok=success)
    """

    def __init__(self, ws: Any, status: str, reason: str | None,
                 *, node: str | None = None) -> None:
        self._ws = ws
        # "granted" | "busy" | "locked" | "error" — the owning service's first frame.
        self.status = status
        self.reason = reason
        # Who is holding the lease. Echoed in the verdict so an arbiter that
        # reports a failure can name the node that actually made the attempt
        # rather than itself — see the ssh service's lock alert. Passed in by the
        # caller rather than looked up here: gatewayclient does not depend on
        # awm-config, and "who is asking" is the caller's fact anyway.
        self._node = node
        self._closed = False

    @property
    def granted(self) -> bool:
        return self.status == "granted"

    async def verdict(self, *, ok: bool, reason: str = "") -> None:
        """Report the outcome on the held socket, then close (a clean release)."""
        if self._closed:
            return
        frame: dict[str, Any] = {"verdict": "ok" if ok else "fail",
                                 "reason": reason}
        if self._node:
            frame["node"] = self._node
        try:
            await self._ws.send(json.dumps(frame))
        except Exception:  # noqa: BLE001 — socket already gone counts as a drop
            pass
        await self.aclose()

    async def aclose(self) -> None:
        """Drop the socket without a verdict (the owning service sees a drop)."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass

    async def __aenter__(self) -> "Lease":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


async def _read_grant(ws: Any, node: str | None = None) -> Lease:
    """Read the owning service's first frame — its grant/deny — into a Lease."""
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
        # The gateway refuses a session by closing with 1011 and the reason.
        rcvd = getattr(exc, "rcvd", None)
        reason = rcvd.reason if rcvd is not None and rcvd.reason else str(exc)
        raise GatewayCallError(502, f"lease grant not received: {reason}", service="lease", fn="grant") from exc
    try:
        frame = json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        frame = {}
    return Lease(ws, frame.get("lease") or "error", frame.get("reason"),
                 node=node)


async def acquire_lease(
    service: str,
    host: str,
    *,
    kind: str = "lease",
    peer: str | None = None,
    as_: str | None = None,
    node: str | None = None,
    timeout: float = 10.0,
) -> Lease:
    """Open a direct-session lease on ``service`` and read the grant frame.

    Opens one WebSocket to ``/hub/session/{service}/{kind}`` (on ``peer`` when it
    names another node), sends ``{"host": host, "node": node}`` as the first text
    frame, and reads the owning service's grant/deny. The caller holds the
    :class:`Lease` for the guarded work and reports a verdict. A refusal by the
    gateway or the service raises ``GatewayCallError``.
    """
    import websockets

    url = f"{_ws_base()}/hub/session/{quote(service)}/{quote(kind)}{_peer_query(peer)}"
    try:
        ws = await websockets.connect(url, additional_headers=_ws_headers(as_),
                                      max_size=None, open_timeout=timeout)
    except websockets.InvalidStatus as exc:
        status = exc.response.status_code
        body = (exc.response.body or b"").decode("utf-8", "replace")
        raise GatewayCallError(status, body, service=service, fn="session") from exc
    init = {"host": host, "node": node} if node else {"host": host}
    try:
        await ws.send(json.dumps(init))
    except Exception as exc:  # noqa: BLE001
        await ws.close()
        raise GatewayCallError(502, f"session init not sent: {exc}", service=service, fn="session") from exc
    return await _read_grant(ws, node)


# ---------------------------------------------------------------------------
# RefCache — validate-by-calling, positive-only, short TTL
# ---------------------------------------------------------------------------


def _freeze_args(args: dict | None) -> frozenset:
    """Build a hashable key from an args dict.

    Falls back to a JSON string for any non-hashable value so nested
    dict/list args still produce a stable, hashable key.
    """
    items: list[tuple[str, Any]] = []
    for k, v in (args or {}).items():
        try:
            hash(v)
            items.append((k, v))
        except TypeError:
            items.append((k, json.dumps(v, sort_keys=True)))
    return frozenset(items)


class RefCache:
    """Short-TTL, positive-only cache over :func:`call`.

    For the "refs are natural keys validated by calling the owning service"
    hot path: a service validates a cross-service reference (e.g. a
    ``project/scope``) by calling the owning service's read function, and
    caches the *positive* result for ``ttl`` seconds. Negative results
    (``None`` / falsy, meaning "not found") are NOT cached, so a ref that
    later becomes valid is picked up on the next call.

    Keyed by ``(service, fn, frozenset(args.items()))``. Time source is
    ``time.monotonic()``.

    Concurrency: this is a plain dict cache with no lock. It is intended for
    use from a single event loop (the common service case). Concurrent
    ``validate`` calls for the same key may each issue an RPC before one
    populates the cache — that is a harmless duplicate read, never a
    correctness problem.
    """

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        # key -> (expires_monotonic, result)
        self._store: dict[tuple[str | None, str, str, frozenset], tuple[float, Any]] = {}

    def _key(self, service: str, fn: str, args: dict | None,
             peer: str | None = None) -> tuple[str | None, str, str, frozenset]:
        # ``peer`` is part of the key so a local ref (``2fa``) and a peer ref
        # (``2fa@mira``) never collide.
        return (peer, service, fn, _freeze_args(args))

    async def validate(
        self,
        service: str,
        fn: str,
        args: dict | None = None,
        *,
        as_: str | None = None,
        peer: str | None = None,
    ) -> Any:
        """Return a cached positive result within TTL, else call and cache.

        A falsy/``None`` RPC result means "not found"; it is returned to the
        caller but NOT cached, so the next ``validate`` re-calls the owning
        service. Any truthy result is cached for ``ttl`` seconds. When ``peer``
        is given the validating call goes to that peer node's edge.
        """
        key = self._key(service, fn, args, peer)
        now = time.monotonic()
        hit = self._store.get(key)
        if hit is not None:
            expires, result = hit
            if expires > now:
                return result
            # Expired — drop and fall through to a fresh call.
            self._store.pop(key, None)

        if peer is not None:
            result = await call(service, fn, args, peer=peer, as_=as_)
        else:
            result = await call(service, fn, args, as_=as_)
        if result:  # positive-only: don't cache None / {} / falsy "not found"
            self._store[key] = (now + self.ttl, result)
        return result

    def invalidate(
        self,
        service: str | None = None,
        fn: str | None = None,
        args: dict | None = None,
        *,
        peer: str | None = None,
    ) -> None:
        """Drop cached entries.

        * ``invalidate()`` — clear everything.
        * ``invalidate(service)`` — clear every entry for that service.
        * ``invalidate(service, fn)`` — clear every entry for that
          ``(service, fn)``.
        * ``invalidate(service, fn, args)`` — clear the one exact entry
          (pass ``peer=`` to target a peer ref).
        """
        if service is None:
            self._store.clear()
            return
        if fn is not None and args is not None:
            self._store.pop(self._key(service, fn, args, peer), None)
            return
        for key in list(self._store):
            k_peer, k_service, k_fn, _ = key
            if k_service != service:
                continue
            if fn is not None and k_fn != fn:
                continue
            self._store.pop(key, None)
