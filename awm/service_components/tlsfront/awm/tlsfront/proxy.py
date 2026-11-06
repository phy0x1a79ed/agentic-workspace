"""A TLS reverse proxy with awm's edge session in front of one loopback upstream.

An awm listener that a browser reaches off-host needs three things: TLS (a
*secure context*, which ``getUserMedia`` and friends demand off-localhost), the
edge session check, and a faithful relay of HTTP and WebSockets to a loopback
upstream that has no auth of its own. This module is those three, once.
:func:`serve` is the whole of it for a front that wraps one app (``dsh``,
``claude-science``); ``httpsfront`` composes its gateway front from the parts.

Two things the front asserts on every proxied request: the caller is
authenticated, and ``X-Awm-As`` names the identity the session was minted for
(``user:<sub>``). The browser's own value of that header is discarded —
downstream services trust it, so only the edge may write it.

**Routing reads the decoded path; forwarding sends the raw one.** Both are
deliberate and neither may be "simplified" into the other. Every routing and
policy decision needs the *decoded* path, because that is what the caller
means; every upstream request needs the *raw* bytes, because rebuilding a URL
from the decoded string loses the difference between ``%23`` and ``#`` and
between ``%2e%2e`` and a directory climb. The gap between the two is closed by
refusing any target that re-segments when decoded — see :func:`re_segments`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from verify_grant import GrantInvalid

from awm.config import usertoken
from awm.tlsfront import portal
from awm.tlsfront.auth import AS_COOKIE_NAME, COOKIE_NAME, AuthGate, Shape, bearer_of

log = logging.getLogger("awm.tlsfront.proxy")

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1); the client/httpx set
# their own. ``host`` is dropped so httpx derives it from the upstream URL.
HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
})

#: Request methods the front forwards (a superset covering the awm surface).
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

#: Headers the gateway reads as *who is calling* or *which session this is*.
#: Only a local process may set the pid pair and the redirect flag; a caller
#: that crossed the edge could otherwise point reflection or `start_other`'s parent
#: stamp at any local session. The service, isolated-session and edge-lane
#: credentials name a caller too, and none of them is a browser's to present.
#: Dropped, never forwarded, case-insensitively.
CALLER_HEADERS = frozenset({
    "x-awm-as", "x-awm-caller-pid", "x-awm-session-pid", "x-awm-peer-redirect",
    "x-awm-service-token", "x-awm-caller-token", "x-awm-caller", "x-awm-edge-token"})


# Routing reads the *decoded* path; forwarding sends the *raw* one. Those two
# facts are the whole of the encoding contract on this edge, and they are the
# kind of invariant that reads as an accident and gets "simplified" back into a
# bug — so they live together, here, with the reason.


def raw_target(scope, decoded: str) -> bytes:
    """The request path exactly as it arrived, percent-encoding intact.

    Starlette's ``url.path`` is percent-*decoded*, so rebuilding an upstream URL
    out of it destroys the difference between ``%23`` and ``#``: httpx reparses
    the string it is handed and takes everything after a ``#`` as a fragment,
    which it never sends. Trilium's client escapes its search string into the
    path, so that alone truncated every attribute search to ``/api/search/``.

    ASGI keeps the original bytes in ``raw_path`` — uvicorn sets it on both its
    h11 and its httptools implementation, HTTP and WebSocket alike. A server
    that omits it leaves us re-encoding the decoded path, which is lossy in
    exactly this way but no worse than what it replaces.
    """
    raw = scope.get("raw_path")
    if raw:
        return raw.split(b"?", 1)[0]
    return quote(decoded).encode("ascii")


def re_segments(raw: bytes, decoded: str) -> bool:
    """Whether this target means one thing to the router and another upstream.

    The two paths agree on everything except a target that *re-segments* when
    decoded. An encoded separator (``%2f``, ``%5c``) is one segment to the
    router and two to the upstream; a ``..`` — plain or arrived as ``%2e%2e`` —
    is a segment to the router and a level up to any client that normalises
    before sending, which is how ``/trilium/api/%2e%2e/etapi/app-info`` used to
    classify as the vault's and arrive as the unauthenticated ETAPI's.

    Refused outright rather than canonicalised: nothing a front serves needs
    either form, and a 404 is the same answer every other unknown path gets.
    """
    low = raw.lower()
    if b"%2f" in low or b"%5c" in low:
        return True
    return ".." in decoded.split("/")


def upstream_url(origin: str, raw_path: bytes, query: bytes) -> httpx.URL:
    """``origin`` + the target, byte for byte.

    ``copy_with(raw_path=…)`` sets the target verbatim instead of reparsing a
    string — the reparse is what swallowed the fragment and what normalised the
    ``..``. It carries the query with it, so nothing else may append one.
    """
    return httpx.URL(origin).copy_with(
        raw_path=raw_path + (b"?" + query if query else b""))


def ws_url(origin: str, raw_path: bytes, query: bytes) -> str:
    """The upstream socket URL. ``websockets.connect`` parses it without
    decoding its path, so the bytes reach the upstream as they arrived — the
    same contract the HTTP leg gets from :func:`upstream_url`."""
    return origin + (raw_path + (b"?" + query if query else b"")).decode("ascii")


def query_of(conn: Request | WebSocket) -> bytes:
    return conn.scope.get("query_string") or b""


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of ``url`` — an RFC 6454 origin, no path."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def as_header(sub: str | None) -> str:
    """The identity stamped on a forwarded request: ``user:<sub>``."""
    return f"user:{sub or 'operator'}"


def origin_override(app) -> str | None:
    """The scheme+authority a rewriting front presents as ``Origin``, or None.

    :data:`HOP` drops the browser's ``Host`` so httpx derives it from the
    upstream URL — so an upstream that compares ``Origin`` against ``Host`` sees
    a pair that cannot match while ``Origin`` is forwarded verbatim. Rewriting
    it to the upstream's own origin restores the comparison the upstream is
    actually trying to make: same-origin, as it would be on loopback.
    """
    return getattr(app.state, "origin_override", None)


def consumed_edge_token(authorization: str | None) -> bool:
    """Whether this ``Authorization`` is a device token the edge has read.

    Any ``awmut1.`` bearer counts, verified or not: one that failed here may
    still be live where it was meant for, and an upstream app that saw it could
    replay it there.
    """
    return usertoken.looks_like_token(bearer_of(authorization))


def req_headers(request: Request, sub: str | None = None) -> dict[str, str]:
    hdrs = {k: v for k, v in request.headers.items()
            if k.lower() not in HOP and k.lower() not in CALLER_HEADERS}
    if consumed_edge_token(request.headers.get("authorization")):
        hdrs.pop("authorization", None)
    hdrs["X-Forwarded-Proto"] = "https"
    # Overwrite, never default: the browser's value is unverified.
    hdrs["X-Awm-As"] = as_header(sub)
    override = origin_override(request.app)
    # Only rewrite a header the browser actually sent: minting an Origin where
    # there was none turns a same-origin navigation into a cross-origin one.
    if override and "origin" in hdrs:
        hdrs["origin"] = override
    # ``host`` is dropped above so httpx derives it from the upstream URL, which
    # means the upstream otherwise cannot tell what address the browser used.
    # awm's own services never needed that, but a reverse-proxied third party
    # does: an app that builds its self-origin from the request (for redirects,
    # or to decide the ``Secure`` flag on its own cookies) derives a loopback
    # http:// origin without this, and its browser session never persists.
    host = request.headers.get("host")
    if host:
        hdrs["X-Forwarded-Host"] = host
    if request.client:
        hdrs["X-Forwarded-For"] = request.client.host
    return hdrs


def resp_headers(resp: httpx.Response) -> list[tuple[str, str]]:
    # Preserve everything except hop-by-hop; keep raw ``aiter_raw`` bytes so
    # content-encoding + content-length stay consistent (we pass bytes through).
    return [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in HOP]


def streamed(resp: httpx.Response, body: AsyncIterator[bytes] | None = None,
             ) -> StreamingResponse:
    """``resp`` relayed to the browser as it arrives, headers intact.

    The raw header list is assigned rather than passed as ``headers=dict(...)``:
    a dict keeps only the last of a repeated key, and ``Set-Cookie`` is repeated
    whenever an upstream sets more than one cookie in a response. Collapsing
    those loses every cookie but the last, which is invisible until a wrapped
    app's session simply fails to establish. ``set_cookie`` appends to this
    same list.

    ``body`` replaces the default relay, which closes the upstream in a
    background task once the body is sent.
    """
    if body is None:
        out = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code,
                                background=BackgroundTask(resp.aclose))
    else:
        out = StreamingResponse(body, status_code=resp.status_code)
    out.raw_headers = [
        (k.encode("latin-1"), v.encode("latin-1")) for k, v in resp_headers(resp)
    ]
    return out


def wants_html(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


def samesite(app) -> str:
    return getattr(app.state, "samesite", "lax")


def set_session_cookie(resp: Response, token: str, max_age: int,
                       samesite: str = "lax") -> None:
    resp.set_cookie(
        COOKIE_NAME, token, max_age=max_age, path="/",
        httponly=True, secure=True, samesite=samesite,
    )


def set_as_cookie(resp: Response, sub: str, max_age: int,
                  samesite: str = "lax") -> None:
    resp.set_cookie(
        AS_COOKIE_NAME, sub, max_age=max_age, path="/",
        httponly=False, secure=True, samesite=samesite,
    )


async def refresh_cookie(app, resp: Response, refreshed: str | None) -> None:
    """Re-set a slid session cookie on ``resp``."""
    if refreshed:
        set_session_cookie(resp, refreshed,
                           int(await app.state.gate.session_ttl_seconds()),
                           samesite(app))


def unpack(result) -> tuple[bool, str | None, str | None]:
    """``(ok, refreshed, sub)`` from a gate answer; a two-tuple (an older gate)
    means the operator."""
    ok, refreshed = result[0], result[1]
    sub = result[2] if len(result) > 2 else None
    return ok, refreshed, (sub or "operator") if ok else None


async def authenticate_sub(request: Request, shape: Shape | None = None,
                           ) -> tuple[bool, str | None, str | None]:
    """(ok, refreshed_cookie_token_or_None, sub) for the current request.

    Only the proxy legs pass ``shape``, so a scoped device token reaches the
    paths its scope names and none of the edge's own routes.
    """
    gate: AuthGate = request.app.state.gate
    return unpack(await gate.authenticate(
        cookie=request.cookies.get(COOKIE_NAME),
        bearer=bearer_of(request.headers.get("authorization")),
        shape=shape,
    ))


async def authenticate_ws(ws: WebSocket, path: str, raw: bytes,
                          ) -> tuple[bool, str | None]:
    """``(ok, sub)`` for a socket handshake.

    Starlette HTTP handling never sees a WS scope, so the guard is enforced
    here, before ``accept()``. A browser sends the session cookie on the
    handshake (same-origin); a client sends a bearer. No cookie refresh on a
    socket, since it is not an HTTP response.
    """
    gate: AuthGate = ws.app.state.gate
    ok, _, sub = unpack(await gate.authenticate(
        cookie=ws.cookies.get(COOKIE_NAME),
        bearer=bearer_of(ws.headers.get("authorization")),
        shape=Shape("GET", path, raw, query_of(ws), True),
    ))
    return ok, sub


def http_shape(request: Request, path: str, raw: bytes) -> Shape:
    return Shape(request.method, path, raw, query_of(request), False)


def not_found() -> Response:
    return Response("not found", status_code=404)


def client_headers(conn: Request | WebSocket) -> dict[str, str]:
    """The client-address headers the HTTP leg gives the relay, for a socket.

    The same values :func:`req_headers` sends: the peer address as
    ``X-Forwarded-For``, and a ``cf-connecting-ip`` passed on as it arrived.
    """
    out: dict[str, str] = {}
    cf = conn.headers.get("cf-connecting-ip")
    if cf:
        out["cf-connecting-ip"] = cf
    if conn.client:
        out["X-Forwarded-For"] = conn.client.host
    return out


def deny(request: Request) -> Response:
    """The approver's passkey login for a browser GET, else 401."""
    if request.method == "GET" and wants_html(request):
        host = request.headers.get("host") or ""
        path = portal.local_path(request.url.path) or "/"
        return RedirectResponse(portal.login_url(f"https://{host}{path}"), status_code=302,
                                headers={"Cache-Control": "no-store"})
    return JSONResponse({"error": "unauthenticated"}, status_code=401)


