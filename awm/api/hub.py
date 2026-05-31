"""Service-hub control plane.

Endpoints:

  POST   /hub/register             — register a new service
  WS     /hub/lease/{service_id}   — hold liveness; disconnect → eviction
  GET    /hub/services             — list registered services
  DELETE /hub/services/{name}      — explicit deregister
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import (
    APIRouter, Depends, HTTPException, Request, WebSocket, status,
)
from pydantic import BaseModel, Field

from awm.middleware_auth import authenticate_websocket, require_bearer
from awm.services.hub.lease import LeaseAlreadyHeld, get_lease_manager
from awm.services.hub.registry import PrefixConflict, get_registry

log = logging.getLogger("awm.api.hub")

router = APIRouter(prefix="/hub", tags=["hub"])


class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=1)
    prefix: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)


class RegisterResponse(BaseModel):
    service_id: str
    name: str
    prefix: str
    url: str
    lease_ws_path: str


@router.post(
    "/register",
    response_model=RegisterResponse,
    dependencies=[Depends(require_bearer)],
)
async def register(req: RegisterRequest) -> RegisterResponse:
    registry = get_registry()
    try:
        rec = await registry.register(req.name, req.prefix, req.url)
    except PrefixConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    log.info("registered service %s prefix=%s url=%s id=%s",
             rec.name, rec.prefix, rec.url, rec.service_id)
    return RegisterResponse(
        service_id=rec.service_id,
        name=rec.name,
        prefix=rec.prefix,
        url=rec.url,
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
        out.append({
            "name": rec.name,
            "prefix": rec.prefix,
            "url": rec.url,
            "service_id": rec.service_id,
            "lease_held": lm.is_held(rec.service_id),
        })
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
        "service_id": rec.service_id,
    }}
