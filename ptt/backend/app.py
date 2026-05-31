"""PTT service: ``/ptt`` prefix, owns audio capture + STT for PTT V2.

Three endpoints:

- ``GET /ptt/``           — diagnostic; echoes the hub-forwarded peer/user.
- ``POST /ptt/transcribe`` — HTTP fallback STT: raw int16 LE 16 kHz mono
                            PCM body → ``{"text": "..."}``. Ported from
                            ``awm/voice/router.py::stt_http``.
- ``WS /ptt/stream``      — per-user PTT/STT WebSocket (start/end/cancel
                            JSON + binary PCM up; ``stt_result``/``status``
                            JSON broadcast down). Delegates to
                            ``ptt.backend.registry.run_ptt_ws_session``.

Auth: the hub injects its peer-bearer toward this service and preserves
``X-Awm-As`` verbatim. HTTP routes guard with ``require_peer_bearer``; WS
guards with ``authenticate_websocket`` (any valid bearer).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket
from starlette.websockets import WebSocketState

from awm.middleware_auth import authenticate_websocket, require_peer_bearer


log = logging.getLogger("awm.services.ptt")


def build_app() -> FastAPI:
    app = FastAPI(title="awm-ptt-svc")
    router = APIRouter(prefix="/ptt", tags=["ptt"])

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
        # authenticate_websocket closes with 1008 on failure and returns None.
        # A None return with the socket already closed → bail.
        if ws.client_state == WebSocketState.DISCONNECTED:
            return
        user_as = ws.headers.get("x-awm-as", "").strip()
        if not user_as:
            await ws.close(code=1008, reason="x-awm-as required")
            return
        await ws.accept(subprotocol=subprotocol)
        from ptt.backend.registry import run_ptt_ws_session
        await run_ptt_ws_session(ws, user_as)

    app.include_router(router)
    return app
