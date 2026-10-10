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

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, AsyncIterator

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
    "call_peer",
    "call_peer_sync",
    "invoke_peer",
    "invoke_peer_sync",
    "peer_token",
    "peer_token_sync",
    "peer_send",
    "peer_send_sync",
    "peer_send_cred",
    "peer_send_cred_sync",
    "resolve_peer",
    "resolve_peer_async",
    "fetch_peer_cred",
    "fetch_peer_cred_async",
    "fetch_peer_file",
    "fetch_peer_file_sync",
    "subscribe",
    "subscribe_peer",
    "peer_env",
    "call_maybe_peer",
    "call_sync_maybe_peer",
    "subscribe_maybe_peer",
    "SupervisedSubscription",
    "spawn_supervised",
    "acquire_lease",
    "acquire_lease_peer",
    "acquire_lease_maybe_peer",
    "Lease",
    "GatewayCallError",
    "PeerError",
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
    on_connect: Any = None,
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

    ``on_connect``, if given, is called with no arguments once the socket is
    open — so a supervisor can observe "connected" without this function
    growing a reconnect loop it must not have.
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
        **_PING_KWARGS,
    ) as ws:
        if on_connect is not None:
            on_connect()
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
# CA-verified TLS, authenticated with a node-signed token (or, for a domestic
# peer that has not upgraded, a bearer fetched over SSH).
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

# Cred-fetch resilience. The fetch is a plain `ssh <alias> cat` — idempotent and
# side-effect-free on the peer, and it never touches a lockout-sensitive host or
# spends an MFA attempt. Retrying it is therefore FREE of lockout risk, which is
# what makes a bounded retry safe here but not on the ssh attempt it guards.
#
# ConnectTimeout matters more than it looks: peer aliases inherit
# `connecttimeout none` from ssh's defaults, so a blackholed route (WSL2 NAT
# churn when a docker network comes up, say) hangs for the FULL subprocess
# timeout rather than failing fast. Capping the TCP connect turns a 15 s stall
# into a ~5 s one and lets the retries actually run inside a sane budget.
_PEER_CRED_ATTEMPTS = 3
_PEER_CRED_BACKOFF = 0.5      # seconds before retry 2; doubled for each retry after
_PEER_CRED_CONNECT_TIMEOUT = 5  # ssh -o ConnectTimeout=<n>


class _TransientCredError(PeerError):
    """A cred fetch that failed in a way a retry could plausibly fix (ssh could
    not reach the peer). Never raised for a definitive answer — a peer that
    replies "no such variable" is misconfigured, not flaky, so retrying it just
    burns the caller's fail-closed budget."""


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


async def resolve_peer_async(name: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """:func:`resolve_peer` for async callers: a cache hit costs nothing, a miss
    runs the blocking lookup in a worker thread so a slow local gateway never
    stalls the caller's event loop (and a ``wait_for`` around the call can fire).
    Resolves the module global at call time so tests that patch ``resolve_peer``
    still take effect."""
    hit = _peer_addr_cache.get(name)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return await asyncio.to_thread(resolve_peer, name, timeout=timeout)


def _fetch_peer_cred_once(ssh_alias: str, timeout: float) -> str:
    """One ``ssh <alias> 'cat "$AWM_PEER_CRED"'``. Raises
    :class:`_TransientCredError` when ssh could not reach the peer (exit 255 is
    ssh's own "connection failed", as distinct from the remote command's status),
    plain :class:`PeerError` when the peer answered definitively."""
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes",
             "-o", f"ConnectTimeout={_PEER_CRED_CONNECT_TIMEOUT}",
             ssh_alias, 'cat "$AWM_PEER_CRED"'],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise _TransientCredError(
            f"ssh fetch of peer cred from {ssh_alias!r} failed: {exc}") from exc
    except OSError as exc:
        # Could not even spawn ssh — a local defect, not peer flakiness.
        raise PeerError(
            f"ssh fetch of peer cred from {ssh_alias!r} failed: {exc}") from exc
    if out.returncode == 255:
        raise _TransientCredError(
            f"ssh fetch of peer cred from {ssh_alias!r} exited 255: "
            f"{out.stderr.strip()[:200]}")
    if out.returncode != 0:
        raise PeerError(
            f"ssh fetch of peer cred from {ssh_alias!r} exited {out.returncode}: "
            f"{out.stderr.strip()[:200]}")
    cred = out.stdout.strip()
    if not cred:
        raise PeerError(f"peer cred from {ssh_alias!r} is empty ($AWM_PEER_CRED unset?)")
    return cred


