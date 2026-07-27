"""The off-host HTTPS reverse proxy — TLS front for the whole awm gateway.

The awm gateway binds **loopback plain HTTP** (``127.0.0.1:7819``) with no auth,
by design. But powerful browser APIs — ``getUserMedia`` for the notes-page
dictation, clipboard, etc. — need a *secure context*, which off-localhost means
HTTPS. So this listener terminates TLS on ``0.0.0.0:<port>`` and transparently
reverse-proxies **every** request — HTTP and WebSocket alike — to the loopback
gateway, so the entire awm surface (pages at ``/ui/*``, service RPC at
``/svc/*``, the hub control plane, config, …) is reachable over one HTTPS origin.

Because the notes page (and every awm page) makes *same-origin* relative calls
(``apiFetch('/svc/...')``, ``new WebSocket(<same-origin path>)``), fronting the
gateway wholesale is what makes those calls Just Work under HTTPS — there is no
per-path allowlist to keep in sync.

Built on Starlette + uvicorn (TLS) with ``httpx`` for HTTP and the ``websockets``
client for WS bridging — all already present in the ``awm`` env (the gateway
depends on them). Runs in a daemon thread launched from the hub adapter's
``on_start``; when the gateway drains/respawns this service, the process exits
and the listener dies with it (one supervised lifetime, exactly like ``mic``).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from awm.httpsfront import pages
from awm.httpsfront.auth import COOKIE_NAME, AuthGate, bearer_of

log = logging.getLogger("awm.httpsfront.proxy")

# Hop-by-hop headers must not be forwarded (RFC 7230 §6.1); the client/httpx set
# their own. ``host`` is dropped so httpx derives it from the upstream URL.
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}

# Request methods the front forwards (a superset covering the awm surface).
_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

_ALL_METHODS = _METHODS  # alias for route registration clarity


def _req_headers(request: Request) -> dict[str, str]:
    hdrs = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    hdrs["X-Forwarded-Proto"] = "https"
    if request.client:
        hdrs["X-Forwarded-For"] = request.client.host
    return hdrs


def _resp_headers(resp: httpx.Response) -> list[tuple[str, str]]:
    # Preserve everything except hop-by-hop; keep raw ``aiter_raw`` bytes so
    # content-encoding + content-length stay consistent (we pass bytes through).
    return [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in _HOP]


def _wants_html(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


def _set_session_cookie(resp: Response, token: str, max_age: int) -> None:
    resp.set_cookie(
        COOKIE_NAME, token, max_age=max_age, path="/",
        httponly=True, secure=True, samesite="lax",
    )


async def _authenticate(request: Request) -> tuple[bool, str | None]:
    """(ok, refreshed_cookie_token_or_None) for the current request."""
    gate: AuthGate = request.app.state.gate
    return await gate.authenticate(
        cookie=request.cookies.get(COOKIE_NAME),
        bearer=bearer_of(request.headers.get("authorization")),
    )


def _deny(request: Request) -> Response:
    """Login page for a browser GET, else 401 (API / peer)."""
    if request.method == "GET" and _wants_html(request):
        return HTMLResponse(pages.login_page(), status_code=200)
    return JSONResponse({"error": "unauthenticated"}, status_code=401)


async def _login(request: Request) -> Response:
    """``POST /__auth/login`` — validate the password via the auth service and,
    on success, set the session cookie."""
    gate: AuthGate = request.app.state.gate
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 — accept form encoding as a fallback
        form = await request.form()
        data = {"password": form.get("password", "")}
    token = await gate.verify_password(str((data or {}).get("password") or ""))
    if not token:
        return JSONResponse({"ok": False}, status_code=401)
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, token, int(await gate.session_ttl_seconds()))
    return resp


async def _logout(request: Request) -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


async def _whoami(request: Request) -> Response:
    ok, _ = await _authenticate(request)
    if ok:
        return JSONResponse({"user": "operator"})
    return JSONResponse({"error": "unauthenticated"}, status_code=401)


async def _root(request: Request) -> Response:
    """Authenticated landing page at ``/`` — a dynamic index of ``/ui/*`` pages
    pulled from the gateway registry."""
    ok, refreshed = await _authenticate(request)
    if not ok:
        return _deny(request)
    app = request.app
    services: list = []
    try:
        client: httpx.AsyncClient = app.state.client
        r = await client.get(app.state.http_up + "/hub/services")
        if r.status_code == 200:
            services = (r.json() or {}).get("services", [])
    except Exception as exc:  # noqa: BLE001 — degrade to an empty index
        log.debug("landing: could not fetch registry: %s", exc)
    resp = HTMLResponse(pages.landing_page(services))
    if refreshed:
        _set_session_cookie(resp, refreshed,
                            int(await app.state.gate.session_ttl_seconds()))
    return resp


async def _http_proxy(request: Request) -> Response:
    app = request.app
    ok, refreshed = await _authenticate(request)
    if not ok:
        return _deny(request)
    client: httpx.AsyncClient = app.state.client
    url = app.state.http_up + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    upstream_req = client.build_request(
        request.method, url, headers=_req_headers(request), content=body,
    )
    try:
        resp = await client.send(upstream_req, stream=True)
    except httpx.ConnectError:
        return Response("upstream gateway unreachable", status_code=502)
    out = StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers=dict(_resp_headers(resp)),
        background=BackgroundTask(resp.aclose),
    )
    if refreshed:
        _set_session_cookie(out, refreshed,
                            int(await app.state.gate.session_ttl_seconds()))
    return out


async def _ca(request: Request) -> Response:
    """Serve the local root CA so a device can install + trust it once, clearing
    ERR_CERT_AUTHORITY_INVALID. Sent as a downloadable
    ``application/x-x509-ca-cert`` so Android/iOS offer to install it."""
    ca_path: str = request.app.state.ca_path
    try:
        body = Path(ca_path).read_bytes()
    except OSError:
        return Response("CA not available", status_code=404)
    return Response(
        body,
        media_type="application/x-x509-ca-cert",
        headers={"Content-Disposition": 'attachment; filename="awm-ca.crt"'},
    )


async def _ws_proxy(ws: WebSocket) -> None:
    """Bridge a browser WebSocket to the same path on the loopback gateway,
    pumping text+binary frames both directions until either side closes."""
    app = ws.app
    path = ws.url.path
    if ws.url.query:
        path += "?" + ws.url.query
    up_url = app.state.ws_up + path

    # Edge auth: Starlette HTTP handling never sees a WS scope, so the guard is
    # enforced here, before accept(). A browser sends the session cookie on the
    # WS handshake (same-origin); a peer/client sends a bearer. No cookie
    # refresh on a WS (it is not an HTTP response).
    gate: AuthGate = app.state.gate
    ok, _ = await gate.authenticate(
        cookie=ws.cookies.get(COOKIE_NAME),
        bearer=bearer_of(ws.headers.get("authorization")),
    )
    if not ok:
        await ws.close(code=1008)  # policy violation
        return

    # Forward cookies / identity so the gateway sees the real caller.
    fwd = {}
    for k in ("cookie", "x-awm-as", "authorization"):
        v = ws.headers.get(k)
        if v:
            fwd[k] = v

    await ws.accept()
    try:
        upstream = await websockets.connect(
            up_url, additional_headers=fwd, max_size=None, open_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — upstream refused / bad path
        log.debug("ws upstream connect failed for %s: %s", up_url, exc)
        await ws.close(code=1011)
        return

    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await ws.receive()
                t = msg.get("type")
                if t == "websocket.disconnect":
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


def build_app(upstream: str, ca_path: str, *, landing: bool = True) -> Starlette:
    """Assemble the front. ``landing=False`` drops the awm index page at ``/``.

    The landing page is right for the gateway front, whose ``/`` has nothing
    else to serve. It is wrong for a front that sits in front of a *single*
    app — claude-view's SPA lives at ``/``, and the index route would shadow
    it. Turning it off lets ``/`` fall through to the catch-all proxy, which is
    what makes this module reusable for any upstream rather than the gateway
    alone. Default keeps the gateway front's behaviour byte-identical.
    """
    http_up = upstream.rstrip("/")
    # http://… → ws://… ,  https://… → wss://…
    ws_up = "ws" + http_up[len("http"):]

    routes = [
        # Public (no auth): CA download so a device can install the root once.
        Route("/ca.crt", _ca, methods=["GET"]),
        Route("/ca.pem", _ca, methods=["GET"]),
        # Auth endpoints — handled by the edge itself, never proxied.
        Route("/__auth/login", _login, methods=["POST"]),
        Route("/__auth/logout", _logout, methods=["POST", "GET"]),
        Route("/__auth/whoami", _whoami, methods=["GET"]),
    ]
    if landing:
        # Authenticated landing page (dynamic index of /ui/* pages).
        routes.append(Route("/", _root, methods=["GET"]))
    routes += [
        # Everything else is auth-gated inside the handler, then proxied.
        WebSocketRoute("/{path:path}", _ws_proxy),
        Route("/{path:path}", _http_proxy, methods=_ALL_METHODS),
    ]
    # Lifespan (Starlette 1.3+ removed the @app.on_event decorator): own the
    # shared upstream httpx client for the server's lifetime.
    @asynccontextmanager
    async def _lifespan(app_: Starlette):
        app_.state.client = httpx.AsyncClient(timeout=None, follow_redirects=False)
        try:
            yield
        finally:
            await app_.state.client.aclose()

    app = Starlette(routes=routes, lifespan=_lifespan)
    app.state.http_up = http_up
    app.state.ws_up = ws_up
    app.state.ca_path = ca_path
    app.state.gate = AuthGate()
    return app


def serve(*, port: int, cert: str, key: str, ca: str, upstream: str,
          landing: bool = True) -> None:
    """Bind ``0.0.0.0:port`` with TLS and reverse-proxy to ``upstream`` forever
    (blocks). Designed to run in a daemon thread from the hub adapter.

    ``upstream`` and ``landing`` are what make this reusable beyond the gateway:
    the ``claude-view`` service calls it against its own loopback binary with
    ``landing=False`` to get TLS, the shared CA, and the ``awm_session`` edge
    auth without duplicating any of it.
    """
    app = build_app(upstream, ca, landing=landing)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        ssl_certfile=cert,
        ssl_keyfile=key,
        log_level="warning",
        ws="websockets",
        timeout_keep_alive=75,
        # Off the main thread uvicorn skips signal handlers automatically.
    )
    server = uvicorn.Server(config)
    log.info("https front listening on https://0.0.0.0:%d → %s (tls on)", port, upstream)
    server.run()
