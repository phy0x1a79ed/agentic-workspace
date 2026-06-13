"""FastAPI app + uvicorn with lifespan management.

This is the gateway's HTTP surface. It exposes only what the gateway owns
itself — the daemon lifecycle (`/status`, `/restart`), the generic tool
dispatch the MCP proxy rides (`/tools`, `/invoke`, both backed by the live
`catalog`), and the hub control plane + routing middleware. Feature surfaces
(scopes, rooms, artifacts, …) are NOT baked in here — they arrive as services
register into the catalog/hub. See `catalog.py` for the registration contract.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request

from awm.config import (
    HOST,
    PORT,
    PID_FILE,
    LOG_FILE,
    WORKSPACE_ROOT,
    IDLE_SHUTDOWN_SECONDS,
)
from awm.persistence.db import init_db
from awm.gateway import catalog
from awm.gateway.core import restart_core

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# Idle shutdown state
# ---------------------------------------------------------------------------

_last_request_time: float = 0.0
_shutdown_event: asyncio.Event | None = None


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _idle_shutdown_loop():
    """Shut down the server after a period of inactivity."""
    global _shutdown_event
    _shutdown_event = asyncio.Event()
    while not _shutdown_event.is_set():
        await asyncio.sleep(30)
        if IDLE_SHUTDOWN_SECONDS <= 0:
            continue
        elapsed = time.time() - _last_request_time
        if elapsed > IDLE_SHUTDOWN_SECONDS:
            print(f"[idle] No requests for {int(elapsed)}s — shutting down")
            _shutdown_event.set()
            # Force exit since uvicorn doesn't have a clean programmatic shutdown
            import os
            os._exit(0)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _last_request_time
    _last_request_time = time.time()

    # Record core start time for awm_status uptime reporting
    catalog.mark_core_start()

    # Init DB
    init_db()

    # Write PID file
    import os
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    # Start background tasks
    idle_task = asyncio.create_task(_idle_shutdown_loop())

    # Reconcile journaled hub services: give each a 10s window to reopen its
    # control WS, then respawn silent ones from start_cmd. Fired as a
    # background task — the hub is available immediately for services that
    # ARE actively reconnecting.
    try:
        from awm.gateway.hub.supervisor import reconcile_journaled_services
        asyncio.create_task(reconcile_journaled_services())
    except Exception as exc:  # noqa: BLE001
        print(f"[awm] service reconcile skipped: {exc}")

    # Fan canonical .mcp.json out to backend-specific configs.
    try:
        from awm.gateway.exports.mcp import sync_mcp_configs
        for entry in sync_mcp_configs():
            name = entry.get("name", "?")
            if entry.get("ok"):
                print(f"[awm] mcp-sync {name} → {entry.get('path')}")
            else:
                print(f"[awm] mcp-sync {name} FAILED: {entry.get('error')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[awm] mcp-sync skipped: {exc}")

    yield

    # Cleanup
    idle_task.cancel()
    if PID_FILE.exists():
        PID_FILE.unlink()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="AWM", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def track_activity(request, call_next):
    global _last_request_time
    _last_request_time = time.time()
    return await call_next(request)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.get("/status")
def get_status():
    """Gateway-native status. ``active_scopes`` is 0 until a scopes service
    registers — kept in the shape so ``_process_utils.probe_existing_awm``
    (which validates ``status == "ok"``) classifies the daemon correctly."""
    return {
        "status": "ok",
        "workspace_root": str(WORKSPACE_ROOT),
        "active_scopes": 0,
    }


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

@app.post("/restart")
def restart_core_endpoint():
    """Restart the AWM core via systemd.

    Returns immediately; the actual restart happens asynchronously.
    MCP clients reconnect transparently on the next tool call.
    """
    try:
        return restart_core()
    except RuntimeError as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Generic tool dispatch (used by the thin MCP stdio proxy)
# ---------------------------------------------------------------------------

@app.get("/tools")
def list_tools_endpoint():
    """Return the current MCP tool definitions from the live catalog.

    The thin stdio proxy fetches this on every `list_tools` call instead of
    caching at its own startup. That keeps the proxy stateless: tools that
    appear/vanish as services register show up immediately, with no Claude
    Code restart. Sync over a GIL-safe registry snapshot — see catalog.py.
    """
    return {"tools": [t.model_dump(by_alias=True) for t in catalog.list_tools()]}


@app.post("/invoke")
async def invoke_tool(payload: dict, request: Request):
    """Dispatch an MCP-style tool call by name through the catalog. Async so
    service ops can be awaited over their control WS on the server loop (no
    second event loop — see catalog.py concurrency note). The MCP proxy
    forwards here over HTTP so the core can restart without tearing down the
    stdio pipe Claude Code has open."""
    name = payload.get("name")
    args = payload.get("args", {}) or {}
    if not name:
        raise HTTPException(400, "missing 'name' in payload")
    as_ = request.headers.get("X-Awm-As")
    try:
        result = await catalog.dispatch(name, args, as_=as_)
    except ValueError as e:
        # Unknown tool name -> 404
        raise HTTPException(404, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    except Exception as e:  # noqa: BLE001 — surface anything else with class+message
        # Inbox #232: bare {"error": "Internal Server Error"} hid the real
        # failure from MCP callers. Log the traceback server-side and put a
        # structured {error_class, error} in the response detail so the
        # caller can branch on the exception class.
        import traceback as _tb
        print(
            f"[awm] /invoke {name} failed with {type(e).__name__}: {e}\n"
            f"{_tb.format_exc()}",
            flush=True,
        )
        raise HTTPException(
            status_code=500,
            detail={"error_class": type(e).__name__, "error": str(e), "tool": name},
        )
    return {"result": result}


# ---------------------------------------------------------------------------
# Hub control plane (/hub/*)
# ---------------------------------------------------------------------------

from awm.gateway.api.hub import router as hub_router  # noqa: E402

app.include_router(hub_router)


# ---------------------------------------------------------------------------
# Hub forwarding middleware (outermost — empty-registry pass-through is
# byte-identical to a hub-less awm). Routes /ui/<page>, /svc/<name>/...,
# and any URL/static prefix that's been registered. Raw ASGI so HTTP and
# WebSocket scopes are both handled.
# ---------------------------------------------------------------------------

from awm.gateway.hub.proxy import proxy_http, proxy_ws  # noqa: E402
from awm.gateway.hub.registry import get_registry as _get_hub_registry  # noqa: E402
from awm.gateway.hub.static import (  # noqa: E402
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
        if path == "/hub" or path.startswith("/hub/"):
            return await self.app(scope, receive, send)
        rec = registry.longest_match(path)
        if rec is None:
            return await self.app(scope, receive, send)
        if rec.kind in ("static", "page"):
            if scope["type"] == "websocket":
                await _ws_close_unsupported(scope, receive, send)
                return
            request = Request(scope, receive=receive)
            response = await _serve_static(request, rec)
            await response(scope, receive, send)
            return
        if rec.kind == "service":
            await self._dispatch_service(scope, receive, send, rec, path)
            return
        if scope["type"] == "http":
            request = Request(scope, receive=receive)
            response = await proxy_http(request, rec.url)
            await response(scope, receive, send)
        else:
            from fastapi import WebSocket as _WS
            ws = _WS(scope, receive=receive, send=send)
            await proxy_ws(ws, rec.url)

    async def _dispatch_service(self, scope, receive, send, rec, path):
        """RPC-WS service routing (kind="service"):

        - POST <prefix>/fn/<name>            -> control-WS call/notify
        - POST <prefix>/session/<kind>       -> open session, return ws_path
        - WS   <prefix>/session/<id>         -> session WS (direct = byte
                                                relay, otherwise enveloped)
        - WS   <prefix>/emit/<topic>         -> emit subscriber WS
        Everything else under the prefix is 404.
        """
        from awm.gateway.hub.proxy import (
            open_session_via_http,
            proxy_service_emit_ws,
            proxy_service_http,
            proxy_session_ws,
        )
        from fastapi import WebSocket as _WS
        from starlette.responses import PlainTextResponse

        rel = path[len(rec.prefix):]
        as_ = None

        if scope["type"] == "http":
            request = Request(scope, receive=receive)
            if rel.startswith("/fn/"):
                response = await proxy_service_http(
                    request, rec.service_id, as_=as_,
                )
            elif rel.startswith("/session/") and request.method == "POST":
                response = await open_session_via_http(
                    request, rec.service_id, as_=as_,
                )
            else:
                response = PlainTextResponse("not found", status_code=404)
            await response(scope, receive, send)
            return

        ws = _WS(scope, receive=receive, send=send)
        if rel.startswith("/session/"):
            sid = rel[len("/session/"):]
            await proxy_session_ws(ws, rec.service_id, sid)
            return
        if rel.startswith("/emit/"):
            topic = rel[len("/emit/"):]
            await proxy_service_emit_ws(ws, rec.service_id, topic, as_=as_)
            return
        await _ws_close_unsupported(scope, receive, send)


app.add_middleware(HubRoutingMiddleware)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_server(foreground: bool = True):
    """Start the uvicorn server."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Per-workspace env file: merge $AWM_WORKSPACE/.awm/env into os.environ
    # before the probe and before any subprocess we spawn (notably git
    # clone over SSH from project_create — inbox #236).
    from awm.config import load_env_file
    load_env_file()
    # Pre-bind probe: if something is already on (HOST, PORT), figure out
    # whether it's a healthy awm against the same workspace (→ exit 0) or
    # a foreign holder (→ exit 1 with a diagnostic). Eliminates the silent
    # EADDRINUSE restart-loop pattern (inbox #232).
    from awm.gateway._process_utils import exit_if_healthy_peer
    exit_if_healthy_peer(HOST, PORT, str(WORKSPACE_ROOT))
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
    )
