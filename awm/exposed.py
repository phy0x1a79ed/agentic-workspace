"""Network-exposed HTTPS listener for AWM.

Separate FastAPI app + uvicorn process from ``awm/server.py``. Sits on a
different port, gets its own PID file and systemd unit, and adds:

- Bearer-token auth on every request (HTTP + WS).
- Destructive-operation gate (configurable via ``AWM_ALLOW_DESTRUCTIVE``).
- Tracked live Claude sessions: create, list, get, stop, kill, WS chat.
- Audit log of mutating requests to ``$AWM_DIR/access.log``.
- Error redaction: absolute paths are scrubbed from 4xx/5xx response bodies.

The local-only core (``awm/server.py``) is untouched and continues to serve
in-process IPC traffic on 127.0.0.1:7819.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

log = logging.getLogger("awm.exposed")

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
)
from starlette.responses import JSONResponse, Response

from awm import __version__, config
from awm.access_log import record as record_access
from awm.db import init_db
from awm.middleware_auth import require_bearer
from awm.middleware_gate import require_destructive
from awm.server import app as core_app
from awm.services import agent_instances, auth as auth_svc


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

def _write_discovery_file() -> Path:
    """Write ``$AWM_DIR/exposed.json`` so CLI/MCP can discover the live
    listener's scheme, host, port, and token-file path without guessing.

    Eliminates the silent config drift between launchers documented in
    inbox bugs #160/#161/#166. Also carries the current leadership view
    so CLI / MCP / operators can see who's ACTIVE without polling /status.
    """
    have_tls = config.TLS_CERT.exists() and config.TLS_KEY.exists()
    leadership_state = "ACTIVE"
    current_leader: str | None = None
    self_priority = 100
    try:
        from awm.services.leadership import state as _ldr_state
        s = _ldr_state.get_state()
        leadership_state = s.leadership
        current_leader = s.current_leader
        self_priority = s.self_priority
    except Exception:
        pass
    discovery = {
        "scheme": "https" if have_tls else "http",
        "host": config.EXPOSED_HOST,
        "port": config.EXPOSED_PORT,
        "token_file": str(config.AUTH_TOKEN_FILE),
        "pid": os.getpid(),
        "leadership_state": leadership_state,
        "current_leader": current_leader,
        "peer_priority": self_priority,
    }
    path = config.AWM_DIR / "exposed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(discovery, indent=2) + "\n")
    return path


def _sync_mcp_configs_on_startup() -> list[dict]:
    """Run every registered MCP-config exporter against the canonical
    ``<workspace>/.mcp.json``.

    Spawned Claudes are launched with ``--strict-mcp-config --mcp-config
    <spawn-mcp.json>``, so they ignore the normal ``.mcp.json`` walk-up.
    The claude-spawn exporter writes that file with the canonical MCP
    catalog plus a runtime-overridden ``awm`` entry pointing at *this*
    exposed server. The opencode exporter writes its own backend config
    so externally-launched opencode in this workspace sees the same
    catalog. See ``awm/exports/backends/`` for the registered exporters.

    Returns the per-exporter report (path, bytes, ok/error) for logging.
    """
    from awm.exports.mcp import sync_mcp_configs

    return sync_mcp_configs()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    info = auth_svc.bootstrap()
    print(
        f"[awm-exposed] auth ready: token={info['token_file']} "
        f"cert={info['tls_cert']} fp={info['tls_fingerprint'][:16]}…"
    )
    agent_instances.reconcile_on_startup()

    config.EXPOSED_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.EXPOSED_PID_FILE.write_text(str(os.getpid()))

    discovery_path = _write_discovery_file()
    print(f"[awm-exposed] discovery file: {discovery_path}")

    for entry in _sync_mcp_configs_on_startup():
        name = entry.get("name", "?")
        if entry.get("ok"):
            print(f"[awm-exposed] mcp-sync {name} → {entry.get('path')}")
        else:
            print(f"[awm-exposed] mcp-sync {name} FAILED: {entry.get('error')}")

    # Periodic sweep of expired one-shot login challenges.
    async def _challenges_sweeper() -> None:
        while True:
            try:
                await asyncio.sleep(30)
                auth_svc.sweep_challenges()
            except asyncio.CancelledError:
                break
            except Exception:
                continue

    challenges_sweeper_task = asyncio.create_task(_challenges_sweeper())

    # Discord bot — opt-in. Started iff $AWM_DIR/discord.toml is present
    # and parseable. Operators are whitelisted via the discord_operators
    # table (see `awm discord add-operator`). Lifecycle is now driven by
    # the leadership state machine: only the ACTIVE peer holds the gateway.
    try:
        from awm.services.discord import config as discord_config
        dc = discord_config.load()
    except Exception as exc:  # noqa: BLE001
        print(f"[awm-exposed] discord disabled: {exc}")
        dc = None
    from awm.services.leadership import discord as ldr_discord
    ldr_discord.set_config(dc)
    if dc is not None:
        print(
            f"[awm-exposed] discord bot configured "
            f"(account={dc.account_name or '?'}) — will connect when ACTIVE"
        )

    # cr-sqlite replication pull loop. Started unconditionally; the loop
    # itself no-ops when the extension binary is missing.
    from awm.services.replication import sync as repl_sync
    replication_task = asyncio.create_task(
        repl_sync.run_sync_loop(), name="replication-sync",
    )

    # Leadership state machine. Configure self from peers-table self-row;
    # register Discord start/stop callbacks; spin up the probe loop. Initial
    # state is STANDBY — the first poll round promotes if nobody outranks us.
    from awm.services.leadership import state as ldr_state
    from awm.services.leadership import poll as ldr_poll
    from awm.services.network import peers as _peer_svc
    self_ident = _peer_svc.get_local_identity()
    if self_ident:
        self_peer_id = self_ident["peer_id"]
        self_row = _peer_svc.get_peer(self_peer_id)
        self_priority = int((self_row or {}).get("peer_priority", 100))
        ldr_state.configure(self_peer_id, self_priority)
        ldr_state.on_promote(ldr_discord.start_bot)
        ldr_state.on_demote(ldr_discord.stop_bot)
        leadership_task = asyncio.create_task(
            ldr_poll.run_poll_loop(), name="leadership-poll"
        )
        print(
            f"[awm-exposed] leadership: self={self_peer_id} "
            f"priority={self_priority} (probe interval={ldr_poll.POLL_INTERVAL_S}s)"
        )
    else:
        # Pre-federation single-node operation: no peer identity, no poll
        # loop. Treat self as ACTIVE so the UI mounts unconditionally.
        ldr_state.configure("local", 0)
        await ldr_state.promote("local")
        leadership_task = None
        print("[awm-exposed] leadership: no peer identity — running ACTIVE single-node")

    # Warm STT model in the background — first connection waits a few
    # seconds rather than every connection paying full load cost. Failures
    # log but don't take down the listener.
    async def _warm_voice() -> None:
        loop = asyncio.get_running_loop()
        try:
            from awm.voice.stt import get_transcriber
            t0 = time.perf_counter()
            await loop.run_in_executor(None, get_transcriber()._ensure_loaded)
            print(f"[awm-exposed] STT model ready in "
                  f"{time.perf_counter() - t0:.1f}s")
        except Exception as exc:  # noqa: BLE001
            print(f"[awm-exposed] STT model unavailable: {exc}")

    warmup_task = asyncio.create_task(_warm_voice())

    try:
        from awm.voice.registry import get_registry
        get_registry().start_reaper()
    except Exception as exc:  # noqa: BLE001
        print(f"[awm-exposed] voice registry init failed: {exc}")

    yield

    challenges_sweeper_task.cancel()
    warmup_task.cancel()
    replication_task.cancel()
    if leadership_task is not None:
        leadership_task.cancel()
    try:
        await ldr_discord.stop_bot()
    except Exception:
        pass
    try:
        from awm.voice.registry import get_registry
        await get_registry().shutdown()
    except Exception:
        pass

    if config.EXPOSED_PID_FILE.exists():
        try:
            config.EXPOSED_PID_FILE.unlink()
        except OSError:
            pass

    discovery_path = config.AWM_DIR / "exposed.json"
    if discovery_path.exists():
        try:
            discovery_path.unlink()
        except OSError:
            pass

    try:
        from awm.services.network import ssh_tunnel
        ssh_tunnel.release_all_tunnels()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AWM (exposed)",
    version=__version__,
    lifespan=lifespan,
)


# Routes that mutate state and should be audited.
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Routes that are extra-destructive (filesystem creation/deletion). The gate
# dependency on each route already handles the 403; this set is just for the
# absence of an HTTP body match. We rely on per-route deps below.

_REDACT_RE = re.compile(r"/(?:home|root|var|tmp)/[^\s\"',:;)]+")


def _scrub(text: str) -> str:
    return _REDACT_RE.sub("<redacted>", text)


@app.middleware("http")
async def middleware(request: Request, call_next):
    """Auth (deferred to deps), audit log, and error-body redaction."""
    start = time.perf_counter()
    response: Response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    # Redact 4xx/5xx JSON bodies. Stream the response into memory only when
    # we have a reason to rewrite — the 2xx fast path passes through unchanged.
    if response.status_code >= 400 and isinstance(response, Response):
        ct = response.headers.get("content-type", "")
        if "application/json" in ct or "text/plain" in ct:
            # StreamingResponse-like: drain body_iterator
            body = b""
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                body += chunk
            scrubbed = _scrub(body.decode("utf-8", errors="replace")).encode("utf-8")
            response = Response(
                content=scrubbed,
                status_code=response.status_code,
                headers={
                    k: v for k, v in response.headers.items()
                    if k.lower() not in ("content-length",)
                },
                media_type=response.media_type,
            )

    # Audit-log mutating requests.
    if request.method in _AUDIT_METHODS:
        client_host = request.client.host if request.client else "?"
        record_access(
            ip=client_host,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            latency_ms=elapsed_ms,
            peer_id=getattr(request.state, "from_peer", None),
        )

    return response


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Generic error shape with path scrubbing."""
    detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": _scrub(detail)},
    )


