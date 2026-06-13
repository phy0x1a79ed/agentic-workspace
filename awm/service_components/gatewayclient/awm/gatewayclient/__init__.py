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
import os
import time
from typing import Any, AsyncIterator

import httpx

from awm.gatewayclient.adapter import ServiceAdapter, SessionContext

__all__ = [
    "call",
    "call_sync",
    "subscribe",
    "GatewayCallError",
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
        self._store: dict[tuple[str, str, frozenset], tuple[float, Any]] = {}

    def _key(self, service: str, fn: str,
             args: dict | None) -> tuple[str, str, frozenset]:
        return (service, fn, _freeze_args(args))

    async def validate(
        self,
        service: str,
        fn: str,
        args: dict | None = None,
        *,
        as_: str | None = None,
    ) -> Any:
        """Return a cached positive result within TTL, else call and cache.

        A falsy/``None`` RPC result means "not found"; it is returned to the
        caller but NOT cached, so the next ``validate`` re-calls the owning
        service. Any truthy result is cached for ``ttl`` seconds.
        """
        key = self._key(service, fn, args)
        now = time.monotonic()
        hit = self._store.get(key)
        if hit is not None:
            expires, result = hit
            if expires > now:
                return result
            # Expired — drop and fall through to a fresh call.
            self._store.pop(key, None)

        result = await call(service, fn, args, as_=as_)
        if result:  # positive-only: don't cache None / {} / falsy "not found"
            self._store[key] = (now + self.ttl, result)
        return result

    def invalidate(
        self,
        service: str | None = None,
        fn: str | None = None,
        args: dict | None = None,
    ) -> None:
        """Drop cached entries.

        * ``invalidate()`` — clear everything.
        * ``invalidate(service)`` — clear every entry for that service.
        * ``invalidate(service, fn)`` — clear every entry for that
          ``(service, fn)``.
        * ``invalidate(service, fn, args)`` — clear the one exact entry.
        """
        if service is None:
            self._store.clear()
            return
        if fn is not None and args is not None:
            self._store.pop(self._key(service, fn, args), None)
            return
        for key in list(self._store):
            k_service, k_fn, _ = key
            if k_service != service:
                continue
            if fn is not None and k_fn != fn:
                continue
            self._store.pop(key, None)