def fetch_peer_cred(ssh_alias: str, *, force: bool = False,
                    timeout: float = 15.0) -> str:
    """Fetch a peer's current credential via SSH: ``ssh <alias> 'cat "$AWM_PEER_CRED"'``.

    SSH host-key + authorized_keys ARE the mutual auth — there is no token
    exchange to attack. Single-quoted so ``$AWM_PEER_CRED`` expands on the peer.
    Cached; pass ``force=True`` to bypass the cache (used on a 401 to pick up a
    rotated credential). Raises :class:`PeerError` on any ssh failure.

    Retries a transient ssh failure up to ``_PEER_CRED_ATTEMPTS`` times with
    exponential backoff, so a momentary route blip does not fail a caller that
    fails CLOSED on this (the ssh service's slot arbiter refuses a connect
    outright when it cannot reach the arbiter peer). Retrying is safe precisely
    because this fetch spends nothing — see the constants above. A definitive
    error (bad exit, empty cred) raises at once without retrying.

    ``timeout`` is the TOTAL budget across every attempt, not per-attempt: the
    retries have to fit inside the caller's original deadline, or adding them
    would multiply the worst case (3 x 15 s) and leave a fail-closed caller
    hanging far longer than before. Attempts stop once the budget is spent.

    BLOCKING — this shells out to ssh. Async callers must use
    :func:`fetch_peer_cred_async`, which runs it off the event loop.
    """
    now = time.monotonic()
    if not force:
        hit = _peer_cred_cache.get(ssh_alias)
        if hit and hit[0] > now:
            return hit[1]
    deadline = now + timeout
    last: Exception | None = None
    for attempt in range(_PEER_CRED_ATTEMPTS):
        if attempt:
            backoff = _PEER_CRED_BACKOFF * (2 ** (attempt - 1))
            if time.monotonic() + backoff >= deadline:
                break
            time.sleep(backoff)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            cred = _fetch_peer_cred_once(ssh_alias, remaining)
        except _TransientCredError as exc:
            last = exc
            log.warning("peer cred fetch from %r failed (attempt %d/%d): %s",
                        ssh_alias, attempt + 1, _PEER_CRED_ATTEMPTS, exc)
            continue
        # Cache against the clock AFTER the ssh round-trip: `now` predates the
        # retries, and honouring it would shorten the TTL by the time we spent
        # failing (a slow fetch would cache a cred that expires early).
        _peer_cred_cache[ssh_alias] = (time.monotonic() + _PEER_CRED_TTL, cred)
        return cred
    raise PeerError(
        f"ssh fetch of peer cred from {ssh_alias!r} failed within {timeout:.0f}s: "
        f"{last}") from last


async def fetch_peer_cred_async(ssh_alias: str, *, force: bool = False,
                                timeout: float = 15.0) -> str:
    """:func:`fetch_peer_cred` off the event loop.

    The sync fetch shells out to ssh and can block for seconds (a blackholed
    route burns ConnectTimeout, then the retries back off). Calling it directly
    from a coroutine stalls the WHOLE service — for the ssh service that means
    reconcile, status, and every other host's connect freeze behind one slow
    peer. Resolve the module global at call time so tests that monkeypatch
    ``fetch_peer_cred`` still take effect.
    """
    return await asyncio.to_thread(fetch_peer_cred, ssh_alias,
                                   force=force, timeout=timeout)


def _peer_headers(bearer: str, as_: str | None) -> dict[str, str]:
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"}
    if as_ is not None:
        h["X-Awm-As"] = as_
    return h


# ---- Node-signed tokens -----------------------------------------------------
# A peer edge accepts either a short-lived token signed by THIS node's key (the
# local ``auth`` service holds the key; ``sign_peer_token`` is operator-only, so
# it is called with no identity) or, for a domestic peer on a node that has not
# upgraded yet, the legacy shared bearer fetched over ssh. A foreign peer never
# sees the legacy bearer: if the token path fails, the call fails.