async def body(request: Request):
    """Stream a request body of known length upstream rather than holding it
    whole: a peer's file-hop call can carry gigabytes. Anything else is read
    first, so the upstream leg is framed by length and a caller's
    ``Transfer-Encoding`` never reaches it."""
    if request.headers.get("content-length") not in (None, "", "0"):
        return request.stream()
    return await request.body()


def clear_identity_cookies(resp: Response, app) -> None:
    """Drop the upstream's own cookies a front names, when the awm identity changes.

    Identity changes at exactly two moments, sign-in and sign-out, and both
    call this.
    """
    for name in getattr(app.state, "identity_cookies", ()):
        resp.delete_cookie(name, path="/")


async def portal_callback(request: Request) -> Response:
    """``GET /__auth/portal?grant=<grant>&next=<path>``: the approver's way back.

    The grant is alcor's ``portal_login`` grant for this node, this host and this
    path, spent once. The only success is a 302 to ``next`` with the session set,
    so nothing renders under the address that carried the grant.
    """
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    path = portal.local_path(request.query_params.get("next"))
    if path is None:
        return JSONResponse({"error": "next is a local path"}, status_code=400, headers=headers)
    try:
        sub = request.app.state.grants.accept(
            request.query_params.get("grant") or "",
            portal.host_of(request.headers.get("host")), path)
    except GrantInvalid as exc:
        log.info("edge: portal grant refused: %s", exc)
        return JSONResponse({"error": "the sign-in grant is refused"}, status_code=403,
                            headers=headers)
    minted = await request.app.state.gate.mint_session(sub) if sub else None
    if minted is None:
        return JSONResponse({"error": "no session can be signed here"}, status_code=503,
                            headers=headers)
    token, ttl = minted
    resp = RedirectResponse(path, status_code=302, headers=headers)
    site = samesite(request.app)
    set_session_cookie(resp, token, ttl, site)
    set_as_cookie(resp, sub, ttl, site)
    clear_identity_cookies(resp, request.app)
    return resp


