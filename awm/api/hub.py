"""Service-hub control plane.

Endpoints:

  POST   /hub/register             — register a new service
  WS     /hub/lease/{service_id}   — hold liveness; disconnect → eviction
  GET    /hub/services             — list registered services
  GET    /hub/stripes              — list stripe registrations with backend status
  DELETE /hub/services/{name}      — explicit deregister

A registration is one of:

* ``kind="url"`` (the original ``svc-*`` shape) — provide ``url``; the
  hub forwards HTTP+WS at the registered prefix to that URL.
* ``kind="static"`` — provide ``static.dir``; the hub serves files from
  that directory at the prefix, optionally with an auto-generated ESM
  shell when the dir has no ``index.html``.
* ``kind="stripe"`` — provide ``stripe.dir`` for the frontend bundle and
  optionally a ``stripe.backend`` block (``cmd``, ``env``, ``health``,
  ``cwd``); the hub serves the frontend canonically at the prefix root
  and spawns + supervises the backend, exposing it at ``<prefix>/_api/*``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter, Depends, HTTPException, Request, WebSocket, status,
)
from pydantic import BaseModel, Field, model_validator

from awm.middleware_auth import authenticate_websocket, require_bearer
from awm.services.hub.lease import LeaseAlreadyHeld, get_lease_manager
from awm.services.hub.registry import PrefixConflict, get_registry
from awm.services.hub.supervisor import (
    PortPoolExhausted, spawn_backend, terminate,
)

log = logging.getLogger("awm.api.hub")

router = APIRouter(prefix="/hub", tags=["hub"])


class StaticSpec(BaseModel):
    dir: str = Field(..., min_length=1,
                     description="Absolute path on the hub host to serve at the prefix.")
    entry: str | None = Field(
        None,
        description="Relative path to the ESM entry script. "
                    "When the dir has no index.html, the hub renders a minimal "
                    "shell that loads this script as a module.",
    )
    css: list[str] = Field(
        default_factory=list,
        description="Optional CSS files (relative to dir) injected into the auto-shell.",
    )
    mount_id: str = Field(
        "app",
        description="DOM id of the mount node in the auto-shell.",
    )


class StripeBackend(BaseModel):
    cmd: list[str] = Field(..., min_length=1,
                           description="argv-style command to spawn the backend. "
                                       "${AWM_SERVICE_PORT} in any arg is substituted "
                                       "with the hub-allocated port before exec.")
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra env vars merged into the process env. "
                    "AWM_SERVICE_PORT is hub-injected and wins on collision.",
    )
    health: str = Field(
        "/healthz",
        description="Path on the backend the hub polls until 200 to mark ready.",
    )
    cwd: str | None = Field(
        None,
        description="Working directory for the spawned process. "
                    "Defaults to the static dir.",
    )


class StripeSpec(BaseModel):
    dir: str = Field(..., min_length=1,
                     description="Absolute path to the frontend bundle dir "
                                 "(canonical-paths static serving at the prefix root).")
    backend: StripeBackend | None = Field(
        None,
        description="Optional supervised backend. When set, the hub spawns it "
                    "at register time and proxies <prefix>/_api/* to it.",
    )


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1)
    prefix: str = Field(..., min_length=1)
    url: str | None = Field(
        None,
        description="Local URL to forward requests to (kind=url). "
                    "Mutually exclusive with `static` and `stripe`.",
    )
    static: StaticSpec | None = Field(
        None,
        description="Directory-serving spec (kind=static). "
                    "Mutually exclusive with `url` and `stripe`.",
    )
    stripe: StripeSpec | None = Field(
        None,
        description="Component-and-backend bundle (kind=stripe). "
                    "Mutually exclusive with `url` and `static`.",
    )

    @model_validator(mode="after")
    def _one_of(self) -> RegisterRequest:
        provided = [
            ("url", bool(self.url)),
            ("static", self.static is not None),
            ("stripe", self.stripe is not None),
        ]
        n = sum(1 for _, v in provided if v)
        if n != 1:
            raise ValueError(
                "exactly one of `url`, `static`, or `stripe` must be provided"
            )
        return self


class RegisterResponse(BaseModel):
    service_id: str
    name: str
    prefix: str
    kind: str
    url: str | None = None
    static: StaticSpec | None = None
    stripe: StripeSpec | None = None
    lease_ws_path: str


@router.post(
    "/register",
    response_model=RegisterResponse,
    dependencies=[Depends(require_bearer)],
)
async def register(req: RegisterRequest) -> RegisterResponse:
    registry = get_registry()
    try:
        if req.url is not None:
            rec = await registry.register(req.name, req.prefix, req.url)
            log.info(
                "registered service %s prefix=%s url=%s id=%s",
                rec.name, rec.prefix, rec.url, rec.service_id,
            )
        elif req.static is not None:
            resolved = Path(req.static.dir).expanduser().resolve()
            if not resolved.is_dir():
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"static.dir {req.static.dir!r} is not a directory on the hub host",
                )
            rec = await registry.register_static(
                req.name,
                req.prefix,
                str(resolved),
                entry=req.static.entry,
                css=tuple(req.static.css),
                mount_id=req.static.mount_id,
            )
            log.info(
                "registered static service %s prefix=%s dir=%s id=%s",
                rec.name, rec.prefix, rec.static_dir, rec.service_id,
            )
        else:
            assert req.stripe is not None  # enforced by validator
            rec = await _register_stripe(registry, req)
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
        stripe=(
            StripeSpec(
                dir=rec.static_dir,
                backend=(req.stripe.backend if req.stripe is not None else None),
            )
            if rec.kind == "stripe" else None
        ),
        lease_ws_path=f"/hub/lease/{rec.service_id}",
    )


async def _register_stripe(registry, req: RegisterRequest):
    """Resolve the frontend dir, spawn the backend (if declared), register
    the record with a teardown that terminates the supervised process."""
    assert req.stripe is not None
    spec = req.stripe
    resolved = Path(spec.dir).expanduser().resolve()
    if not resolved.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"stripe.dir {spec.dir!r} is not a directory on the hub host",
        )

    # Backendless stripe — just a frontend bundle, no supervision.
    if spec.backend is None:
        rec = await registry.register_stripe(
            req.name, req.prefix, str(resolved),
            has_backend=False,
            backend_port=None,
            teardown=None,
        )
        log.info(
            "registered stripe (frontend-only) %s prefix=%s dir=%s id=%s",
            rec.name, rec.prefix, rec.static_dir, rec.service_id,
        )
        return rec

    # Pre-register WITH has_backend=True so backend_status starts as
    # "starting"; routing returns 503 for `_api/*` until the health poll
    # flips it to "ready". Otherwise a request hitting the proxy before
    # the supervised process binds gets ECONNREFUSED.
    rec = await registry.register_stripe(
        req.name, req.prefix, str(resolved),
        has_backend=True,
        backend_port=None,
        teardown=None,
    )

    cwd = spec.backend.cwd or str(resolved)
    try:
        sup = await spawn_backend(
            rec,
            cmd=list(spec.backend.cmd),
            env_extra=dict(spec.backend.env),
            cwd=cwd,
            health_path=spec.backend.health,
        )
    except PortPoolExhausted as exc:
        await registry.evict_by_name(req.name)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    except (OSError, ValueError) as exc:
        await registry.evict_by_name(req.name)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"failed to spawn backend for {req.name!r}: {exc}",
        )

    rec.backend_port = sup.port
    rec.backend_pid = sup.process.pid

    async def _teardown() -> None:
        await terminate(sup)
    rec.teardown = _teardown

    log.info(
        "registered stripe %s prefix=%s dir=%s backend_port=%d id=%s",
        rec.name, rec.prefix, rec.static_dir, sup.port, rec.service_id,
    )
    return rec


@router.websocket("/lease/{service_id}")
async def lease(websocket: WebSocket, service_id: str) -> None:
    """Liveness WS. Close → service evicted from the registry."""
    subprotocol = await authenticate_websocket(websocket)

    registry = get_registry()
    rec = next(
        (r for r in await registry.list() if r.service_id == service_id),
        None,
    )
    if rec is None:
        try:
            await websocket.close(code=4404, reason="unknown service_id")
        except Exception:
            pass
        return

    try:
        await websocket.accept(subprotocol=subprotocol)
    except Exception:
        # authenticate_websocket already closed on auth failure → .accept raises.
        return
    log.info("lease opened for %s (id=%s)", rec.name, service_id)

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


@router.get("/services", dependencies=[Depends(require_bearer)])
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
        }
        if rec.kind == "url":
            entry["url"] = rec.url
        elif rec.kind == "static":
            entry["static"] = {
                "dir": rec.static_dir,
                "entry": rec.entry,
                "css": list(rec.css),
                "mount_id": rec.mount_id,
            }
        else:  # stripe
            entry["stripe"] = {
                "dir": rec.static_dir,
                "backend_port": rec.backend_port,
                "backend_status": rec.backend_status,
                "backend_url": (
                    f"{rec.prefix}/_api/" if rec.backend_port is not None else None
                ),
            }
        out.append(entry)
    return {"services": out}


@router.get("/stripes")
async def list_stripes() -> dict[str, Any]:
    """Global stripe map. Unauthenticated by design — the dev-shell (or
    any composer) fetches this on mount to discover backend URLs to
    inject into stripe components via context. The actual backend
    requests still flow through the hub's authenticated proxy at
    ``<prefix>/_api/*``.
    """
    registry = get_registry()
    out: dict[str, dict[str, Any]] = {}
    for rec in await registry.list():
        if rec.kind != "stripe":
            continue
        out[rec.name] = {
            "prefix": rec.prefix,
            "frontend_url": rec.prefix,
            "backend_url": (
                f"{rec.prefix}/_api/" if rec.backend_port is not None else None
            ),
            "status": rec.backend_status,
        }
    return {"stripes": out}


@router.delete(
    "/services/{name}",
    dependencies=[Depends(require_bearer)],
)
async def deregister(name: str, request: Request) -> dict[str, Any]:
    registry = get_registry()
    rec = await registry.evict_by_name(name)
    if rec is None:
        raise HTTPException(404, f"unknown service: {name}")
    log.info("deregistered service %s (id=%s) via DELETE", name, rec.service_id)
    return {"evicted": {
        "name": rec.name,
        "prefix": rec.prefix,
        "kind": rec.kind,
        "service_id": rec.service_id,
    }}
