"""FastAPI app + uvicorn with lifespan management."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Query

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
    ScopeCreateRequest,
    ScopeUpdateRequest,
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
)
from awm.operations.sessions import SESSION_OPERATIONS
from awm.registry import register_fastapi_routes
from awm.services import projects, scopes, locks, shared_resources, skills

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

    return StatusResponse(
        workspace_root=str(WORKSPACE_ROOT),
        active_locks=active_locks,
        active_scopes=scope_result.total,
        active_shared_edits=active_edits,
    )


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


@app.post("/skills/reindex")
def reindex_skills_endpoint():
    content = skills.regenerate_index()
    return {"message": "Index regenerated", "content": content}


# ---------------------------------------------------------------------------
# Sessions — registered via registry
# ---------------------------------------------------------------------------

register_fastapi_routes(app, SESSION_OPERATIONS)


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