_PEER_TOKEN_MARGIN = 30.0     # stop reusing a token this long before it expires
_PEER_TOKEN_DEFAULT_TTL = 300.0
#: The edge's answer to a credential it does not accept. Only this status sends a
#: domestic peer to the legacy bearer: a 403 is an authorisation verdict on a
#: caller the edge did identify, which a different credential cannot cure.
_TOKEN_REFUSED = (401,)
#: How long a domestic peer that refused our token is called with the legacy
#: bearer straight away, so an un-upgraded node costs one failed attempt per
#: window rather than a signing RPC and two requests per call.
_PEER_TOKEN_REFUSAL_TTL = 300.0
_peer_token_cache: dict[str, tuple[float, str]] = {}
_peer_token_refused: dict[str, float] = {}


def _peer_aud(peer: str) -> str:
    """The node label a token for ``peer`` must name as its audience."""
    try:
        from awm.config import peertoken
    except ImportError as exc:
        raise PeerError(f"cannot sign a node token for {peer!r}: {exc}") from exc
    aud = peertoken.node_label(peer)
    if not aud:
        raise PeerError("cannot sign a node token for an empty peer name")
    return aud


def _is_foreign(entry: dict[str, Any] | None) -> bool:
    """Whether a resolved peer entry is not domestic. A ``relation`` key that is
    absent means the answering gateway predates relations, so every peer was
    domestic; one that is present but empty or unknown fails closed as foreign."""
    entry = entry or {}
    return "relation" in entry and entry["relation"] != "domestic"


def _skip_token(peer: str, foreign: bool) -> bool:
    """Whether to go straight to the legacy bearer: a domestic peer that refused
    our token within the last ``_PEER_TOKEN_REFUSAL_TTL`` seconds."""
    return (not foreign
            and _peer_token_refused.get(_peer_aud(peer), 0.0) > time.monotonic())


def _note_token_refused(peer: str, foreign: bool, status: int) -> None:
    """Drop the cached token, log the audience we signed for (a book name that
    differs from the peer's node name shows up here), and remember a domestic
    refusal."""
    aud = _peer_aud(peer)
    _peer_token_cache.pop(aud, None)
    log.warning("peer %r refused the signed token (HTTP %s, aud=%r)%s",
                peer, status, aud,
                "" if foreign else "; using the legacy bearer for a while")
    if not foreign:
        _peer_token_refused[aud] = time.monotonic() + _PEER_TOKEN_REFUSAL_TTL


def _cached_peer_token(aud: str) -> str | None:
    hit = _peer_token_cache.get(aud)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    return None


def _remember_peer_token(aud: str, reply: Any) -> str:
    token = reply.get("token") if isinstance(reply, dict) else None
    if not isinstance(token, str) or not token:
        raise PeerError(f"auth.sign_peer_token returned no token for {aud!r}")
    try:
        ttl = float(reply.get("expires_in") or _PEER_TOKEN_DEFAULT_TTL)
    except (TypeError, ValueError):
        ttl = _PEER_TOKEN_DEFAULT_TTL
    _peer_token_cache[aud] = (
        time.monotonic() + max(ttl - _PEER_TOKEN_MARGIN, 0.0), token)
    return token


def peer_token_sync(peer: str, *, force: bool = False) -> str:
    """A signed token for ``peer``, from the cache until shortly before expiry.

    Raises :class:`PeerError` when the local ``auth`` service cannot sign one."""
    aud = _peer_aud(peer)
    if not force:
        hit = _cached_peer_token(aud)
        if hit:
            return hit
    try:
        reply = call_sync("auth", "sign_peer_token", {"aud": aud}, as_=None,
                          timeout=10.0)
    except (GatewayCallError, httpx.HTTPError) as exc:
        raise PeerError(f"could not sign a node token for {peer!r}: {exc}") from exc
    return _remember_peer_token(aud, reply)


async def peer_token(peer: str, *, force: bool = False) -> str:
    """Async variant of :func:`peer_token_sync`."""
    aud = _peer_aud(peer)
    if not force:
        hit = _cached_peer_token(aud)
        if hit:
            return hit
    try:
        reply = await call("auth", "sign_peer_token", {"aud": aud}, as_=None,
                           timeout=10.0)
    except (GatewayCallError, httpx.HTTPError) as exc:
        raise PeerError(f"could not sign a node token for {peer!r}: {exc}") from exc
    return _remember_peer_token(aud, reply)


