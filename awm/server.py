"""FastAPI app + uvicorn with lifespan management."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from starlette.responses import JSONResponse, Response

from awm import __version__
from awm.config import (
    HOST,
    PORT,
    PID_FILE,
    LOG_FILE,
    WORKSPACE_ROOT,
    IDLE_SHUTDOWN_SECONDS,
)
from awm.db import init_db, get_connection
from awm.models import (
    StatusResponse,
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectListInfo,
    ProjectListResponse,
    ProjectScopeCounts,
    ScopeCreateRequest,
    ScopeUpdateRequest,
    ScopeSyncRequest,
    ScopeListResponse,
    ScopeActionResponse,
    SkillListResponse,
    SkillContentResponse,
)
from awm.operations.sessions import SESSION_OPERATIONS
from awm._lib.operations import register_fastapi_routes
from awm.services import core, projects, scopes, skills
from awm._lib.tool_dispatch import TOOL_DEFINITIONS, handle_tool, mark_core_start

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
    mark_core_start()

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
        from awm.services.hub.supervisor import reconcile_journaled_services
        asyncio.create_task(reconcile_journaled_services())
    except Exception as exc:  # noqa: BLE001
        print(f"[awm] service reconcile skipped: {exc}")

    # Bootstrap the unified vagrant-scopes bare repo. Idempotent — cheap
    # no-op when present. Non-fatal: /vagrant/* 503s with a clear message if
    # disabled.
    try:
        from awm.services.scopes import ensure_vagrant_repo
        bare = ensure_vagrant_repo()
        print(f"[awm] vagrant-scopes ready: {bare}")
    except Exception as exc:  # noqa: BLE001
        print(f"[awm] vagrant-scopes disabled: {exc}")

    # Fan canonical .mcp.json out to backend-specific configs.
    try:
        from awm.exports.mcp import sync_mcp_configs
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

@app.get("/status", response_model=StatusResponse)
def get_status():
    scope_result = scopes.search_scopes(status="active", limit=10_000)

    return StatusResponse(
        workspace_root=str(WORKSPACE_ROOT),
        active_scopes=scope_result.total,
    )


@app.get("/projects/search", response_model=ProjectListResponse)
def search_projects_endpoint(
    query: str | None = None,
    active_only: bool = False,
    limit: int = 50,
    offset: int = 0,
):
    """Search projects (hybrid keyword + semantic). v37: counts are derived
    from the agents table (``allocated|active`` → active, ``retired`` →
    completed)."""
    from awm.services import projects as projects_svc
    return projects_svc.search_projects(
        query=query, active_only=active_only, limit=limit, offset=offset,
    )


# ---------------------------------------------------------------------------
# Inbox (federated entry point — remote peers POST here)
# ---------------------------------------------------------------------------

@app.post("/inbox")
def post_inbox(request: Request, payload: dict):
    """User-facing inbox-send. Stores a message with the sender from the
    request body verbatim — federated cross-peer sends go through
    ``POST /peer/inbox`` instead (which prefixes the origin peer id)."""
    from awm.models import MessageSendRequest
    from awm.services import messaging

    try:
        req = MessageSendRequest(**payload)
    except Exception as exc:
        raise HTTPException(400, f"invalid message body: {exc}")
    try:
        return messaging.send_message(req)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/messages")
def get_messages_search(
    scope: str | None = None,
    status: str | None = None,
    msg_type: str | None = None,
    query: str | None = None,
    limit: int = 50,
):
    from awm.services import messaging
    try:
        return messaging.search_messages(
            scope=scope, status=status, msg_type=msg_type, query=query, limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/messages/recipients")
def get_messages_recipients(query: str, peer: str | None = None):
    from awm.services import messaging
    try:
        recipients = messaging.list_recipients(query=query, peer=peer)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except NotImplementedError as exc:
        raise HTTPException(501, str(exc))
    return {"recipients": recipients, "total": len(recipients), "query": query}


@app.get("/messages/fetch")
def get_messages_fetch(
    scope: str,
    status: str | None = None,
    msg_type: str | None = None,
    limit: int = 50,
    mark_read: bool = False,
):
    from awm.services import messaging
    try:
        return messaging.fetch_messages(
            scope=scope, status=status, msg_type=msg_type,
            limit=limit, mark_read=mark_read,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


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
        return core.restart_core()
    except RuntimeError as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@app.post("/projects", response_model=ProjectCreateResponse)
def create_project(req: ProjectCreateRequest):
    try:
        return projects.create_project(req)
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Scopes (formerly Tasks)
# ---------------------------------------------------------------------------

@app.get("/scopes/search", response_model=ScopeListResponse)
def search_scopes_endpoint(
    query: str | None = Query(None),
    status: str = Query("active"),
    project: str | None = Query(None),
    limit: int = Query(50, ge=1, le=10000),
    offset: int = Query(0, ge=0),
):
    """Search scopes (hybrid keyword + semantic). Defaults to status='active'."""
    return scopes.search_scopes(
        query=query, status=status, project=project,
        limit=limit, offset=offset,
    )


@app.post("/scopes", response_model=ScopeActionResponse)
def create_scope(req: ScopeCreateRequest):
    try:
        return scopes.create_scope(req)
    except (FileNotFoundError, FileExistsError) as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.patch("/scopes/{project}/{scope}", response_model=ScopeActionResponse)
def update_scope(project: str, scope: str, req: ScopeUpdateRequest):
    try:
        return scopes.update_scope(project, scope, req)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.delete("/scopes/{project}/{scope}", response_model=ScopeActionResponse)
def delete_scope(project: str, scope: str):
    try:
        return scopes.delete_scope(project, scope)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.post("/scopes/{project}/{scope}/sync", response_model=ScopeActionResponse)
def sync_scope_endpoint(project: str, scope: str, req: ScopeSyncRequest):
    try:
        return scopes.sync_scope(project, scope, req)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@app.get("/skills/search", response_model=SkillListResponse)
def search_skills_endpoint(
    query: str | None = Query(None),
    type: str | None = Query(None),
    tags: str | None = Query(None, description="Comma-separated tags"),
):
    """Search skills (hybrid keyword + semantic). Omit `query` to filter
    only by `type` / `tags`."""
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    return skills.search_skills(query=query, type_filter=type, tags=tag_list)


@app.get("/skills/{path:path}", response_model=SkillContentResponse)
def get_skill_endpoint(path: str):
    try:
        return skills.get_skill(path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


# ---------------------------------------------------------------------------
# Sessions — registered via registry
# ---------------------------------------------------------------------------

register_fastapi_routes(app, SESSION_OPERATIONS)


# ---------------------------------------------------------------------------
# Artifact content (phase 6 — federates when origin_peer != self)
# ---------------------------------------------------------------------------

@app.get("/artifacts/{artifact_ref}/content")
def get_artifact_content(artifact_ref: str):
    """Return the bytes of an artifact. ``artifact_ref`` is the public
    legacy_id (``42``) or a peer-scoped ref (``42@capella``). Remote-
    origin rows transparently proxy to the owning peer via /peer/."""
    from fastapi.responses import Response as _Response
    from awm.services import artifacts

    # parse_id_ref also accepts plain ints; the path arg arrives as str.
    try:
        # Try int first for compactness; falls back to str grammar.
        ref: int | str = int(artifact_ref)
    except ValueError:
        ref = artifact_ref
    try:
        data = artifacts.get_content(ref)
    except artifacts.ArtifactNotFound as exc:
        raise HTTPException(404, str(exc))
    except artifacts.ArtifactContentUnavailable as exc:
        raise HTTPException(502, str(exc))
    return _Response(content=data, media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# Generic tool dispatch (used by the thin MCP stdio proxy)
# ---------------------------------------------------------------------------

@app.get("/tools")
def list_tools_endpoint():
    """Return the current MCP tool definitions.

    The thin stdio proxy fetches this on every `list_tools` call instead of
    importing `TOOL_DEFINITIONS` at its own process startup. That keeps the
    proxy stateless: tools added/removed in the core show up immediately
    after a core restart without needing to restart Claude Code.
    """
    return {"tools": [t.model_dump(by_alias=True) for t in TOOL_DEFINITIONS]}


@app.post("/invoke")
def invoke_tool(payload: dict):
    """Dispatch an MCP-style tool call by name. The MCP proxy forwards here
    over HTTP so the core can be restarted without tearing down the stdio
    pipe Claude Code has open."""
    name = payload.get("name")
    args = payload.get("args", {}) or {}
    if not name:
        raise HTTPException(400, "missing 'name' in payload")
    try:
        result = handle_tool(name, args)
    except ValueError as e:
        # Unknown tool name -> 404
        raise HTTPException(404, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"result": result}


# ---------------------------------------------------------------------------
# Hub control plane (/hub/*) and rooms surface (/rooms/*)
# ---------------------------------------------------------------------------

from awm.api.hub import router as hub_router  # noqa: E402
from awm.api.rooms import router as rooms_router  # noqa: E402
from awm.api.vagrant import router as vagrant_router  # noqa: E402

app.include_router(hub_router)
app.include_router(rooms_router)
app.include_router(vagrant_router)


# ---------------------------------------------------------------------------
# Hub forwarding middleware (outermost — empty-registry pass-through is
# byte-identical to a hub-less awm). Routes /ui/<page>, /svc/<name>/...,
# and any URL/static prefix that's been registered. Raw ASGI so HTTP and
# WebSocket scopes are both handled.
# ---------------------------------------------------------------------------

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
        from awm.services.hub.proxy import (
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
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
    )