# ---------------------------------------------------------------------------
# Rooms surface (M4). The legacy /agent-sessions/* routes are gone —
# sessions are no longer addressable directly; clients drive agents via
# rooms.
# ---------------------------------------------------------------------------

from awm.api.hub import router as hub_router  # noqa: E402
from awm.api.peer import router as peer_router  # noqa: E402
from awm.api.rooms import router as rooms_router  # noqa: E402
from awm.api.vagrant import router as vagrant_router  # noqa: E402
from awm.services.replication.endpoint import router as repl_router  # noqa: E402
from awm.voice.router import router as voice_router  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

app.include_router(rooms_router)
app.include_router(peer_router)
app.include_router(repl_router)
app.include_router(voice_router)
app.include_router(vagrant_router)
app.include_router(hub_router)


# ---------------------------------------------------------------------------
# Web UI auth — one-shot login flow (Discord DM or `awm login` CLI)
# ---------------------------------------------------------------------------

def _is_loopback(request: Request) -> bool:
    """Reject ``/auth/mint`` from anywhere but the loopback interface.

    The CLI and the in-process Discord bot both call /auth/mint over
    loopback, so we don't need a separate auth layer for it. Non-loopback
    callers (browser, federated peers) can not mint challenges — they go
    through the Discord bot UX or the CLI.

    ``testclient`` is Starlette TestClient's sentinel host string —
    accept it so unit tests can exercise this route directly. The
    sentinel is unreachable in production (no real socket binds to it).
    """
    if request.client is None:
        return False
    host = request.client.host
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


