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
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

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

    # Periodic sweep of expired web-UI session cookies.
    async def _session_sweeper() -> None:
        while True:
            try:
                await asyncio.sleep(60)
                auth_svc.sweep_expired()
            except asyncio.CancelledError:
                break
            except Exception:
                continue

    session_sweeper_task = asyncio.create_task(_session_sweeper())

    # Warm voice models in the background — first connection waits a few
    # seconds rather than every connection paying full load cost. Failures
    # log but don't take down the listener.
    async def _warm_voice() -> None:
        loop = asyncio.get_running_loop()
        try:
            from awm.voice.stt import get_transcriber
            from awm.voice.tts import get_synthesizer
            t0 = time.perf_counter()
            await loop.run_in_executor(None, get_transcriber()._ensure_loaded)
            await loop.run_in_executor(None, get_synthesizer()._ensure_loaded)
            print(f"[awm-exposed] voice models ready in "
                  f"{time.perf_counter() - t0:.1f}s")
        except Exception as exc:  # noqa: BLE001
            print(f"[awm-exposed] voice models unavailable: {exc}")

    warmup_task = asyncio.create_task(_warm_voice())

    try:
        from awm.voice.registry import get_registry
        get_registry().start_reaper()
    except Exception as exc:  # noqa: BLE001
        print(f"[awm-exposed] voice registry init failed: {exc}")

    yield

    session_sweeper_task.cancel()
    warmup_task.cancel()
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

from awm.api.peer import router as peer_router  # noqa: E402
from awm.api.rooms import router as rooms_router  # noqa: E402
from awm.voice.router import router as voice_router  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

app.include_router(rooms_router)
app.include_router(peer_router)
app.include_router(voice_router)


# ---------------------------------------------------------------------------
# Web UI auth bootstrap — bearer-in-URL-hash → session cookie
# ---------------------------------------------------------------------------

@app.post("/auth/exchange")
async def auth_exchange(request: Request):
    """Trade a one-shot bearer for a session cookie.

    The web UI loads with ``#token=<bearer>`` in the URL hash, POSTs that
    bearer here, then clears the hash via ``history.replaceState`` before
    any other code runs. Subsequent requests / WS handshakes carry the
    cookie automatically.
    """
    auth = request.headers.get("authorization", "")
    token: str | None = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        try:
            body = await request.json()
            token = (body.get("token") or "").strip() if isinstance(body, dict) else None
        except Exception:
            token = None
    identity = auth_svc.verify_bearer(token) if token else None
    if identity is None or identity.kind != "local":
        # Only minted from the local-daemon bearer. Session-issued tokens
        # can not bootstrap further sessions.
        raise HTTPException(401, "unauthorized")

    sid = auth_svc.mint_session(identity)
    response = JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "expires_in_s": auth_svc.SESSION_TTL_SECONDS,
        },
    )
    response.set_cookie(
        key=auth_svc.SESSION_COOKIE,
        value=sid,
        max_age=auth_svc.SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.delete("/auth/session")
async def auth_logout(request: Request):
    """Drop the caller's web-UI session and clear the cookie."""
    sid = request.cookies.get(auth_svc.SESSION_COOKIE)
    dropped = auth_svc.drop_session(sid) if sid else False
    response = JSONResponse(status_code=200, content={"dropped": dropped})
    response.delete_cookie(auth_svc.SESSION_COOKIE, path="/")
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
    """
    path = request.url.path

    # Routes mounted directly on the exposed app (with their own
    # Depends(require_bearer)) skip middleware-level auth to avoid
    # double work. /ui and /auth/exchange are intentionally
    # unauthenticated at the middleware level: /ui is static HTML;
    # /auth/exchange validates its own bearer to mint a session cookie.
    if (path.startswith("/rooms") or path.startswith("/ui")
            or path.startswith("/voice")
            or path.startswith("/auth/")):
        return await call_next(request)

    # Auth check — local-token bearer or live web-UI session cookie.
    auth = request.headers.get("authorization", "")
    token: str | None = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.cookies.get(auth_svc.SESSION_COOKIE)
    identity = auth_svc.verify_bearer(token) if token else None
    if identity is None:
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