async def peer_send_cred(peer: str, entry: dict[str, Any] | None,
                         send: Any) -> tuple[httpx.Response, str]:
    """:func:`peer_send` that also returns the credential the answer came on, for
    a caller that must open a second connection (a WS) with the same credential."""
    entry = entry or {}
    foreign = _is_foreign(entry)
    token = None
    try:
        if not _skip_token(peer, foreign):
            token = await peer_token(peer)
    except PeerError as exc:
        if foreign:
            raise
        log.info("no signed token for domestic peer %r (%s); using the legacy bearer",
                 peer, exc)
    if token is not None:
        resp = await send(token)
        if resp.status_code not in _TOKEN_REFUSED:
            return resp, token
        _note_token_refused(peer, foreign, resp.status_code)
        if foreign:
            return resp, token
    ssh_alias = entry.get("ssh_alias") or peer
    for attempt in (0, 1):
        bearer = await fetch_peer_cred_async(ssh_alias, force=(attempt == 1))
        resp = await send(bearer)
        if resp.status_code == 401 and attempt == 0:
            continue  # credential likely rotated — force a re-fetch and retry
        return resp, bearer
    return resp, bearer


def peer_send_cred_sync(peer: str, entry: dict[str, Any] | None,
                        send: Any) -> tuple[httpx.Response, str]:
    """Synchronous variant of :func:`peer_send_cred`."""
    entry = entry or {}
    foreign = _is_foreign(entry)
    token = None
    try:
        if not _skip_token(peer, foreign):
            token = peer_token_sync(peer)
    except PeerError as exc:
        if foreign:
            raise
        log.info("no signed token for domestic peer %r (%s); using the legacy bearer",
                 peer, exc)
    if token is not None:
        resp = send(token)
        if resp.status_code not in _TOKEN_REFUSED:
            return resp, token
        _note_token_refused(peer, foreign, resp.status_code)
        if foreign:
            return resp, token
    ssh_alias = entry.get("ssh_alias") or peer
    for attempt in (0, 1):
        bearer = fetch_peer_cred(ssh_alias, force=(attempt == 1))
        resp = send(bearer)
        if resp.status_code == 401 and attempt == 0:
            continue
        return resp, bearer
    return resp, bearer


async def peer_send(peer: str, entry: dict[str, Any] | None, send: Any) -> httpx.Response:
    """Run ``send(bearer) -> Response`` against ``peer`` with the right credential.

    The signed token goes first. When a domestic peer's edge refuses it with a
    401 (an un-upgraded node), or this node cannot sign one, the request is
    repeated once with the legacy ssh-fetched bearer, itself re-fetched once on a
    401; the peer is then called with the legacy bearer directly for a few
    minutes. A foreign peer is never offered the legacy bearer: its refusal is
    returned as is, and a signing failure raises :class:`PeerError`.
    """
    return (await peer_send_cred(peer, entry, send))[0]