@app.post("/auth/mint")
async def auth_mint(request: Request, _auth: None = Depends(require_bearer)):
    """Mint a one-shot login challenge.

    Loopback-only. Body: ``{"awm_user": "<name>"}``. Returns
    ``{"nonce": "...", "url": "https://.../auth/bootstrap?ot=..."}``. The
    operator opens the URL in a browser; ``/auth/bootstrap`` consumes the
    nonce and sets the bearer as an HttpOnly cookie.
    """
    if not _is_loopback(request):
        raise HTTPException(403, "/auth/mint is loopback-only")
    try:
        body = await request.json()
    except Exception:
        body = {}
    awm_user = (body.get("awm_user") if isinstance(body, dict) else None) or "operator"
    awm_user = str(awm_user).strip() or "operator"

    nonce = auth_svc.mint_challenge(awm_user)
    url = f"{auth_svc.base_url()}/auth/bootstrap?ot={nonce}"
    return JSONResponse({
        "nonce": nonce,
        "url": url,
        "awm_user": awm_user,
        "expires_in_s": auth_svc.CHALLENGE_TTL_SECONDS,
    })


@app.get("/auth/bootstrap")
async def auth_bootstrap(request: Request):
    """Consume a one-shot nonce and set the bearer cookie.

    The bearer itself is stored verbatim in the HttpOnly ``awm_session``
    cookie — no server-side session state. A non-HttpOnly ``awm_as``
    cookie carries the operator's display name so the SPA can populate
    ``X-Awm-As``.
    """
    nonce = request.query_params.get("ot", "")
    awm_user = auth_svc.consume_challenge(nonce)
    if awm_user is None:
        raise HTTPException(401, "challenge expired or invalid — request a new login")

    try:
        bearer = auth_svc.local_token(generate_if_missing=False)
    except auth_svc.TokenMissing:
        raise HTTPException(500, "daemon token not initialized")

    response = Response(status_code=302, headers={"Location": "/ui/"})
    response.set_cookie(
        key=auth_svc.SESSION_COOKIE,
        value=bearer,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        key=auth_svc.AS_COOKIE,
        value=awm_user,
        httponly=False,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.get("/auth/whoami")
async def auth_whoami(request: Request, _auth: None = Depends(require_bearer)):
    """Return the operator display name (from ``awm_as`` cookie).

    Used by ``/ui/login.html`` to poll for completion after the operator
    clicks a DM'd bootstrap link in another tab.
    """
    awm_user = request.cookies.get(auth_svc.AS_COOKIE) or "operator"
    return JSONResponse({"awm_user": awm_user})


@app.delete("/auth/session")
async def auth_logout(_request: Request):
    """Clear the bearer cookie. Operator must re-login via Discord or CLI."""
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth_svc.SESSION_COOKIE, path="/")
    response.delete_cookie(auth_svc.AS_COOKIE, path="/")
    return response

