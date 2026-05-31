"""PTT service: ``/ptt`` prefix, owns audio capture + STT for PTT V2.

Stub today — endpoints accept the right shapes and return mocked
responses so the hub round-trip + frontend integration can be wired
before the real audio path lands. Each method is annotated with the
real-implementation pointer (``awm/voice/router.py`` is the V1 source).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket

from awm.middleware_auth import require_peer_bearer
from awm.ws_auth import authenticate_websocket


log = logging.getLogger("awm.services.ptt")


def build_app() -> FastAPI:
    app = FastAPI(title="awm-ptt-svc")
    router = APIRouter(prefix="/ptt", tags=["ptt"])

    @router.get("/", dependencies=[Depends(require_peer_bearer)])
    async def root(req: Request) -> dict:
        return {
            "service": "ptt",
            "version": "0.0.1",
            "x_awm_from": req.headers.get("x-awm-from"),
            "x_awm_as": req.headers.get("x-awm-as"),
        }

    @router.post("/transcribe", dependencies=[Depends(require_peer_bearer)])
    async def transcribe(req: Request) -> dict:
        # V2 target: port logic from awm/voice/router.py::stt_http.
        body = await req.body()
        return {
            "text": "",
            "bytes_received": len(body),
            "x_awm_as": req.headers.get("x-awm-as"),
            "stub": True,
        }

    @router.websocket("/stream")
    async def stream(ws: WebSocket) -> None:
        # V2 target: port logic from awm/voice/router.py::voice_ws +
        # awm/voice/registry.py broadcast plumbing.
        if not await authenticate_websocket(ws):
            return
        await ws.accept(subprotocol=ws.headers.get("sec-websocket-protocol"))
        try:
            async for msg in ws.iter_text():
                await ws.send_text(f"ack:{msg}")
        except Exception:  # noqa: BLE001
            log.debug("ws stream closed", exc_info=True)
            return

    app.include_router(router)
    return app
