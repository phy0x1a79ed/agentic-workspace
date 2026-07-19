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
    APIRouter, HTTPException, WebSocket, status,
)
from pydantic import BaseModel, Field, model_validator

from awm.gateway.hub import rpc
from awm.gateway.hub.lease import LeaseAlreadyHeld, get_lease_manager
from awm.gateway.hub.registry import (
    NoBaseToShadow, PrefixConflict, ServiceRecord, get_registry,
)
from awm.gateway.hub.supervisor import _resolve_identity, update_service_journal_entry

log = logging.getLogger("awm.api.hub")

router = APIRouter(prefix="/hub", tags=["hub"])

# WS close code for "a newer shadow took over this prefix" — a peer of the
# existing 4409 (lease already held) / 4404 (unknown service). The evicted
# client (service adapter or `awm dev shadow` page lease) maps this code to a
# clean stand-down carrying the who/why reason, not a reconnect bounce.
_EVICTED_BY_SHADOW = 4410


def _close_reason(text: str) -> str:
    """Clamp to the 123-byte WS close-frame reason limit (by encoded bytes,
    not characters — a char slice can still overflow and `websockets` raises)."""
    return text.encode("utf-8")[:123].decode("utf-8", errors="ignore")


def _notify_evicted(evicted: list[ServiceRecord], evictor: str) -> None:
    """Stage a who/why notice on each evicted overlay's lease so its WS handler
    closes with code 4410 + ``evicted by {evictor}: a newer shadow connected``.
    The records are already out of the registry; this just wakes their handlers."""
    lm = get_lease_manager()
    for ev in evicted:
        lm.signal_evicted(ev.service_id, "a newer shadow connected", evictor)


# ============================================================================
# Non-package registrations: kind="url" / kind="static" / kind="page"
# ============================================================================


class StaticSpec(BaseModel):
    dir: str = Field(..., min_length=1)
    entry: str | None = Field(None)
    css: list[str] = Field(default_factory=list)
    mount_id: str = Field("app")
    deny: list[str] = Field(
        default_factory=list,
        description="Mask globs. A request whose resolved path (relative to "
                    "`dir`, post-symlink) matches any glob 404s as if missing. "
                    "Matched with PurePosixPath.full_match, so `**` spans "
                    "segments. Lets a broad mount (e.g. root '/') hide secrets.",
    )


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
                deny=tuple(req.static.deny),
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
                deny=list(rec.deny),
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
                                      "the overlay and the base resumes. A new "
                                      "overlay evicts any incumbent overlay on "
                                      "the prefix (last connect wins).")
    origin: str | None = Field(None,
                               description="Human 'who' label for an overlay "
                                           "(e.g. 'stt-shadow @ web-stt'), used "
                                           "as the evictor identity in the notice "
                                           "sent to an overlay this one evicts.")


