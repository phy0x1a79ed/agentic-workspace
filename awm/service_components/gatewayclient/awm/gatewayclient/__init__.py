"""Service-originated calls to other services, via the gateway.

In the modular architecture the gateway is the sole interface. A *browser
or MCP client* already reaches a feature service through it:
``POST /svc/<name>/fn/<fn>`` is translated by the gateway's service-routing
layer (``awm.gateway.hub.proxy.proxy_service_http``) into a
``ControlChannel.call`` on that service's control WS, and the JSON reply is
returned as the HTTP body. The ``X-Awm-As: <principal>`` header carries the
caller's identity end-to-end (the route layer reads it and threads it through
as ``as_``).

What was missing is a way for a SERVICE PROCESS to originate the same kind of
call — which is exactly what the "no global identity; refs are natural keys
validated by calling the owning service via gateway RPC, cached" invariant
requires. A service that holds a reference to, say, ``project/scope`` does not
import the scopes service to validate it; it *calls* the scopes service via
the gateway and caches the positive answer. This module is that small client.

Public API
----------
``call(service, fn, args, *, as_=None, timeout=30.0)`` — async; one RPC.
``call_sync(service, fn, args, *, as_=None, timeout=30.0)`` — sync variant.
``subscribe(service, topic, *, as_=None)`` — async generator over a topic
    (provisional; may be unused in pass 1).
``GatewayCallError`` — raised on a non-2xx reply, carrying ``status`` + ``body``.
``RefCache`` — short-TTL, positive-only cache wrapping ``call`` for the
    validate-by-calling hot path.

Transport notes (mirrored from ``proxy_service_http``)
------------------------------------------------------
* URL shape is ``{AWM_HUB_URL}/svc/{service}/fn/{fn}``; ``AWM_HUB_URL`` is the
  gateway base URL the hub injects into every service process (e.g.
  ``http://127.0.0.1:7819/``).
* The request body IS the args object, JSON-encoded (``proxy_service_http``
  does ``args = json.loads(body)``; empty body → ``null`` args). We always
  send a JSON body, defaulting ``args`` to ``{}``.
* Identity rides ``X-Awm-As: <as_>`` when ``as_`` is given — the same header
  the gateway route reads to populate ``as_`` on the upstream ``call``.
* The gateway binds loopback-only with no auth layer on the ``/svc`` surface
  (federation is retired), so no bearer is required — the registration
  handshake carries no token at all.
* The reply is JSON. ``proxy_service_http`` returns the raw result, or ``{}``
  when the service returned ``None`` — so callers see ``{}`` for a null
  result, never Python ``None``, over this boundary.

A service calling ITSELF must use its own local DAO — do NOT round-trip
through the gateway to reach your own functions. This client is for
cross-service references only.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from awm.gatewayclient.adapter import ServiceAdapter, SessionContext

log = logging.getLogger("awm.gatewayclient")

__all__ = [
    "call",
    "call_sync",
    "call_peer",
    "call_peer_sync",
    "resolve_peer",
    "fetch_peer_cred",
    "subscribe",
    "subscribe_peer",
    "peer_env",
    "call_maybe_peer",
    "subscribe_maybe_peer",
    "GatewayCallError",
    "PeerError",
    "RefCache",
    "hub_base_url",
    "ServiceAdapter",
    "SessionContext",
]

# Default only used when AWM_HUB_URL is unset. Prefer the env var: the hub
# injects it into every service process and it carries the correct per-sandbox
# port (prod 7819, dev sandboxes 7821/7831/...).
_DEFAULT_HUB_URL = "http://127.0.0.1:7819/"


class GatewayCallError(Exception):
    """A ``/svc/<name>/fn/<fn>`` call returned a non-2xx response.

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
        where = f"{service}/{fn}" if service or fn else "<svc>/<fn>"
        super().__init__(f"gateway call {where} failed: HTTP {status}: {body}")