def peer_send_sync(peer: str, entry: dict[str, Any] | None, send: Any) -> httpx.Response:
    """Synchronous variant of :func:`peer_send`; ``send`` is a plain function."""
    return peer_send_cred_sync(peer, entry, send)[0]


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

    Resolves the peer's edge via the local gateway, then POSTs
    ``{edge}/svc/{service}/fn/{fn}`` **directly to the peer edge** over
    CA-verified TLS — no bytes traverse the local gateway. Authenticates with a
    node-signed token; a domestic peer that refuses it gets the legacy ssh-fetched
    bearer instead (see :func:`peer_send`).
    """
    entry = await resolve_peer_async(peer)
    edge = entry["edge_url"].rstrip("/")
    url = f"{edge}/svc/{service}/fn/{fn}"
    ca = _peer_ca()
    body = json.dumps(args or {})

    async def send(bearer: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout, verify=ca) as cli:
            return await cli.post(url, content=body,
                                  headers=_peer_headers(bearer, as_))

    resp = await peer_send(peer, entry, send)
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
    url = f"{edge}/svc/{service}/fn/{fn}"
    ca = _peer_ca()
    body = json.dumps(args or {})

    def send(bearer: str) -> httpx.Response:
        with httpx.Client(timeout=timeout, verify=ca) as cli:
            return cli.post(url, content=body, headers=_peer_headers(bearer, as_))

    resp = peer_send_sync(peer, entry, send)
    return _parse_reply(resp, f"{service}@{peer}", fn)


def _parse_invoke_reply(resp: httpx.Response, peer: str, name: str) -> Any:
    """The ``result`` of a peer ``/invoke`` reply, JSON-decoded when it is a JSON
    object or array and the raw string otherwise (``"42"`` stays a string). Non-2xx raises :class:`GatewayCallError`."""
    body = _parse_reply(resp, f"{peer}", name)
    result = body.get("result") if isinstance(body, dict) else body
    if isinstance(result, str) and result.lstrip()[:1] in ("{", "["):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result


async def invoke_peer(
    peer: str,
    name: str,
    args: dict | None = None,
    *,
    timeout: float = 30.0,
) -> Any:
    """Call the tool ``name`` on ``peer`` through its edge ``/invoke``.

    ``name`` is the flat tool name (``kb_search``) or a domain name with
    ``args={"verb": ..., "args": {...}}``. This is the door a foreign peer
    exposes, so it carries no ``X-Awm-As``: the peer's edge stamps who we are
    from the signed token. A foreign peer is only ever offered the token; a
    domestic one may fall back to the legacy bearer (see :func:`peer_send`).
    Raises :class:`PeerError` for setup failures and :class:`GatewayCallError`
    for a non-2xx reply, which for a refused verb is the peer's 404.
    """
    entry = await resolve_peer_async(peer)
    edge = entry["edge_url"].rstrip("/")
    ca = _peer_ca()
    body = json.dumps({"name": name, "args": args or {}})

    async def send(bearer: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout, verify=ca) as cli:
            return await cli.post(f"{edge}/invoke", content=body,
                                  headers=_peer_headers(bearer, None))

    resp = await peer_send(peer, entry, send)
    return _parse_invoke_reply(resp, peer, name)


def invoke_peer_sync(
    peer: str,
    name: str,
    args: dict | None = None,
    *,
    timeout: float = 30.0,
) -> Any:
    """Synchronous variant of :func:`invoke_peer`."""
    entry = resolve_peer(peer)
    edge = entry["edge_url"].rstrip("/")
    ca = _peer_ca()
    body = json.dumps({"name": name, "args": args or {}})

    def send(bearer: str) -> httpx.Response:
        with httpx.Client(timeout=timeout, verify=ca) as cli:
            return cli.post(f"{edge}/invoke", content=body,
                            headers=_peer_headers(bearer, None))

    resp = peer_send_sync(peer, entry, send)
    return _parse_invoke_reply(resp, peer, name)


async def subscribe_peer(
    peer: str,
    service: str,
    topic: str,
    *,
    as_: str | None = None,
    on_connect: Any = None,
) -> AsyncIterator[Any]:
    """Async generator over a peer node's emitter topic, via its edge directly.

    The cross-peer analogue of :func:`subscribe`. Resolves the peer's edge via
    the local gateway, then opens a WebSocket to
    ``{edge}/svc/{service}/emit/{topic}`` **directly on the peer edge** (never
    relayed through a gateway), over CA-verified TLS with a node-signed token (a
    domestic peer that refuses it gets the ssh-fetched legacy bearer). Yields the
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

    entry = await resolve_peer_async(peer)
    edge = entry["edge_url"].rstrip("/")
    ssh_alias = entry.get("ssh_alias") or peer
    foreign = _is_foreign(entry)
    ws_base = edge.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_base}/svc/{service}/emit/{topic}"
    ssl_ctx = ssl.create_default_context(cafile=_peer_ca())

    async def connect(bearer: str):
        headers = [("Authorization", f"Bearer {bearer}")]
        if as_ is not None:
            headers.append(("X-Awm-As", as_))
        return await websockets.connect(
            ws_url,
            additional_headers=headers,
            ssl=ssl_ctx,
            max_size=None,
            open_timeout=10,
            **_PING_KWARGS,
        )

    def refused(exc: Exception) -> bool:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        return status in _TOKEN_REFUSED

    # The signed token goes first. A domestic peer that refuses it, or a node that
    # cannot sign one, gets the legacy bearer; a foreign peer never does. (A
    # foreign edge serves only /tools and /invoke, so its handshake fails cleanly.)
    conn = None
    token = None
    try:
        if not _skip_token(peer, foreign):
            token = await peer_token(peer)
    except PeerError:
        if foreign:
            raise
    if token is not None:
        try:
            conn = await connect(token)
        except websockets.InvalidStatus as exc:
            if not refused(exc):
                raise
            _note_token_refused(peer, foreign, 401)
            if foreign:
                raise
    if conn is None:
        for attempt in (0, 1):
            bearer = await fetch_peer_cred_async(ssh_alias, force=(attempt == 1))
            try:
                conn = await connect(bearer)
            except websockets.InvalidStatus as exc:
                # Handshake rejected by the peer edge. 401 here means the
                # bearer was stale (rotated) — force a re-fetch and reconnect
                # once, the WS analogue of call_peer's 401 retry. Any other
                # status, or a second rejection, propagates loudly.
                if attempt == 0 and refused(exc):
                    continue
                raise
            break

    async with conn:
        if on_connect is not None:
            on_connect()
        async for raw in conn:
            if isinstance(raw, (bytes, bytearray)):
                # Non-direct emit fan-out is always JSON text; ignore binary.
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                yield raw