class ServiceRegisterResponse(BaseModel):
    service_id: str
    name: str
    prefix: str
    control_ws_path: str
    bridge_ws_base: str
    canonical_workspace: str = Field(
        "",
        description="The hub's canonical workspace root. A service learns where "
                    "agents/data actually live FROM the hub on register, rather "
                    "than assuming it from its own AWM_WORKSPACE env. A shadow "
                    "overlay keeps its DBs on an isolated local root but points "
                    "real work here; a native service's local root == this.")


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
            # Last connect wins: this overlay evicts any incumbent overlay(s)
            # on the prefix, then notifies each with a who/why close.
            rec = ServiceRecord(
                name=req.name,
                prefix=prefix,
                kind="service",
                start_cmd=req.start or [],
                cwd=req.cwd or "",
                backend_pid=req.pid,
                backend_status="starting",
                origin=req.origin or req.name,
            )
            rec, evicted = await registry.replace_overlays(rec)
            _notify_evicted(evicted, rec.origin or rec.name)
        else:
            # Duplicate-instance guard (T3): if a record for this name already
            # exists AND its control-WS lease is currently held, a live
            # instance is connected — turn the newcomer away with 409 so it
            # stands down (the adapter raises GiveUp and exits 0) and the
            # incumbent's service_id + lease are left untouched. A record whose
            # lease is NOT held is dead (or a benign reconcile placeholder), so
            # we fall through to the existing replace-in-place takeover.
            existing = registry.get_by_name("service", req.name)
            if existing is not None and get_lease_manager().is_held(
                    existing.service_id):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"service {req.name!r} already has a live instance connected",
                )
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
        # Journal identity that matches what a respawn will actually use: for a
        # discoverable service, discovery is authoritative, so a self-register
        # from a wrong worktree can never re-contaminate the journal's cwd. Only
        # a non-discoverable external registration records its self-reported
        # start_cmd/cwd (defense-in-depth complementing _resolve_identity's
        # respawn-side win).
        j_start, j_cwd = _resolve_identity(rec.name, {
            "start_cmd": list(rec.start_cmd), "cwd": rec.cwd,
        })
        update_service_journal_entry(rec.name, {
            "service_id": rec.service_id,
            "prefix": rec.prefix,
            "last_pid": rec.backend_pid,
            "start_cmd": list(j_start),
            "cwd": j_cwd,
            "last_register": _now_iso(),
            "control_ws_open": False,
        })

    log.info("registered service %s prefix=%s pid=%s id=%s",
             rec.name, rec.prefix, rec.backend_pid, rec.service_id)
    # Report the hub's own canonical workspace so the service can resolve where
    # agents/data live without assuming it from its (possibly isolated) env.
    from awm import config as _config
    return ServiceRegisterResponse(
        service_id=rec.service_id,
        name=rec.name,
        prefix=rec.prefix,
        control_ws_path=f"/hub/service/control/{rec.service_id}",
        bridge_ws_base=f"/hub/service/bridge/{rec.service_id}",
        canonical_workspace=str(_config.WORKSPACE_ROOT),
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
                _route_inbound(ch, service_id, env)
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
        # hold returned: either the client closed, or a newer shadow evicted us.
        notice = lm.take_eviction(service_id)
        if notice is not None:
            reason, evictor = notice
            try:
                await websocket.close(
                    code=_EVICTED_BY_SHADOW,
                    reason=_close_reason(f"evicted by {evictor}: {reason}"),
                )
            except Exception:
                pass
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
        # Runtime crash-respawn watchdog (T5): the service's control WS just
        # dropped and the lease was released (eviction ran inside lm.hold).
        # If this wasn't a deliberate stop, a gateway teardown, or an overlay,
        # give the service the reconnect window to come back, else respawn it.
        # The journal-entry check (a deliberate `awm services stop` drops the
        # entry *before* killing) and the shutting-down flag keep this from
        # resurrecting something that is supposed to stay down.
        try:
            from awm.gateway.hub import discovery, supervisor
            if (not supervisor.is_shutting_down()
                    and not rec.is_overlay
                    and supervisor.load_service_journal().get(rec.name)
                    and discovery.is_enabled(rec.name)):
                asyncio.create_task(supervisor.supervise_disconnect(rec.name))
        except Exception:
            log.debug("disconnect watchdog hook skipped for %s",
                      rec.name, exc_info=True)


def _route_inbound(ch: "rpc.ControlChannel", service_id: str,
                   env: dict[str, Any]) -> None:
    kind = env.get("kind")
    if kind == "ready":
        ch.set_api(env.get("api") or {})
        # Re-look-up the record fresh by service_id rather than trusting a record
        # captured at WS-accept time: a concurrent reconcile / disconnect-respawn
        # may have replaced the registry record for this service_id in the
        # interim, and flipping a stale (already-evicted) record to ``ready``
        # would leave the live one stuck ``starting`` with an empty ``api``.
        registry = get_registry()
        rec = registry.get_by_id(service_id)
        if rec is not None:
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
                      description="Shadow name (must NOT collide with the base; "
                                  "may reuse an incumbent overlay's name — the "
                                  "incumbent is evicted, last connect wins).")
    prefix: str = Field(..., min_length=1,
                        description="Prefix to shadow; a base must already "
                                    "exist for this prefix.")
    origin: str | None = Field(None,
                               description="Human 'who' label for this overlay "
                                           "(e.g. 'shadow:stt:web-stt'), used as "
                                           "the evictor identity in the notice "
                                           "sent to an overlay this one evicts.")
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
            origin=req.origin or req.name,
        )
        rec.backend_status = "ready"
        rec, evicted = await registry.replace_overlays(rec)
        _notify_evicted(evicted, rec.origin or rec.name)
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
        # hold returned: either the client closed, or a newer shadow evicted us.
        notice = lm.take_eviction(service_id)
        if notice is not None:
            reason, evictor = notice
            try:
                await websocket.close(
                    code=_EVICTED_BY_SHADOW,
                    reason=_close_reason(f"evicted by {evictor}: {reason}"),
                )
            except Exception:
                pass
    finally:
        reader_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass


# ============================================================================
# Hub list / deregister and the feature-service lifecycle (`awm services …`)
# moved onto the declarative generation system — see
# ``awm.gateway.gateway_ops.GATEWAY_OPERATIONS``. Their HTTP routes
# (GET /hub/services, DELETE /hub/services/{name}, GET /hub/services/discovered,
# POST /hub/services/{name}/{start,stop,restart,enable,disable}) are generated
# in ``server.py`` via ``register_fastapi_routes`` from the same Operations that
# drive the MCP tools + CLI commands. Do not re-add hand-rolled duplicates here.
# ============================================================================