def hub_base_url() -> str:
    """The gateway base URL, normalized without a trailing slash.

    Read from ``AWM_HUB_URL`` (injected into every service process); falls
    back to the prod loopback default when unset.
    """
    return (os.environ.get("AWM_HUB_URL") or _DEFAULT_HUB_URL).rstrip("/")


def _svc_url(service: str, fn: str) -> str:
    return f"{hub_base_url()}/svc/{service}/fn/{fn}"


def _headers(as_: str | None) -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if as_ is not None:
        h["X-Awm-As"] = as_
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
        # The /svc surface always returns JSON on 2xx; treat a non-JSON 2xx
        # body as raw text rather than masking it.
        return resp.text


async def call(
    service: str,
    fn: str,
    args: dict | None = None,
    *,
    as_: str | None = None,
    timeout: float = 30.0,
) -> Any:
    """Call ``fn`` on ``service`` via the gateway and return the JSON result.

    POSTs ``json(args)`` to ``{AWM_HUB_URL}/svc/{service}/fn/{fn}`` with an
    ``X-Awm-As: <as_>`` header when ``as_`` is given. Raises
    ``GatewayCallError`` on any non-2xx response (status + body attached).

    A ``None``/null service result comes back as ``{}`` (the gateway
    substitutes ``{}`` for ``None``) — callers that need a true "not found"
    signal should have the owning service return a falsy payload they can
    test (e.g. ``null`` inside a field), not rely on HTTP status.
    """
    url = _svc_url(service, fn)
    async with httpx.AsyncClient(timeout=timeout) as cli:
        resp = await cli.post(url, content=json.dumps(args or {}),
                              headers=_headers(as_))
    return _parse_reply(resp, service, fn)


def call_sync(
    service: str,
    fn: str,
    args: dict | None = None,
    *,
    as_: str | None = None,
    timeout: float = 30.0,
) -> Any:
    """Synchronous variant of :func:`call`, for non-async service code.

    Same URL/header/body contract; uses a blocking ``httpx.Client``. Do not
    call this from inside the event loop of an async service — use
    :func:`call` there.
    """
    url = _svc_url(service, fn)
    with httpx.Client(timeout=timeout) as cli:
        resp = cli.post(url, content=json.dumps(args or {}),
                        headers=_headers(as_))
    return _parse_reply(resp, service, fn)


async def subscribe(
    service: str,
    topic: str,
    *,
    as_: str | None = None,
) -> AsyncIterator[Any]:
    """Async generator over a service's emitter topic, via the gateway.

    **Provisional / may be unused in pass 1.** Opens a WS to
    ``{AWM_HUB_URL}/svc/{service}/emit/{topic}`` — the same browser-side
    subscription surface (``proxy_service_emit_ws``), which registers a
    subscriber on the owning service's control channel and fans each
    ``emit`` payload out as one JSON text frame. Yields the decoded payload
    per frame; exits when the gateway closes the socket.

    The ``X-Awm-As`` identity header is sent as a connect header when
    ``as_`` is given (the gateway's emit route reads it the same way the
    HTTP route reads it).
    """
    import websockets  # local import: WS isn't needed on the call() hot path

    base = hub_base_url()
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_base}/svc/{service}/emit/{topic}"
    extra: list[tuple[str, str]] = []
    if as_ is not None:
        extra.append(("X-Awm-As", as_))

    async with websockets.connect(
        ws_url,
        additional_headers=extra or None,
        max_size=None,
        open_timeout=10,
    ) as ws:
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                # Non-direct emit fan-out is always JSON text; ignore binary.
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                yield raw


# ---------------------------------------------------------------------------
# Cross-peer calls — reach ANOTHER node's service edge directly (never relayed
# through a gateway). The local gateway is asked only to RESOLVE the peer's
# address; the call then goes straight to the peer's httpsfront edge, over
# CA-verified TLS, authenticated with a bearer fetched over SSH.
# ---------------------------------------------------------------------------