# ---------------------------------------------------------------------------
# Cross-peer BYTES — the third transport, beside cross-peer calls and cross-peer
# streaming. Neither of those can carry a file: `call_peer` is JSON and fully
# buffered, and `subscribe_peer` discards binary frames outright. A service that
# produces a file therefore returns its *address* on the serving node's
# `fileviewer` mount, and the caller pulls the bytes down here.
# ---------------------------------------------------------------------------

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class _FileFetch:
    """The outcome of one download attempt, shaped for :func:`peer_send_sync`
    (which reads only ``status_code``); the bytes are already on disk on a 2xx."""

    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _safe_basename(name: str, fallback: str = "peer-file") -> str:
    """A filesystem-safe basename for a file whose name a PEER chose.

    Directory components are stripped and unsafe characters collapsed: the
    remote side names the file, this side names the directory, and nothing the
    remote sends may escape it.
    """
    base = os.path.basename(urllib.parse.unquote(name or "")).strip()
    base = _UNSAFE_NAME.sub("_", base).strip("._")
    return base or fallback


def fetch_peer_file_sync(
    peer: str,
    url: str,
    *,
    dest_dir: str | None = None,
    filename: str | None = None,
    entry: dict | None = None,
    as_: str | None = None,
    timeout: float = 120.0,
) -> str:
    """Download ``url`` from peer node ``peer`` and return the LOCAL path.

    ``url`` must be **origin-relative** (``/files/tmp/awm-social-xyz/a.png``, as
    returned beside a file in a service reply). The peer names the path; this
    side names the host — an absolute or protocol-relative URL is refused, so a
    reply cannot aim the credentialed request at a third party.

    Same setup as :func:`call_peer` — resolve the peer's edge via the local
    gateway, fetch the bearer over ssh, CA-verified TLS, one forced credential
    re-fetch on a 401 — but the body is streamed to disk rather than parsed, so
    a large file never lands in memory. Raises :class:`PeerError`; a 404 is
    called out specially because the two ways to get one (the mount's denylist
    hid the file, or the peer's ``fileviewer`` is not holding its mount) are
    both invisible from the status alone.
    """
    if not isinstance(url, str) or not url.startswith("/"):
        raise PeerError(
            f"peer file url must be origin-relative (start with '/'), got {url!r}")
    split = urllib.parse.urlsplit(url)
    if split.scheme or split.netloc:
        raise PeerError(f"peer file url must name no host, got {url!r}")

    if entry is None or not entry.get("edge_url"):
        entry = resolve_peer(peer)
    edge = str(entry["edge_url"]).rstrip("/")
    ca = _peer_ca()
    target = edge + url

    if dest_dir is None:
        dest_dir = tempfile.mkdtemp(prefix=f"awm-peer-{_safe_basename(peer, 'node')}-")
    dest = os.path.join(dest_dir, _safe_basename(filename or split.path))

    def send(bearer: str) -> _FileFetch:
        headers = {"Authorization": f"Bearer {bearer}"}
        if as_ is not None:
            headers["X-Awm-As"] = as_
        # follow_redirects stays off: a redirect is the one way an origin-relative
        # URL could still end up sending the bearer somewhere else.
        with httpx.Client(timeout=timeout, verify=ca, follow_redirects=False) as cli:
            with cli.stream("GET", target, headers=headers) as resp:
                status = resp.status_code
                if status in _TOKEN_REFUSED or status == 404:
                    return _FileFetch(status)
                if status >= 400:
                    resp.read()
                    return _FileFetch(status, resp.text)
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_bytes(65536):
                        fh.write(chunk)
                return _FileFetch(status)

    # A foreign peer's edge serves only /tools and /invoke, so a foreign fetch
    # fails with the 404 below; it is never offered the legacy bearer.
    result = peer_send_sync(peer, entry, send)
    if result.status_code == 404:
        raise PeerError(
            f"{peer} has no file at {url} — either its fileviewer "
            f"denylist hides it (*.key, *.pem, *.token, credentials, "
            f"secrets/…, which 404 exactly like a missing file), or "
            f"the peer's fileviewer is not holding its mount")
    if result.status_code in _TOKEN_REFUSED:
        raise PeerError(
            f"GET {url} from {peer} failed: unauthorized after credential refresh")
    if result.status_code >= 400:
        raise PeerError(
            f"GET {url} from {peer} failed: HTTP {result.status_code}: "
            f"{result.text[:200]}")
    return dest


