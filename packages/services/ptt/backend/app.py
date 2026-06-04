"""PTT service — backend for the @awm/ptt stripe.

The hub serves the UI bundle at ``<prefix>/`` (``/ptt/`` by default) and
proxies ``<prefix>/_api/*`` to this process. After path-rewrite the
backend sees root paths:

- ``GET /healthz``    — hub health poll (no auth)
- ``GET /``           — diagnostic; echoes the hub-forwarded peer/user
- ``POST /transcribe`` — HTTP fallback STT (raw int16 LE 16 kHz mono PCM body)
- ``WS /stream``      — per-user PTT/STT session (delegates to ``backend.registry``)

Auth: the hub injects its peer-bearer toward this service and preserves
``X-Awm-As`` verbatim. HTTP routes guard with ``require_peer_bearer``;
WS guards with ``authenticate_websocket``. ``/healthz`` is unauthed so the
supervisor's local httpx poll flips ``backend_status`` to ready.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket
from starlette.websockets import WebSocketState

from awm.services.auth.middleware_auth import authenticate_websocket, require_peer_bearer


log = logging.getLogger("awm.services.ptt")


def build_app() -> FastAPI:
    app = FastAPI(title="awm-ptt-svc")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    router = APIRouter(tags=["ptt"])

    @router.get("/", dependencies=[Depends(require_peer_bearer)])
    async def root(req: Request) -> dict:
        return {
            "service": "ptt",
            "version": "0.1.0",
            "x_awm_from": req.headers.get("x-awm-from"),
            "x_awm_as": req.headers.get("x-awm-as"),
        }

    @router.post("/transcribe", dependencies=[Depends(require_peer_bearer)])
    async def transcribe(req: Request) -> dict:
        body = await req.body()
        if not body:
            raise HTTPException(400, "empty pcm body")
        from awm.voice.stt import get_transcriber

        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(
                None, get_transcriber().transcribe, body, 16000,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("stt http path failed")
            raise HTTPException(500, f"stt: {exc}")
        return {"text": text or ""}

    @router.websocket("/stream")
    async def stream(ws: WebSocket) -> None:
        subprotocol = await authenticate_websocket(ws)
        if ws.client_state == WebSocketState.DISCONNECTED:
            return
        user_as = ws.headers.get("x-awm-as", "").strip()
        if not user_as:
            await ws.close(code=1008, reason="x-awm-as required")
            return
        await ws.accept(subprotocol=subprotocol)
        from backend.registry import run_ptt_ws_session
        await run_ptt_ws_session(ws, user_as)

    app.include_router(router)
    return app
