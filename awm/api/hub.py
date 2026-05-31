"""Service-hub control plane.

Endpoints:

  POST   /hub/register             — register a new service
  WS     /hub/lease/{service_id}   — hold liveness; disconnect → eviction
  GET    /hub/services             — list registered services
  DELETE /hub/services/{name}      — explicit deregister

A registration is either:

* ``kind="url"`` (the original ``svc-*`` shape) — provide ``url``; the
  hub forwards HTTP+WS at the registered prefix to that URL.
* ``kind="static"`` — provide ``static.dir``; the hub serves files from
  that directory at the prefix, optionally with an auto-generated ESM
  shell when the dir has no ``index.html``.
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


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1)
    prefix: str = Field(..., min_length=1)
    url: str | None = Field(
        None,
        description="Local URL to forward requests to (kind=url). "
                    "Mutually exclusive with `static`.",
    )
    static: StaticSpec | None = Field(
        None,
        description="Directory-serving spec (kind=static). "
                    "Mutually exclusive with `url`.",
    )

    @model_validator(mode="after")
    def _one_of(self) -> RegisterRequest:
        has_url = bool(self.url)
        has_static = self.static is not None
        if has_url == has_static:
            raise ValueError(
                "exactly one of `url` or `static` must be provided"
            )
        return self


class RegisterResponse(BaseModel):
    service_id: str
    name: str
    prefix: str
    kind: str
    url: str | None = None
    static: StaticSpec | None = None
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
        else:
            assert req.static is not None  # enforced by validator
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
        lease_ws_path=f"/hub/lease/{rec.service_id}",
    )


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
        else:
            entry["static"] = {
                "dir": rec.static_dir,
                "entry": rec.entry,
                "css": list(rec.css),
                "mount_id": rec.mount_id,
            }
        out.append(entry)
    return {"services": out}


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