_STATIC_DIR = _Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_STATIC_DIR), html=True), name="ui")


# ---------------------------------------------------------------------------
# Gate destructive routes on the core app
# ---------------------------------------------------------------------------
# The core app's destructive routes are reached through the mount at "/".
# Gating happens by overlaying a deny route on the exposed app before the
# mount for each path, since the more-specific match wins.

def _deny(req: Request, dep_destructive: None = Depends(require_destructive),
          dep_auth: None = Depends(require_bearer)) -> None:
    """Used as a route handler — only reached if auth + gate pass.
    If we get here, fall through to the mounted core app by re-dispatching.
    This is awkward in FastAPI; instead we precheck and then forward.
    """
    raise HTTPException(500, "gate handler reached without forward")


# ---------------------------------------------------------------------------
# Auth + gating for the core app routes (reached via mount)
# ---------------------------------------------------------------------------
# We can't add per-route dependencies to a mounted app, so we enforce
# auth and gating via a middleware that runs before the mount dispatch.

_DESTRUCTIVE_ROUTES = [
    ("DELETE", re.compile(r"^/scopes/[^/]+/[^/]+$")),
    ("POST", re.compile(r"^/projects$")),
]


def _is_destructive(method: str, path: str) -> bool:
    for m, pat in _DESTRUCTIVE_ROUTES:
        if m == method and pat.match(path):
            return True
    return False


_STANDBY_GATED_PREFIXES = ("/ui/", "/ui")
_STANDBY_GATED_PATHS = ("/auth/mint", "/auth/bootstrap")


def _is_standby_gated(path: str) -> bool:
    """Routes that should 503+Location when the local peer is STANDBY:
    the SPA itself and the login-flow entry points. ``/auth/whoami`` and
    ``/auth/session`` stay reachable so an already-cookied browser can
    finish its current request and clear state cleanly."""
    if path in _STANDBY_GATED_PATHS:
        return True
    if path == "/ui" or path.startswith("/ui/"):
        return True
    return False


