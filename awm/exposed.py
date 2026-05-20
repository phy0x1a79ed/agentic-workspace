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
    Query,
    Request,
    WebSocket,
)
from starlette.responses import JSONResponse, Response

from awm import __version__, config
from awm.access_log import record as record_access
from awm.db import init_db
from awm.middleware_auth import authenticate_websocket, require_bearer
from awm.middleware_gate import require_destructive
from awm.models import (
    AgentSessionActionResponse,
    AgentSessionCreateRequest,
    AgentSessionInfo,
    AgentSessionListResponse,
)
from awm.server import app as core_app
from awm.services import sessions_live


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    sessions_live.reconcile_on_startup()

    config.EXPOSED_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.EXPOSED_PID_FILE.write_text(str(os.getpid()))

    yield

    if config.EXPOSED_PID_FILE.exists():
        try:
            config.EXPOSED_PID_FILE.unlink()
        except OSError:
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
# Live-session routes (NEW — not in core_app)
# ---------------------------------------------------------------------------

@app.post(
    "/agent-sessions",
    response_model=AgentSessionInfo,
    dependencies=[Depends(require_bearer)],
)
async def create_agent_session(req: AgentSessionCreateRequest):
    try:
        return await sessions_live.create_session(req)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.get(
    "/agent-sessions",
    response_model=AgentSessionListResponse,
    dependencies=[Depends(require_bearer)],
)
def list_agent_sessions(
    project: str | None = Query(None),
    scope: str | None = Query(None),
    status: str | None = Query(None),
):
    items = sessions_live.list_sessions(project=project, scope=scope, status=status)
    return AgentSessionListResponse(sessions=items, total=len(items))


@app.get(
    "/agent-sessions/{session_id}",
    response_model=AgentSessionInfo,
    dependencies=[Depends(require_bearer)],
)
def get_agent_session(session_id: int):
    info = sessions_live.get_session(session_id)
    if info is None:
        raise HTTPException(404, "session not found")
    return info


@app.post(
    "/agent-sessions/{session_id}/stop",
    response_model=AgentSessionActionResponse,
    dependencies=[Depends(require_bearer)],
)
async def stop_agent_session(session_id: int):
    try:
        info = await sessions_live.stop_session(session_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return AgentSessionActionResponse(
        id=info.id, status=info.status, message="SIGTERM sent"
    )


@app.post(
    "/agent-sessions/{session_id}/kill",
    response_model=AgentSessionActionResponse,
    dependencies=[Depends(require_bearer)],
)
async def kill_agent_session(session_id: int):
    try:
        info = await sessions_live.kill_session(session_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return AgentSessionActionResponse(
        id=info.id, status=info.status, message="SIGKILL sent"
    )


@app.get(
    "/agent-sessions/{session_id}/log",
    dependencies=[Depends(require_bearer)],
)
def get_agent_session_log(session_id: int, lines: int = Query(200, ge=1, le=5000)):
    try:
        text = sessions_live.tail_log(session_id, lines=lines)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"log": text}


@app.websocket("/agent-sessions/{session_id}/chat")
async def chat_agent_session(websocket: WebSocket, session_id: int):
    """Bidirectional stream-json chat with a live session.

    Auth: ``Sec-WebSocket-Protocol: bearer.<token>`` (preferred) or
    ``?token=<token>`` query string.
    """
    subprotocol = await authenticate_websocket(websocket)
    # If auth failed, authenticate_websocket already closed the socket.
    if websocket.client_state.name == "DISCONNECTED":
        return

    await websocket.accept(subprotocol=subprotocol)

    try:
        await sessions_live.attach_ws(session_id, websocket)
    except FileNotFoundError:
        await websocket.close(code=1008, reason="session not found")
    except sessions_live.AttachError as exc:
        await websocket.close(code=1008, reason=str(exc))
    except Exception:
        # WebSocket disconnect — receive_text raises. attach_ws cleans up.
        pass


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

    # The exposed app's own /agent-sessions routes already use Depends auth.
    if path.startswith("/agent-sessions"):
        return await call_next(request)

    # Auth check
    auth = request.headers.get("authorization", "")
    token: str | None = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    from awm.middleware_auth import load_token
    import hmac as _hmac
    expected = load_token()
    if not expected or not token or not _hmac.compare_digest(token, expected):
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
    """Start the network-exposed uvicorn listener with optional TLS."""
    config.EXPOSED_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    kwargs: dict = {
        "host": config.EXPOSED_HOST,
        "port": config.EXPOSED_PORT,
        "log_level": "info",
    }
    if config.TLS_CERT and config.TLS_KEY:
        kwargs["ssl_certfile"] = config.TLS_CERT
        kwargs["ssl_keyfile"] = config.TLS_KEY
        # Minimum TLS 1.2 per objectives.md hygiene.
        import ssl
        kwargs["ssl_version"] = ssl.PROTOCOL_TLS_SERVER
    else:
        print(
            "[awm-exposed] WARNING: no TLS cert configured "
            "(set AWM_TLS_CERT and AWM_TLS_KEY). Starting plaintext."
        )

    uvicorn.run(app, **kwargs)
