"""FastAPI app + uvicorn with lifespan management."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request

from awm import __version__
from awm.config import (
    HOST,
    PORT,
    PID_FILE,
    LOG_FILE,
    WORKSPACE_ROOT,
    IDLE_SHUTDOWN_SECONDS,
    REAPER_INTERVAL,
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
    LockAcquireRequest,
    LockListResponse,
    LockActionResponse,
    SharedEditRequest,
    SharedEditListResponse,
    SharedEditActionResponse,
    SkillListResponse,
    SkillContentResponse,
    PeerInfo,
    PeerListResponse,
    PeerPingResponse,
)
from awm.operations.sessions import SESSION_OPERATIONS
from awm.registry import register_fastapi_routes
from awm.services import core, projects, scopes, locks, shared_resources, skills
from awm.tool_dispatch import TOOL_DEFINITIONS, handle_tool, mark_core_start

# ---------------------------------------------------------------------------
# Idle shutdown + reaper state
# ---------------------------------------------------------------------------

_last_request_time: float = 0.0
_shutdown_event: asyncio.Event | None = None


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _reaper_loop():
    """Periodically reap stale locks."""
    while True:
        try:
            reaped = locks.reap_stale()
            if reaped:
                print(f"[reaper] Reaped {reaped} stale lock(s)")
        except Exception as e:
            print(f"[reaper] Error: {e}")
        await asyncio.sleep(REAPER_INTERVAL)


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

    # Reap stale locks from previous crashes
    reaped = locks.reap_stale()
    if reaped:
        print(f"[startup] Reaped {reaped} stale lock(s) from previous session")

    # Write PID file
    import os
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    # Start background tasks
    reaper_task = asyncio.create_task(_reaper_loop())
    idle_task = asyncio.create_task(_idle_shutdown_loop())

    yield

    # Cleanup
    reaper_task.cancel()
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
    conn = get_connection()
    try:
        active_locks = conn.execute("SELECT COUNT(*) FROM locks").fetchone()[0]
        active_edits = conn.execute(
            "SELECT COUNT(*) FROM shared_edits WHERE status = 'active'"
        ).fetchone()[0]
    finally:
        conn.close()

    scope_result = scopes.list_scopes(status="active")

    # Leadership view — only meaningful when reached via the exposed listener.
    # When the singleton hasn't been configured (core-only / pre-lifespan),
    # default to ACTIVE so single-node deployments keep their UI reachable.
    try:
        from awm.services.leadership import state as ldr_state
        s = ldr_state.get_state()
        leadership_state = s.leadership
        current_leader = s.current_leader
        peer_priority = s.self_priority
    except Exception:
        leadership_state = "ACTIVE"
        current_leader = None
        peer_priority = 100

    return StatusResponse(
        workspace_root=str(WORKSPACE_ROOT),
        active_locks=active_locks,
        active_scopes=scope_result.total,
        active_shared_edits=active_edits,
        leadership_state=leadership_state,
        current_leader=current_leader,
        peer_priority=peer_priority,
    )


# ---------------------------------------------------------------------------
# Peer identity (federation)
# ---------------------------------------------------------------------------

@app.get("/peer")
def get_peer_identity():
    """Return the local peer_id + advertise_url, or 404 if `awm peer init`
    has not been run on this instance. Used by remote peers to verify they
    are talking to the right host."""
    from awm.services.network.peers import get_local_identity, LocalIdentityError
    try:
        ident = get_local_identity()
    except LocalIdentityError as exc:
        raise HTTPException(500, str(exc))
    if ident is None:
        raise HTTPException(404, "local peer identity not configured")
    return ident


@app.get("/peers", response_model=PeerListResponse)
def list_peers_endpoint():
    """List registered remote peers (control-center surface)."""
    from awm.services.network import peers as peer_svc
    rows = peer_svc.list_peers()
    return PeerListResponse(peers=[PeerInfo(**r) for r in rows])


@app.get("/peers/{peer_id}/ping", response_model=PeerPingResponse)
def ping_peer_endpoint(peer_id: str):
    """Probe a registered peer via its SSH tunnel; reports reachability
    and round-trip ms."""
    from awm.services.network import peers as peer_svc
    t0 = time.monotonic()
    result = peer_svc.ping_peer(peer_id)
    rtt_ms = (time.monotonic() - t0) * 1000.0
    ok = bool(result.get("ok"))
    return PeerPingResponse(
        peer_id=peer_id,
        ok=ok,
        rtt_ms=round(rtt_ms, 2) if ok else None,
        error=None if ok else (result.get("reason") or "ping failed"),
    )


@app.get("/projects", response_model=ProjectListResponse)
def list_projects_endpoint():
    """List projects derived from the scopes table, with per-status counts."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT project, status, COUNT(*) AS n FROM scopes "
            "GROUP BY project, status ORDER BY project"
        ).fetchall()
    finally:
        conn.close()
    by_project: dict[str, ProjectScopeCounts] = {}
    for r in rows:
        counts = by_project.setdefault(r["project"], ProjectScopeCounts())
        if r["status"] == "active":
            counts.active = r["n"]
        elif r["status"] == "completed":
            counts.completed = r["n"]
        elif r["status"] == "deleted":
            counts.deleted = r["n"]
    return ProjectListResponse(projects=[
        ProjectListInfo(name=name, scope_counts=counts)
        for name, counts in by_project.items()
    ])


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

@app.get("/scopes", response_model=ScopeListResponse)
def list_scopes_endpoint(
    status: str | None = Query(None),
    project: str | None = Query(None),
):
    return scopes.list_scopes(status=status, project=project)


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
# Locks
# ---------------------------------------------------------------------------

@app.post("/locks", response_model=LockActionResponse)
def acquire_lock(req: LockAcquireRequest):
    result = locks.acquire(req)
    if result.lock is None:
        raise HTTPException(409, result.message)
    return result


@app.delete("/locks", response_model=LockActionResponse)
def release_lock(
    path: str = Query(...),
    holder: str = Query(...),
):
    return locks.release(path, holder)


@app.get("/locks", response_model=LockListResponse)
def list_locks_endpoint(
    holder: str | None = Query(None),
    path: str | None = Query(None),
):
    return locks.list_locks(holder_id=holder, path=path)


@app.post("/locks/heartbeat", response_model=LockActionResponse)
def heartbeat_locks(holder: str = Query(...)):
    return locks.heartbeat(holder)


@app.post("/locks/reap")
def reap_locks():
    count = locks.reap_stale()
    return {"reaped": count}


# ---------------------------------------------------------------------------
# Shared Resources
# ---------------------------------------------------------------------------

@app.post("/shared", response_model=SharedEditActionResponse)
def start_shared_edit(req: SharedEditRequest):
    try:
        return shared_resources.start_edit(req)
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.post("/shared/{name}/merge", response_model=SharedEditActionResponse)
def merge_shared_edit(name: str):
    return shared_resources.merge_edit(name)


@app.get("/shared", response_model=SharedEditListResponse)
def list_shared_edits(status: str | None = Query(None)):
    return shared_resources.list_edits(status=status)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@app.get("/skills/search", response_model=SkillListResponse)
def search_skills_endpoint(q: str = Query(...)):
    return skills.search_skills(q)


@app.get("/skills", response_model=SkillListResponse)
def list_skills_endpoint(
    type: str | None = Query(None),
    tags: str | None = Query(None, description="Comma-separated tags"),
):
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    return skills.list_skills(type_filter=type, tags=tag_list)


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