async def fetch_peer_file(
    peer: str,
    url: str,
    *,
    dest_dir: str | None = None,
    filename: str | None = None,
    entry: dict | None = None,
    as_: str | None = None,
    timeout: float = 120.0,
) -> str:
    """:func:`fetch_peer_file_sync` off the event loop.

    The download blocks on an ssh, a TLS handshake and then arbitrarily many
    bytes to disk. Run in a thread so one large attachment cannot stall the
    caller's whole service.
    """
    return await asyncio.to_thread(
        fetch_peer_file_sync, peer, url, dest_dir=dest_dir, filename=filename,
        entry=entry, as_=as_, timeout=timeout)


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


def call_sync_maybe_peer(
    peer: str | None,
    service: str,
    fn: str,
    args: dict | None = None,
    *,
    as_: str | None = None,
    timeout: float = 30.0,
) -> Any:
    """:func:`call_sync` when ``peer`` is falsy, else :func:`call_peer_sync`.

    The blocking twin of :func:`call_maybe_peer`, for a consumer whose call site
    is not on the event loop. It exists so such a consumer routes through the
    same single branch point as everyone else: a sync caller that hand-rolls its
    own local POST is exactly how a node ends up borrowing a singleton for one
    consumer and not another, which is the half-routing this module's selectors
    are here to prevent.
    """
    if peer:
        return call_peer_sync(peer, service, fn, args, as_=as_, timeout=timeout)
    return call_sync(service, fn, args, as_=as_, timeout=timeout)


async def subscribe_maybe_peer(
    peer: str | None,
    service: str,
    topic: str,
    *,
    as_: str | None = None,
    on_connect: Any = None,
) -> AsyncIterator[Any]:
    """:func:`subscribe` when ``peer`` is falsy, else :func:`subscribe_peer`.

    The streaming twin of :func:`call_maybe_peer`. Yields identically either way
    (both decode paths are byte-for-byte the same), so a consumer's event
    handling is oblivious to whether the stream is local or from a peer.
    """
    if peer:
        gen = subscribe_peer(peer, service, topic, as_=as_,
                             on_connect=on_connect)
    else:
        gen = subscribe(service, topic, as_=as_, on_connect=on_connect)
    async for item in gen:
        yield item


# ---------------------------------------------------------------------------
# Direct-session leases — hold a service slot for as long as a WS stays open.
# The open socket IS the lease (ZooKeeper-ephemeral / etcd-keepalive style): the
# holder keeps it open for the guarded work, then reports a verdict; a drop is
# observed by the owning service at once. The two-step open mirrors the browser
# direct-session handshake (POST /svc/<svc>/session/<kind> → open the ws_path)
# and, for a peer, goes straight to the peer edge over CA-verified TLS with the
# peer bearer — the session analogue of call_peer / subscribe_peer.
# ---------------------------------------------------------------------------


