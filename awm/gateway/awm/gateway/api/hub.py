"""Service-hub control plane.

Endpoints fall into three groups:

* **Generic** — list / deregister / lease, used by all kinds:

    GET    /hub/services
    DELETE /hub/services/{name}
    WS     /hub/lease/{service_id}

* **Service kind** — RPC-over-WS services at /svc/<name>:

    POST /hub/service/register
    WS   /hub/service/control/{service_id}
    WS   /hub/service/bridge/{service_id}/{bridge_id}

* **Shadow overlays** — push a same-prefix overlay on top of a
  base registration:

    POST /hub/shadow/register

* **Non-package registrations** — kind="url" / kind="static" / kind="page":

    POST /hub/register
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()

from fastapi import (
    APIRouter, HTTPException, Request, WebSocket, status,
)
from pydantic import BaseModel, Field, model_validator

from awm.gateway.hub import rpc
from awm.gateway.hub.lease import LeaseAlreadyHeld, get_lease_manager
from awm.gateway.hub.registry import (
    NoBaseToShadow, PrefixConflict, ServiceRecord, get_registry,
)
from awm.gateway.hub.supervisor import (
    remove_service_journal_entry,
    update_service_journal_entry,
)

log = logging.getLogger("awm.api.hub")

router = APIRouter(prefix="/hub", tags=["hub"])


# ============================================================================
# Non-package registrations: kind="url" / kind="static" / kind="page"
# ============================================================================


class StaticSpec(BaseModel):
    dir: str = Field(..., min_length=1)
    entry: str | None = Field(None)
    css: list[str] = Field(default_factory=list)
    mount_id: str = Field("app")


class PageSpec(BaseModel):
    dir: str = Field(..., min_length=1,
                     description="Absolute path to the page bundle dir; "
                                 "served canonically at the /ui/<name> prefix.")


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1)
    prefix: str = Field(..., min_length=1)
    url: str | None = Field(None)
    static: StaticSpec | None = Field(None)
    page: PageSpec | None = Field(None)

    @model_validator(mode="after")
    def _one_of(self) -> "RegisterRequest":
        provided = [
            ("url", bool(self.url)),
            ("static", self.static is not None),
            ("page", self.page is not None),
        ]
        n = sum(1 for _, v in provided if v)
        if n != 1:
            raise ValueError(
                "exactly one of `url`, `static`, or `page` must be provided"
            )
        return self


class RegisterResponse(BaseModel):
    service_id: str
    name: str
    prefix: str
    kind: str
    url: str | None = None
    static: StaticSpec | None = None
    page: PageSpec | None = None
    lease_ws_path: str


@router.post("/register", response_model=RegisterResponse)
async def register(req: RegisterRequest) -> RegisterResponse:
    registry = get_registry()
    try:
        if req.url is not None:
            rec = await registry.register(req.name, req.prefix, req.url)
        elif req.static is not None:
            resolved = Path(req.static.dir).expanduser().resolve()
            if not resolved.is_dir():
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"static.dir {req.static.dir!r} is not a directory",
                )
            rec = await registry.register_static(
                req.name, req.prefix, str(resolved),
                entry=req.static.entry,
                css=tuple(req.static.css),
                mount_id=req.static.mount_id,
            )
        else:
            assert req.page is not None
            resolved = Path(req.page.dir).expanduser().resolve()
            if not resolved.is_dir():
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"page.dir {req.page.dir!r} is not a directory",
                )
            rec = await registry.register_page(req.name, req.prefix, str(resolved))
    except PrefixConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return RegisterResponse(
        service_id=rec.service_id,
        name=rec.name,
        prefix=rec.prefix,
        kind=rec.kind,
        url=rec.url if rec.kind == "url" else None,
        static=(
            StaticSpec(
                dir=rec.static_dir,
                entry=rec.entry,
                css=list(rec.css),
                mount_id=rec.mount_id,
            )
            if rec.kind == "static" else None
        ),
        page=(PageSpec(dir=rec.static_dir) if rec.kind == "page" else None),
        lease_ws_path=f"/hub/lease/{rec.service_id}",
    )


# ============================================================================
# Service kind (G3): /svc/<name>/* + RPC over control WS
# ============================================================================


class ServiceRegisterRequest(BaseModel):
    name: str = Field(..., min_length=1,
                      description="Service name; URL prefix becomes /svc/<name> "
                                  "unless prefix is set explicitly.")
    prefix: str | None = Field(None,
                               description="URL prefix the service claims. "
                                           "Must begin /svc/. Defaults to "
                                           "/svc/<name>.")
    pid: int | None = Field(None,
                            description="The service process PID. The hub uses "
                                        "it for SIGTERM on restart-after-silence.")
    start: list[str] | None = Field(None,
                                    description="Argv to respawn the service. "
                                                "Required only if the hub needs to "
                                                "restart it (hub-spawned services "
                                                "carry this from supervisor side).")
    cwd: str | None = Field(None,
                            description="Working directory for restart. "
                                        "Defaults to current dir if unset.")
    overlay: bool = Field(False,
                          description="Register as a shadow overlay on top of "
                                      "the existing base for /svc/<name> instead "
                                      "of as the base. Set by `awm dev shadow` "
                                      "(via AWM_SERVICE_OVERLAY=1). Requires a "
                                      "base to already exist; the overlay's own "
                                      "control WS is its lease — closing it pops "
                                      "the overlay and the base resumes.")


class ServiceRegisterResponse(BaseModel):
    service_id: str
    name: str
    prefix: str
    control_ws_path: str
    bridge_ws_base: str


@router.post("/service/register", response_model=ServiceRegisterResponse)
async def service_register(req: ServiceRegisterRequest) -> ServiceRegisterResponse:
    registry = get_registry()
    prefix = req.prefix or f"/svc/{req.name}"
    try:
        if req.overlay:
            # Shadow overlay: one process, one identity. The service drives its
            # own control WS as the lease; there is no separate overlay
            # registration to keep in sync (the split-brain `awm dev shadow`
            # used to create). Requires a base for the prefix to already exist.
            rec = ServiceRecord(
                name=req.name,
                prefix=prefix,
                kind="service",
                start_cmd=req.start or [],
                cwd=req.cwd or "",
                backend_pid=req.pid,
                backend_status="starting",
            )
            rec = await registry.push_shadow(rec)
        else:
            rec = await registry.register_service(
                req.name, prefix,
                pid=req.pid,
                start_cmd=req.start or [],
                cwd=req.cwd or "",
            )
    except NoBaseToShadow as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    except PrefixConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    # Pre-create the control channel so the WS handler doesn't race
    # against the supervisor reconnect loop.
    rpc.ensure_control(rec.service_id)

    # Overlays are ephemeral — they are never journaled (reconcile must not
    # respawn them; they belong to a live `awm dev shadow` process).
    if not rec.is_overlay:
        update_service_journal_entry(rec.name, {
            "service_id": rec.service_id,
            "prefix": rec.prefix,
            "last_pid": rec.backend_pid,
            "start_cmd": list(rec.start_cmd),
            "cwd": rec.cwd,
            "last_register": _now_iso(),
            "control_ws_open": False,
        })

    log.info("registered service %s prefix=%s pid=%s id=%s",
             rec.name, rec.prefix, rec.backend_pid, rec.service_id)
    return ServiceRegisterResponse(
        service_id=rec.service_id,
        name=rec.name,
        prefix=rec.prefix,
        control_ws_path=f"/hub/service/control/{rec.service_id}",
        bridge_ws_base=f"/hub/service/bridge/{rec.service_id}",
    )


@router.websocket("/service/control/{service_id}")
async def service_control(websocket: WebSocket, service_id: str) -> None:
    """Persistent RPC envelope channel for one registered service.

    Lifecycle:
      1. Service connects (no auth — the gateway binds loopback-only).
      2. Service sends ``{kind: "ready", api: {...}}``.
      3. Hub routes ``call`` / ``notify`` / ``sub`` / ``session.*``
         envelopes outbound; service replies / emits inbound.
      4. WS close → service evicted from registry (same lease semantics
         as the legacy /hub/lease/{id}). The supervisor's reconnect
         window (10s) gives the service one chance to come back
         before the hub respawns it.
    """

    registry = get_registry()
    rec = registry.get_by_id(service_id)
    if rec is None or rec.kind != "service":
        try:
            await websocket.close(code=4404, reason="unknown service")
        except Exception:
            pass
        return

    try:
        await websocket.accept()
    except Exception:
        return

    ch = rpc.ensure_control(service_id)
    log.info("control WS opened for service %s id=%s", rec.name, service_id)

    if not rec.is_overlay:
        update_service_journal_entry(rec.name, {"control_ws_open": True})

    lm = get_lease_manager()
    disconnect = asyncio.Event()

    async def writer() -> None:
        try:
            while not disconnect.is_set():
                env = await ch.next_outbound()
                try:
                    await websocket.send_text(json.dumps(env))
                except Exception:
                    return
        except Exception:
            return

    async def reader() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                text = msg.get("text")
                if text is None:
                    continue
                try:
                    env = json.loads(text)
                except json.JSONDecodeError:
                    log.warning("invalid JSON on service control WS %s", service_id)
                    continue
                _route_inbound(ch, rec, env)
        except Exception:
            return
        finally:
            disconnect.set()

    writer_task = asyncio.create_task(writer())
    reader_task = asyncio.create_task(reader())

    try:
        try:
            await lm.hold(service_id, disconnect)
        except LeaseAlreadyHeld:
            await websocket.close(code=4409, reason="lease already held")
            return
    finally:
        rec.backend_status = "down"
        if not rec.is_overlay:
            update_service_journal_entry(rec.name, {"control_ws_open": False})
        writer_task.cancel()
        reader_task.cancel()
        rpc.drop_control(service_id)
        try:
            await websocket.close()
        except Exception:
            pass


def _route_inbound(ch: "rpc.ControlChannel", rec: ServiceRecord,
                   env: dict[str, Any]) -> None:
    kind = env.get("kind")
    if kind == "ready":
        ch.set_api(env.get("api") or {})
        rec.api = ch.api
        rec.backend_status = "ready"
    elif kind == "reply":
        ch.handle_reply(env)
    elif kind == "emit":
        ch.handle_emit(env)
    elif kind == "session.opened":
        ch.handle_session_opened(env)
    elif kind == "session.frame":
        ch.handle_session_frame(env)
    elif kind == "session.close":
        ch.handle_session_close(env)
    else:
        log.debug("ignored inbound envelope kind=%s on service %s",
                  kind, rec.name)


@router.websocket("/service/bridge/{service_id}/{bridge_id}")
async def service_bridge(websocket: WebSocket, service_id: str,
                         bridge_id: str) -> None:
    """Upstream side of a direct session/emitter bridge.

    The service opens this WS in response to receiving a ``session.open``
    envelope with a ``bridge_id``. The hub's ``proxy_session_ws`` (and,
    for direct emitters, the equivalent emitter path) handles the
    browser side; this handler just holds the upstream WS open until
    the relay coroutine signals close.
    """

    ch = rpc.get_control(service_id)
    if ch is None:
        try:
            await websocket.close(code=4404, reason="unknown service")
        except Exception:
            pass
        return
    bridge = ch.get_bridge(bridge_id)
    if bridge is None:
        try:
            await websocket.close(code=4404, reason="unknown bridge")
        except Exception:
            pass
        return

    try:
        await websocket.accept()
    except Exception:
        return

    bridge.upstream_obj = websocket
    bridge.upstream_ready.set()

    # The browser-side relay (proxy_session_ws._relay_direct_session)
    # owns the actual frame pumping. We just block here so the upstream
    # WS stays open until the session is dropped or the upstream closes
    # itself.
    sess = ch.get_session(bridge.session_id)
    if sess is None:
        try:
            await websocket.close(code=1011, reason="session vanished")
        except Exception:
            pass
        return

    await sess.closed.wait()


# ============================================================================
# Shadow overlays (G5)
# ============================================================================


class ShadowRegisterRequest(BaseModel):
    name: str = Field(..., min_length=1,
                      description="Unique shadow name (must NOT collide with "
                                  "the base or any other overlay).")
    prefix: str = Field(..., min_length=1,
                        description="Prefix to shadow; a base must already "
                                    "exist for this prefix.")
    page: dict = Field(...,
                       description="Page shadow: {dir: <absolute path>}. Service "
                                   "overlays no longer use this endpoint — a "
                                   "service shadows itself by registering at "
                                   "/hub/service/register with overlay=true (one "
                                   "process, one identity, its own control WS as "
                                   "the lease).")


class ShadowRegisterResponse(BaseModel):
    service_id: str
    name: str
    prefix: str
    kind: str
    lease_ws_path: str
    control_ws_path: str | None = None
    bridge_ws_base: str | None = None


@router.post("/shadow/register", response_model=ShadowRegisterResponse)
async def shadow_register(req: ShadowRegisterRequest) -> ShadowRegisterResponse:
    """Push a *page* overlay on an existing /ui/<name> base. Service overlays
    register through /hub/service/register with ``overlay=true`` instead."""
    registry = get_registry()
    try:
        page_dir = Path(str(req.page.get("dir", ""))).expanduser().resolve()
        if not page_dir.is_dir():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"page.dir {req.page.get('dir')!r} is not a directory",
            )
        rec = ServiceRecord(
            name=req.name, prefix=req.prefix, kind="page",
            static_dir=str(page_dir),
        )
        rec.backend_status = "ready"
        rec = await registry.push_shadow(rec)
        return ShadowRegisterResponse(
            service_id=rec.service_id, name=rec.name, prefix=rec.prefix,
            kind=rec.kind, lease_ws_path=f"/hub/lease/{rec.service_id}",
        )
    except NoBaseToShadow as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    except PrefixConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))


# ============================================================================
# Lease, list, deregister (shared)
# ============================================================================


@router.websocket("/lease/{service_id}")
async def lease(websocket: WebSocket, service_id: str) -> None:
    """Generic liveness WS for non-service kinds (page shadows, url, static).
    Service-kind registrations use /hub/service/control/{id} which doubles
    as the lease (closing it evicts the service)."""

    registry = get_registry()
    rec = registry.get_by_id(service_id)
    if rec is None:
        try:
            await websocket.close(code=4404, reason="unknown service_id")
        except Exception:
            pass
        return

    try:
        await websocket.accept()
    except Exception:
        return

    lm = get_lease_manager()
    disconnect = asyncio.Event()

    async def reader():
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
        except Exception:
            return
        finally:
            disconnect.set()

    reader_task = asyncio.create_task(reader())
    try:
        try:
            await websocket.send_json({"type": "ready",
                                       "service_id": service_id,
                                       "name": rec.name})
        except Exception:
            disconnect.set()
        try:
            await lm.hold(service_id, disconnect)
        except LeaseAlreadyHeld:
            await websocket.close(code=4409, reason="lease already held")
            return
    finally:
        reader_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass


@router.get("/services")
async def list_services() -> dict[str, Any]:
    registry = get_registry()
    lm = get_lease_manager()
    out = []
    for rec in await registry.list():
        entry: dict[str, Any] = {
            "name": rec.name,
            "prefix": rec.prefix,
            "kind": rec.kind,
            "service_id": rec.service_id,
            "lease_held": lm.is_held(rec.service_id),
            "is_overlay": rec.is_overlay,
            "backend_status": rec.backend_status,
            "backend_pid": rec.backend_pid,
        }
        if rec.kind == "url":
            entry["url"] = rec.url
        elif rec.kind in ("static", "page"):
            entry["dir"] = rec.static_dir
        elif rec.kind == "service":
            entry["api"] = rec.api
        out.append(entry)
    return {"services": out}


@router.delete("/services/{name}")
async def deregister(name: str, request: Request, kind: str | None = None) -> dict[str, Any]:
    registry = get_registry()
    from awm.gateway.hub.registry import PrefixConflict
    try:
        rec = await registry.evict_by_name(name, kind=kind)
    except PrefixConflict as e:
        raise HTTPException(409, str(e))
    if rec is None:
        raise HTTPException(404, f"unknown service: {name}")
    log.info("deregistered service %s (id=%s)", name, rec.service_id)
    return {"evicted": {
        "name": rec.name,
        "prefix": rec.prefix,
        "kind": rec.kind,
        "service_id": rec.service_id,
    }}


# ============================================================================
# Feature-service lifecycle — the `awm services` control surface.
#
# The running gateway is authoritative: it owns the discovery root, the
# enable-state file, the PID journal, and the registry. So `awm services`
# routes everything here rather than re-deriving locally (which would resolve
# the wrong tree under a dev-sandbox shadow). One spawn path
# (supervisor.spawn_and_journal, shared with first-boot bootstrap); one stop
# path (evict + kill pid group + drop journal) so reconcile never resurrects a
# stopped service.
# ============================================================================


@router.get("/services/discovered")
async def list_discovered_services() -> dict[str, Any]:
    """Discovery ⋈ enable-state ⋈ live registration — the `awm services list`
    view."""
    from awm.gateway.hub import discovery
    registry = get_registry()
    out = []
    for spec in discovery.discover_services():
        rec = registry.get_by_name("service", spec.name)
        out.append({
            "name": spec.name,
            "enabled": spec.enabled,
            "running": rec is not None,
            "status": rec.backend_status if rec is not None else "stopped",
            "pid": rec.backend_pid if rec is not None else None,
        })
    return {"services": out}


@router.post("/services/{name}/start")
async def start_service(name: str) -> dict[str, Any]:
    from awm.gateway.hub import discovery, supervisor
    registry = get_registry()
    if registry.get_by_name("service", name) is not None:
        return {"name": name, "started": False, "reason": "already-running"}
    spec = discovery.discover_service(name)
    if spec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"no service folder {name!r} with a run.sh")
    pid = supervisor.spawn_and_journal(name, list(spec.start_cmd), spec.cwd)
    return {"name": name, "started": True, "pid": pid}


@router.post("/services/{name}/stop")
async def stop_service(name: str) -> dict[str, Any]:
    from awm.gateway.hub import supervisor
    registry = get_registry()
    entry = supervisor.load_service_journal().get(name) or {}
    rec = registry.get_by_name("service", name)
    pid = (rec.backend_pid if rec is not None else None) or entry.get("last_pid")
    if rec is not None:
        try:
            await registry.evict_by_name(name, kind="service")
        except PrefixConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if pid:
        await asyncio.get_event_loop().run_in_executor(
            None, supervisor.kill_pid_group, pid)
    supervisor.remove_service_journal_entry(name)
    return {"name": name, "stopped": True, "killed_pid": pid}


@router.post("/services/{name}/enable")
async def enable_service(name: str) -> dict[str, Any]:
    from awm.gateway.hub import discovery
    discovery.set_enabled(name, True)
    return {"name": name, "enabled": True, "start": await start_service(name)}


@router.post("/services/{name}/disable")
async def disable_service(name: str) -> dict[str, Any]:
    from awm.gateway.hub import discovery
    discovery.set_enabled(name, False)
    return {"name": name, "enabled": False, "stop": await stop_service(name)}