async def logout(request: Request) -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    resp.delete_cookie(AS_COOKIE_NAME, path="/")
    clear_identity_cookies(resp, request.app)
    return resp


async def whoami(request: Request) -> Response:
    ok, _, sub = await authenticate_sub(request)
    if ok:
        return JSONResponse({"user": sub})
    return JSONResponse({"error": "unauthenticated"}, status_code=401)


async def ca(request: Request) -> Response:
    """Serve the local root CA so a device can install + trust it once, clearing
    ERR_CERT_AUTHORITY_INVALID. Sent as a downloadable
    ``application/x-x509-ca-cert`` so Android/iOS offer to install it."""
    ca_path: str = request.app.state.ca_path
    try:
        data = Path(ca_path).read_bytes()
    except OSError:
        return Response("CA not available", status_code=404)
    return Response(
        data,
        media_type="application/x-x509-ca-cert",
        headers={"Content-Disposition": 'attachment; filename="awm-ca.crt"'},
    )


def upstream_unreachable(request: Request) -> Response:
    return Response("upstream gateway unreachable", status_code=502)


async def forward(request: Request, up: str, raw: bytes, sub: str | None,
                  refreshed: str | None, *,
                  unreachable: Callable[[Request], Response] = upstream_unreachable,
                  content: bytes | None = None,
                  ) -> Response:
    """Relay one authenticated request to ``up`` as ``sub``.

    ``content`` is a body the caller has already read; otherwise the request's
    own is relayed (see :func:`body`).
    """
    client: httpx.AsyncClient = request.app.state.client
    upstream_req = client.build_request(
        request.method, upstream_url(up, raw, query_of(request)),
        headers=req_headers(request, sub),
        content=await body(request) if content is None else content,
    )
    try:
        resp = await client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        return unreachable(request)
    out = streamed(resp)
    await refresh_cookie(request.app, out, refreshed)
    return out