class Lease:
    """A held direct-session slot. The OPEN WebSocket is the lease: hold it for
    the duration of the guarded work, then :meth:`verdict` (clean release) or let
    it close/drop — the owning service treats an unreported drop as a failure.

    Use as an async context manager so any exit path still drops the socket::

        async with await acquire_lease_maybe_peer(peer, "ssh", host) as lease:
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


def _lease_ws_path(resp: httpx.Response, where: str) -> str:
    if resp.status_code < 200 or resp.status_code >= 300:
        raise GatewayCallError(resp.status_code, resp.text,
                               service=where, fn="session")
    ws_path = (resp.json() or {}).get("ws_path")
    if not ws_path:
        raise GatewayCallError(resp.status_code,
                               "session open returned no ws_path",
                               service=where, fn="session")
    return ws_path


async def _read_grant(ws: Any, node: str | None = None) -> Lease:
    """Read the owning service's first frame — its grant/deny — into a Lease."""
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
        raise PeerError(f"lease grant not received: {exc}") from exc
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
    as_: str | None = None,
    node: str | None = None,
    timeout: float = 10.0,
) -> Lease:
    """Open a direct-session lease on a LOCAL service and read the grant frame.

    POSTs ``{hub}/svc/{service}/session/{kind}`` with ``{"host": host}`` to
    allocate the session, then opens the returned ``ws_path`` and reads the first
    frame (the owning service's grant/deny). The caller holds the :class:`Lease`
    for the guarded work and reports a verdict.
    """
    import websockets

    base = hub_base_url()
    body = json.dumps({"host": host, "node": node} if node else {"host": host})
    async with httpx.AsyncClient(timeout=timeout) as cli:
        resp = await cli.post(f"{base}/svc/{service}/session/{kind}",
                              content=body, headers=_headers(as_))
    ws_path = _lease_ws_path(resp, service)
    ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
    extra = [("X-Awm-As", as_)] if as_ is not None else None
    ws = await websockets.connect(f"{ws_base}{ws_path}",
                                  additional_headers=extra,
                                  max_size=None, open_timeout=10)
    return await _read_grant(ws, node)


async def acquire_lease_peer(
    peer: str,
    service: str,
    host: str,
    *,
    kind: str = "lease",
    as_: str | None = None,
    node: str | None = None,
    timeout: float = 10.0,
) -> Lease:
    """:func:`acquire_lease` against a PEER node's service, via its edge directly.

    The direct-session analogue of :func:`call_peer` / :func:`subscribe_peer`:
    resolve the peer edge, POST the session open with a node-signed token (the
    legacy ssh bearer only for a domestic peer that refuses it, see
    :func:`peer_send`) and open the lease WS **on the peer edge** over
    CA-verified TLS with the credential that opened the session. One retry if the
    socket handshake rejects it with a 401.
    """
    import ssl

    import websockets

    entry = await resolve_peer_async(peer)
    edge = entry["edge_url"].rstrip("/")
    foreign = _is_foreign(entry)
    ws_base = edge.replace("https://", "wss://").replace("http://", "ws://")
    ca = _peer_ca()
    ssl_ctx = ssl.create_default_context(cafile=ca)
    body = json.dumps({"host": host, "node": node} if node else {"host": host})
    where = f"{service}@{peer}"

    async def post(bearer: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout, verify=ca) as cli:
            return await cli.post(f"{edge}/svc/{service}/session/{kind}",
                                  content=body, headers=_peer_headers(bearer, as_))

    ws = None
    for attempt in (0, 1):
        resp, bearer = await peer_send_cred(peer, entry, post)
        ws_path = _lease_ws_path(resp, where)
        headers = [("Authorization", f"Bearer {bearer}")]
        if as_ is not None:
            headers.append(("X-Awm-As", as_))
        try:
            ws = await websockets.connect(
                f"{ws_base}{ws_path}", additional_headers=headers,
                ssl=ssl_ctx, max_size=None, open_timeout=10)
        except websockets.InvalidStatus as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # The credential that opened the session was refused on the socket:
            # drop what we cached and open once more. Never for a foreign peer,
            # whose retry would only repeat the same refusal.
            if attempt == 0 and status in _TOKEN_REFUSED and not foreign:
                _peer_token_cache.pop(_peer_aud(peer), None)
                _peer_cred_cache.pop(entry.get("ssh_alias") or peer, None)
                continue
            raise
        break
    if ws is None:
        raise PeerError(f"could not open lease WS on {where}")
    return await _read_grant(ws)


async def acquire_lease_maybe_peer(
    peer: str | None,
    service: str,
    host: str,
    *,
    kind: str = "lease",
    as_: str | None = None,
    node: str | None = None,
    timeout: float = 10.0,
) -> Lease:
    """:func:`acquire_lease` when ``peer`` is falsy, else :func:`acquire_lease_peer`.

    The direct-session twin of :func:`call_maybe_peer`: one branch decides
    local-vs-peer for a slot-arbiter consumer, so the decision lives here.
    """
    if peer:
        return await acquire_lease_peer(peer, service, host, kind=kind, as_=as_,
                                        node=node, timeout=timeout)
    return await acquire_lease(service, host, kind=kind, as_=as_, node=node,
                               timeout=timeout)


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