class PeerError(Exception):
    """A cross-peer call could not be set up (unknown peer, no CA, ssh-fetch
    failed). Distinct from :class:`GatewayCallError`, which is an HTTP non-2xx
    from a reachable edge."""


# Short-TTL caches so the resolve + ssh-fetch don't run on every call. Keyed by
# peer name / ssh alias; monotonic expiry. Positive-only.
_PEER_ADDR_TTL = 60.0
_PEER_CRED_TTL = 300.0
_peer_addr_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_peer_cred_cache: dict[str, tuple[float, str]] = {}


def _peer_ca() -> str:
    """Path to the CA that signs peer edge certs (the shared remote-audio root).

    Never fall back to ``verify=False`` — a bearer sent over an unverified TLS
    connection could be captured by a MITM. If the CA is absent we raise, so the
    failure is loud rather than silently insecure.
    """
    env = os.environ.get("AWM_PEER_CA")
    if env:
        return env
    ca_dir = os.environ.get("REMOTE_AUDIO_CA_DIR")
    if ca_dir:
        return str(Path(ca_dir) / "ca.pem")
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return str(Path(base) / "remote-audio" / "ca" / "ca.pem")


def resolve_peer(name: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Resolve a peer name to its ``{edge_url, ssh_alias, ...}`` entry by asking
    THIS node's gateway (``GET /peers/{name}``). Cached briefly. Raises
    :class:`PeerError` if the peer is unknown."""
    now = time.monotonic()
    hit = _peer_addr_cache.get(name)
    if hit and hit[0] > now:
        return hit[1]
    url = f"{hub_base_url()}/peers/{name}"
    try:
        with httpx.Client(timeout=timeout) as cli:
            resp = cli.get(url)
    except httpx.HTTPError as exc:
        raise PeerError(f"could not reach local gateway to resolve peer {name!r}: {exc}") from exc
    if resp.status_code == 404:
        raise PeerError(f"unknown peer: {name}")
    if resp.status_code < 200 or resp.status_code >= 300:
        raise PeerError(f"resolve peer {name!r} failed: HTTP {resp.status_code}: {resp.text}")
    entry = (resp.json() or {}).get("peer") or {}
    if not entry.get("edge_url"):
        raise PeerError(f"peer {name!r} has no edge_url")
    _peer_addr_cache[name] = (now + _PEER_ADDR_TTL, entry)
    return entry


def fetch_peer_cred(ssh_alias: str, *, force: bool = False,
                    timeout: float = 15.0) -> str:
    """Fetch a peer's current credential via SSH: ``ssh <alias> 'cat "$AWM_PEER_CRED"'``.

    SSH host-key + authorized_keys ARE the mutual auth — there is no token
    exchange to attack. Single-quoted so ``$AWM_PEER_CRED`` expands on the peer.
    Cached; pass ``force=True`` to bypass the cache (used on a 401 to pick up a
    rotated credential). Raises :class:`PeerError` on any ssh failure.
    """
    now = time.monotonic()
    if not force:
        hit = _peer_cred_cache.get(ssh_alias)
        if hit and hit[0] > now:
            return hit[1]
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", ssh_alias, 'cat "$AWM_PEER_CRED"'],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PeerError(f"ssh fetch of peer cred from {ssh_alias!r} failed: {exc}") from exc
    if out.returncode != 0:
        raise PeerError(
            f"ssh fetch of peer cred from {ssh_alias!r} exited {out.returncode}: "
            f"{out.stderr.strip()[:200]}")
    cred = out.stdout.strip()
    if not cred:
        raise PeerError(f"peer cred from {ssh_alias!r} is empty ($AWM_PEER_CRED unset?)")
    _peer_cred_cache[ssh_alias] = (now + _PEER_CRED_TTL, cred)
    return cred


def _peer_headers(bearer: str, as_: str | None) -> dict[str, str]:
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"}
    if as_ is not None:
        h["X-Awm-As"] = as_
    return h


async def call_peer(
    peer: str,
    service: str,
    fn: str,
    args: dict | None = None,
    *,
    as_: str | None = None,
    timeout: float = 30.0,
) -> Any:
    """Call ``fn`` on ``service`` running on peer node ``peer`` and return the
    JSON result.

    Resolves the peer's edge via the local gateway, fetches the peer bearer over
    SSH, then POSTs ``{edge}/svc/{service}/fn/{fn}`` **directly to the peer edge**
    over CA-verified TLS — no bytes traverse the local gateway. On a 401 the
    credential is re-fetched once (it may have rotated) and the call retried.
    """
    entry = resolve_peer(peer)
    edge = entry["edge_url"].rstrip("/")
    ssh_alias = entry.get("ssh_alias") or peer
    url = f"{edge}/svc/{service}/fn/{fn}"
    ca = _peer_ca()
    body = json.dumps(args or {})
    for attempt in (0, 1):
        bearer = fetch_peer_cred(ssh_alias, force=(attempt == 1))
        async with httpx.AsyncClient(timeout=timeout, verify=ca) as cli:
            resp = await cli.post(url, content=body,
                                  headers=_peer_headers(bearer, as_))
        if resp.status_code == 401 and attempt == 0:
            continue  # credential likely rotated — force a re-fetch and retry
        return _parse_reply(resp, f"{service}@{peer}", fn)
    return _parse_reply(resp, f"{service}@{peer}", fn)


def call_peer_sync(
    peer: str,
    service: str,
    fn: str,
    args: dict | None = None,
    *,
    as_: str | None = None,
    timeout: float = 30.0,
) -> Any:
    """Synchronous variant of :func:`call_peer`."""
    entry = resolve_peer(peer)
    edge = entry["edge_url"].rstrip("/")
    ssh_alias = entry.get("ssh_alias") or peer
    url = f"{edge}/svc/{service}/fn/{fn}"
    ca = _peer_ca()
    body = json.dumps(args or {})
    for attempt in (0, 1):
        bearer = fetch_peer_cred(ssh_alias, force=(attempt == 1))
        with httpx.Client(timeout=timeout, verify=ca) as cli:
            resp = cli.post(url, content=body, headers=_peer_headers(bearer, as_))
        if resp.status_code == 401 and attempt == 0:
            continue
        return _parse_reply(resp, f"{service}@{peer}", fn)
    return _parse_reply(resp, f"{service}@{peer}", fn)


async def subscribe_peer(
    peer: str,
    service: str,
    topic: str,
    *,
    as_: str | None = None,
) -> AsyncIterator[Any]:
    """Async generator over a peer node's emitter topic, via its edge directly.

    The cross-peer analogue of :func:`subscribe`. Resolves the peer's edge via
    the local gateway, fetches the peer bearer over SSH, then opens a WebSocket
    to ``{edge}/svc/{service}/emit/{topic}`` **directly on the peer edge** (never
    relayed through a gateway), over CA-verified TLS with the bearer. Yields the
    decoded payload per frame, byte-for-byte as :func:`subscribe` does for a
    local topic — a consumer cannot tell a peer stream from a local one.

    The peer's ``httpsfront`` edge authenticates the bearer during the WS
    handshake, BEFORE accepting the socket, so a stale/rotated credential is
    rejected as a handshake failure (``InvalidStatus`` 401/403), not a 401 on an
    already-open socket. On that we re-fetch the credential once (``force=True``)
    and reconnect, mirroring :func:`call_peer`'s 401 retry. Auth is checked only
    at connect, so a mid-stream rotation (~12 h cadence) bites only on the next
    reconnect; callers needing indefinite liveness wrap this in their OWN
    reconnect loop (as the ``/approve`` consumers already do) — this keeps the
    single-connection contract, so do NOT add an inner reconnect loop here beyond
    the one credential-refresh retry.
    """
    import ssl
    import websockets  # local import: WS isn't needed on the call() hot path

    entry = resolve_peer(peer)
    edge = entry["edge_url"].rstrip("/")
    ssh_alias = entry.get("ssh_alias") or peer
    ws_base = edge.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_base}/svc/{service}/emit/{topic}"
    ssl_ctx = ssl.create_default_context(cafile=_peer_ca())

    conn = None
    for attempt in (0, 1):
        bearer = fetch_peer_cred(ssh_alias, force=(attempt == 1))
        headers = [("Authorization", f"Bearer {bearer}")]
        if as_ is not None:
            headers.append(("X-Awm-As", as_))
        try:
            conn = await websockets.connect(
                ws_url,
                additional_headers=headers,
                ssl=ssl_ctx,
                max_size=None,
                open_timeout=10,
            )
        except websockets.InvalidStatus as exc:
            # Handshake rejected by the peer edge. 401/403 here means the bearer
            # was stale (rotated) — force a re-fetch and reconnect once, the WS
            # analogue of call_peer's 401 retry. Any other status, or a second
            # rejection, propagates loudly.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt == 0 and status in (401, 403):
                continue
            raise
        break

    async with conn:
        async for raw in conn:
            if isinstance(raw, (bytes, bytearray)):
                # Non-direct emit fan-out is always JSON text; ignore binary.
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                yield raw


# ---------------------------------------------------------------------------
# Local-or-peer selectors — a SINGLE branch point so a service that consumes a
# singleton (e.g. ssh→2fa, ssh/2fa/auth→social) routes to the local service or a
# peer node's edge based on ONE piece of config, and can never half-route (a
# wrong flag can't send the burst locally but the reply to a peer, or vice
# versa). The singleton's home is node-level, not per-call: on the node that
# OWNS the singleton the selector is empty and every call stays local; on a node
# that borrows it, the selector names the peer and every call goes there.
# ---------------------------------------------------------------------------


def peer_env(var: str) -> str | None:
    """Read a peer-selector env var; empty/unset → ``None`` (local).

    The convention for singleton re-homing: a node borrowing a singleton exports
    e.g. ``AWM_SOCIAL_PEER=mira`` / ``AWM_TWOFA_PEER=mira``; the node that owns
    the singleton leaves it unset so calls stay local. Read fresh each time so a
    reconnect loop picks up a change without a restart.
    """
    v = (os.environ.get(var) or "").strip()
    return v or None


async def call_maybe_peer(
    peer: str | None,
    service: str,
    fn: str,
    args: dict | None = None,
    *,
    as_: str | None = None,
    timeout: float = 30.0,
) -> Any:
    """:func:`call` when ``peer`` is falsy, else :func:`call_peer` to that peer.

    The one branch that decides local-vs-peer for a singleton consumer; callers
    pass the selector (typically :func:`peer_env`) so the decision lives here,
    not scattered across call sites.
    """
    if peer:
        return await call_peer(peer, service, fn, args, as_=as_, timeout=timeout)
    return await call(service, fn, args, as_=as_, timeout=timeout)


async def subscribe_maybe_peer(
    peer: str | None,
    service: str,
    topic: str,
    *,
    as_: str | None = None,
) -> AsyncIterator[Any]:
    """:func:`subscribe` when ``peer`` is falsy, else :func:`subscribe_peer`.

    The streaming twin of :func:`call_maybe_peer`. Yields identically either way
    (both decode paths are byte-for-byte the same), so a consumer's event
    handling is oblivious to whether the stream is local or from a peer.
    """
    if peer:
        gen = subscribe_peer(peer, service, topic, as_=as_)
    else:
        gen = subscribe(service, topic, as_=as_)
    async for item in gen:
        yield item


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
            result = await call_peer(peer, service, fn, args, as_=as_)
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