async def http_proxy(request: Request) -> Response:
    """The catch-all: authenticate, then relay to the one upstream."""
    path = request.url.path
    raw = raw_target(request.scope, path)
    if re_segments(raw, path):
        return not_found()
    ok, refreshed, sub = await authenticate_sub(request, http_shape(request, path, raw))
    if not ok:
        return deny(request)
    return await forward(request, request.app.state.http_up, raw, sub, refreshed)


def ws_headers(ws: WebSocket) -> dict[str, str]:
    """The handshake headers a socket carries upstream, before any identity.

    Cookies so the upstream sees its own session. ``origin`` and the
    forwarded-* hints matter only for a reverse-proxied third party: an upstream
    that origin-checks its upgrades (an allowlist of the exact scheme+host+port
    a browser may connect from) rejects every handshake if the header never
    arrives. Only these are copied, so none of :data:`CALLER_HEADERS` can ride
    along; a device token is dropped for the reason given at
    :func:`consumed_edge_token`.
    """
    fwd: dict[str, str] = {}
    for k in ("cookie", "authorization", "origin"):
        v = ws.headers.get(k)
        if v:
            fwd[k] = v
    if consumed_edge_token(fwd.get("authorization")):
        fwd.pop("authorization", None)
    override = origin_override(ws.app)
    if override and "origin" in fwd:
        fwd["origin"] = override
    fwd["X-Forwarded-Proto"] = "https"
    host = ws.headers.get("host")
    if host:
        fwd["X-Forwarded-Host"] = host
    return fwd