@app.middleware("http")
async def gate_and_auth_for_core(request: Request, call_next):
    """Enforce auth + destructive gate for routes that fall through to the
    mounted core app. Routes defined directly on the exposed app already use
    Depends(require_bearer) per-route; this middleware is for everything
    else (proxied through the mount).

    Also handles peer-aware identity: if ``X-Awm-From`` is set to a known
    peer-id, ``request.state.from_peer`` is populated so /inbox can prefix
    the stored sender. Unknown peer-ids are 4xx'd rather than silently
    accepted, to avoid spoofing of un-registered origins.

    Leadership gate: when the local peer is STANDBY and the request targets
    the UI or the login-flow entry, redirect (or 503-without-Location if no
    leader is known yet) so operators land on the ACTIVE peer.
    """
    path = request.url.path

    # Leadership gate runs first — STANDBY peers must not serve UI even if
    # the caller has a valid cookie. /peer/*, /status, and the rest of the
    # federation surface stay open in STANDBY.
    if _is_standby_gated(path):
        try:
            from awm.services.leadership import state as _ldr_state
            s = _ldr_state.get_state()
            if s.leadership == "STANDBY":
                leader = _ldr_state.current_leader_base_url()
                if leader:
                    return Response(
                        status_code=503,
                        headers={"Location": f"{leader}{path}"},
                        content=b"standby; redirecting to active leader",
                    )
                return Response(
                    status_code=503,
                    content=b"standby; no active leader known yet",
                )
        except Exception:
            # Leadership service not configured — fall through (single-node).
            pass

    # Routes mounted directly on the exposed app (with their own
    # Depends(require_bearer)) skip middleware-level auth to avoid
    # double work. /ui and /auth/exchange are intentionally
    # unauthenticated at the middleware level: /ui is static HTML;
    # /auth/exchange validates its own bearer to mint a session cookie.
    if (path.startswith("/rooms") or path.startswith("/ui")
            or path.startswith("/voice")
            or path.startswith("/peer/")
            or path.startswith("/auth/")):
        return await call_next(request)

    # Auth check — local-token bearer, peer-token bearer, or the
    # awm_session cookie (which carries the bearer verbatim).
    auth = request.headers.get("authorization", "")
    token: str | None = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.cookies.get(auth_svc.SESSION_COOKIE)
    if not auth_svc.verify_bearer(token):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})

    # Peer-origin identification. We trust the bearer; X-Awm-From is the
    # origin claim used for sender-prefixing and audit. If the peer-id is
    # not in our registry, refuse — spoofing of an unknown origin shouldn't
    # silently succeed.
    from_peer_header = request.headers.get("x-awm-from")
    if from_peer_header:
        from awm.services.network import peers as _peers
        if _peers.get_peer(from_peer_header) is None:
            return JSONResponse(
                status_code=400,
                content={"detail": f"unknown peer in X-Awm-From: {from_peer_header}"},
            )
        request.state.from_peer = from_peer_header

    # Destructive gate
    if _is_destructive(request.method, path):
        if os.environ.get("AWM_ALLOW_DESTRUCTIVE") != "1":
            return JSONResponse(
                status_code=403,
                content={"detail": "destructive operations disabled"},
            )

    return await call_next(request)


# ---------------------------------------------------------------------------
# Mount the existing AWM REST surface
# ---------------------------------------------------------------------------
# Mounted apps' lifespans do NOT run (Starlette behavior), so the core's
# PID-file/idle-shutdown logic is correctly inactive when reached via this
# exposed listener. Our own lifespan handles DB init + PID file.

app.mount("/", core_app)


# ---------------------------------------------------------------------------
# Hub forwarding middleware (S4/S5 of infra-service-hub)
# ---------------------------------------------------------------------------
# Sits OUTERMOST: empty registry → byte-identical pass-through. On a
# prefix match, the request never reaches the in-process routers; the
# hub forwards it to the registered service URL with hub-as-peer auth.
# Raw ASGI so both HTTP and WebSocket scopes are handled.

from awm.services.hub.proxy import proxy_http, proxy_ws  # noqa: E402
from awm.services.hub.registry import get_registry as _get_hub_registry  # noqa: E402
from awm.services.hub.static import (  # noqa: E402
    close_ws_unsupported as _ws_close_unsupported,
    serve_static as _serve_static,
)


class HubRoutingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        registry = _get_hub_registry()
        if registry.is_empty():
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        # Never forward the hub control plane itself, even if a registered
        # prefix would shadow it. Defensive — registration validation
        # could also reject these prefixes, but cheap to enforce here too.
        if path == "/hub" or path.startswith("/hub/"):
            return await self.app(scope, receive, send)
        rec = registry.longest_match(path)
        if rec is None:
            return await self.app(scope, receive, send)
        if rec.kind == "static":
            if scope["type"] == "websocket":
                await _ws_close_unsupported(scope, receive, send)
                return
            request = Request(scope, receive=receive)
            response = await _serve_static(request, rec)
            await response(scope, receive, send)
            return
        if scope["type"] == "http":
            request = Request(scope, receive=receive)
            response = await proxy_http(request, rec.url)
            await response(scope, receive, send)
        else:
            from fastapi import WebSocket as _WS
            ws = _WS(scope, receive=receive, send=send)
            await proxy_ws(ws, rec.url)


app.add_middleware(HubRoutingMiddleware)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_exposed_server() -> None:
    """Start the HTTPS listener. TLS is mandatory and auto-bootstrapped."""
    config.EXPOSED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cert, key = auth_svc.bootstrap_tls(generate_if_missing=True)

    import ssl
    kwargs: dict = {
        "host": config.EXPOSED_HOST,
        "port": config.EXPOSED_PORT,
        "log_level": "info",
        "ssl_certfile": str(cert),
        "ssl_keyfile": str(key),
        "ssl_version": ssl.PROTOCOL_TLS_SERVER,
    }
    uvicorn.run(app, **kwargs)