def upstream_refusal(exc: Exception) -> int:
    """The status to answer a client whose upstream handshake failed with ``exc``.

    The upstream's own 4xx or 5xx goes through unchanged. The tether relay's
    clients read it: a 404 on a held slot means the relay restarted and the
    operator must restore the slot, while any other answer is a path to redial.
    """
    if isinstance(exc, websockets.exceptions.InvalidStatus):
        status = exc.response.status_code
        if 400 <= status < 600:
            return status
    return 502


async def refuse_handshake(ws: WebSocket, status: int) -> None:
    """Reject the handshake outright with ``status``, never accept-then-close.

    Without the ASGI denial extension the server can only answer 403.
    """
    if "websocket.http.response" in ws.scope.get("extensions", {}):
        await ws.send_denial_response(Response(status_code=status))
    else:
        await ws.close(code=1011)


async def bridge(ws: WebSocket, up_url: str, fwd: dict[str, str]) -> None:
    """Open ``up_url`` and pump text and binary frames both ways until either
    side closes."""
    # Subprotocols are negotiated end to end rather than dropped: a browser that
    # asks for one and gets a handshake response without it fails the connection
    # per RFC 6455.
    subprotocols = list(ws.scope.get("subprotocols") or [])
    try:
        upstream = await websockets.connect(
            up_url, additional_headers=fwd, max_size=None, open_timeout=10,
            subprotocols=subprotocols or None,
        )
    except Exception as exc:  # noqa: BLE001 — upstream refused / bad path
        # Path only, never the query string — see the log_level note in serve_app().
        log.debug("ws upstream connect failed for %s: %s", ws.url.path, exc)
        await refuse_handshake(ws, upstream_refusal(exc))
        return
    await ws.accept(subprotocol=getattr(upstream, "subprotocol", None))

    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except WebSocketDisconnect:
            pass

    async def upstream_to_client() -> None:
        try:
            async for data in upstream:
                if isinstance(data, (bytes, bytearray)):
                    await ws.send_bytes(bytes(data))
                else:
                    await ws.send_text(data)
        except Exception:  # noqa: BLE001 — upstream closed
            pass

    a = asyncio.ensure_future(client_to_upstream())
    b = asyncio.ensure_future(upstream_to_client())
    try:
        await asyncio.wait({a, b}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (a, b):
            task.cancel()
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


async def ws_proxy(ws: WebSocket) -> None:
    """The socket catch-all: authenticate, then bridge to the one upstream."""
    path = ws.url.path
    raw = raw_target(ws.scope, path)
    if re_segments(raw, path):
        await ws.close(code=1008)
        return
    ok, sub = await authenticate_ws(ws, path, raw)
    if not ok:
        await ws.close(code=1008)  # policy violation
        return
    fwd = ws_headers(ws)
    fwd["X-Awm-As"] = as_header(sub)
    await bridge(ws, ws_url(ws.app.state.ws_up, raw, query_of(ws)), fwd)


#: An extra endpoint a wrapping front adds: ``(path, methods, handler)``.
ExtraRoute = tuple[str, Sequence[str], Callable[[Request], Awaitable[Response]]]


def gated(
    handler: Callable[[Request], Awaitable[Response]],
) -> Callable[[Request], Awaitable[Response]]:
    """Wrap a route in the edge session check the catch-all proxy applies.

    Same deny shape (login page for a browser GET, 401 otherwise) and the same
    sliding-cookie refresh, so an added endpoint is indistinguishable from a
    proxied path as far as auth is concerned.
    """
    async def _wrapped(request: Request) -> Response:
        ok, refreshed, _ = await authenticate_sub(request)
        if not ok:
            return deny(request)
        resp = await handler(request)
        await refresh_cookie(request.app, resp, refreshed)
        return resp

    return _wrapped


def build_app(upstream: str, ca_path: str, *,
              extra_routes: Sequence[ExtraRoute] | None = None,
              rewrite_origin: bool = False,
              routes: Sequence[BaseRoute] = (),
              http_handler: Callable[[Request], Awaitable[Response]] = http_proxy,
              ws_handler: Callable[[WebSocket], Awaitable[None]] = ws_proxy,
              ca_download: bool = True,
              same_site: str = "lax",
              identity_cookies: Sequence[str] = (),
              lifespan: Callable[[Starlette], AbstractAsyncContextManager] | None = None,
              ) -> Starlette:
    """Assemble a front for ``upstream``.

    Routes, in order: ``/ca.crt`` and ``/ca.pem`` (unless ``ca_download`` is
    off), the edge's own ``/__auth/*``, then ``routes`` as given (ungated — a
    front gates its own), then ``extra_routes``, then the catch-alls.

    ``extra_routes`` are ``(path, methods, handler)`` triples for endpoints the
    upstream cannot serve itself — a sign-in shim that has to shell out to the
    wrapped binary, say. They come after the ``/__auth/*`` routes, so they
    cannot shadow the edge's own auth surface, and **this function gates
    them** with the same check the catch-all proxy applies, including the
    sliding cookie refresh. Handlers are therefore plain endpoints that may
    assume an authenticated caller: the gate is not something a caller can
    forget, because the whole reason such a route exists is that it does
    something privileged on the upstream's behalf.

    ``rewrite_origin=True`` replaces a present ``Origin`` with the upstream's
    own scheme+authority on both the HTTP and the WebSocket path. Two wrapped
    apps want opposite things here. One origin-checks its WebSocket upgrades
    against an allowlist of the exact browser origins it expects, and needs the
    real header (``claude-science``). The other compares ``Origin`` against
    ``Host`` and demands they match — and since ``Host`` is dropped so httpx
    derives it from the upstream URL, the browser's real ``Origin`` can never
    match it, so every request 403s (``dsh``). Default off.

    ``identity_cookies`` names upstream cookies to drop at sign-in and
    sign-out. ``lifespan`` runs inside the front's own, which owns the shared
    upstream client at ``app.state.client``.
    """
    http_up = upstream.rstrip("/")
    # http://… → ws://… ,  https://… → wss://…
    ws_up = "ws" + http_up[len("http"):]

    table: list[BaseRoute] = []
    if ca_download:
        # No auth: the CA download is how a device comes to trust the front.
        table.append(Route("/ca.crt", ca, methods=["GET"]))
        table.append(Route("/ca.pem", ca, methods=["GET"]))
    # Auth endpoints — handled by the edge itself, never proxied.
    table.append(Route(portal.CALLBACK, portal_callback, methods=["GET"]))
    table.append(Route("/__auth/logout", logout, methods=["POST", "GET"]))
    table.append(Route("/__auth/whoami", whoami, methods=["GET"]))
    table += routes
    for path, methods, handler in (extra_routes or ()):
        table.append(Route(path, gated(handler), methods=list(methods)))
    table += [
        WebSocketRoute("/{path:path}", ws_handler),
        Route("/{path:path}", http_handler, methods=METHODS),
    ]

    # Lifespan (Starlette 1.3+ removed the @app.on_event decorator): own the
    # shared upstream httpx client for the server's lifetime.
    @asynccontextmanager
    async def _lifespan(app_: Starlette):
        app_.state.client = httpx.AsyncClient(timeout=None, follow_redirects=False)
        try:
            if lifespan is None:
                yield
            else:
                async with lifespan(app_):
                    yield
        finally:
            await app_.state.client.aclose()

    app = Starlette(routes=table, lifespan=_lifespan)
    app.state.http_up = http_up
    app.state.ws_up = ws_up
    app.state.ca_path = ca_path
    # Derived from the upstream URL rather than taken as a string, so the
    # rewritten Origin is by construction the one httpx will also put in Host.
    app.state.origin_override = origin_of(http_up) if rewrite_origin else None
    app.state.gate = AuthGate()
    app.state.grants = portal.Grants()
    app.state.samesite = same_site
    app.state.identity_cookies = tuple(identity_cookies)
    return app


def serve_app(app: Starlette, *, port: int, cert: str, key: str,
              tls: bool = True, label: str = "") -> None:
    """Bind ``0.0.0.0:port`` with TLS and serve ``app`` forever (blocks).
    Designed to run in a daemon thread from a hub adapter.

    ``tls=False`` binds plain HTTP on ``127.0.0.1:port`` instead, trusting
    ``X-Forwarded-*`` from loopback only — the shape for a TLS-terminating
    nginx in front. ``cert``/``key`` are unused then.
    """
    bind: dict = (
        {"host": "0.0.0.0", "ssl_certfile": cert, "ssl_keyfile": key}
        if tls else
        {"host": "127.0.0.1", "proxy_headers": True,
         "forwarded_allow_ips": "127.0.0.1"}
    )
    config = uvicorn.Config(
        app,
        port=port,
        **bind,
        # SECURITY, not tidiness: uvicorn writes access records — including the
        # full query string — to `uvicorn.access` at INFO. `GET /__auth/portal?grant=…`
        # carries a live sign-in grant, so raising this to "info" or "debug" to
        # chase an unrelated problem would start writing grants to disk.
        # Pinned by tests/test_no_access_logging.py.
        log_level="warning",
        ws="websockets",
        timeout_keep_alive=75,
        # Off the main thread uvicorn skips signal handlers automatically.
    )
    server = uvicorn.Server(config)
    log.info("front listening on %s:%d → %s (tls %s)",
             bind["host"], port, label or app.state.http_up, "on" if tls else "off")
    server.run()


def serve(*, port: int, cert: str, key: str, ca: str, upstream: str,
          extra_routes: Sequence[ExtraRoute] | None = None,
          rewrite_origin: bool = False) -> None:
    """Front one loopback app with TLS and the edge session (blocks).

    ``/`` falls through to the upstream, so a wrapped app's SPA keeps its root.
    """
    serve_app(build_app(upstream, ca, extra_routes=extra_routes,
                        rewrite_origin=rewrite_origin),
              port=port, cert=cert, key=key)
