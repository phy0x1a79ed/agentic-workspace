"""PTT service — backend for the @awm/ptt stripe.

The hub serves the UI bundle at ``<prefix>/`` (``/ptt/`` by default) and
proxies ``<prefix>/_api/*`` to this process. After path-rewrite the
backend sees root paths:

- ``GET /healthz``    — hub health poll (no auth)
- ``GET /``           — diagnostic; echoes the hub-forwarded peer/user
- ``POST /transcribe`` — HTTP fallback STT (raw int16 LE 16 kHz mono PCM body)
- ``WS /stream``      — per-user PTT/STT session (delegates to ``backend.registry``)

The hub forwards traffic on loopback without auth headers (federation/auth
retired). ``/healthz`` is unauthed so the supervisor's local httpx poll
flips ``backend_status`` to ready.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket


log = logging.getLogger("awm.services.ptt")


def build_app() -> FastAPI:
    app = FastAPI(title="awm-ptt-svc")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    router = APIRouter(tags=["ptt"])

    @router.get("/")
    async def root(req: Request) -> dict:
        return {
            "service": "ptt",
            "version": "0.1.0",
            "x_awm_from": req.headers.get("x-awm-from"),
            "x_awm_as": req.headers.get("x-awm-as"),
        }

    @router.post("/transcribe")
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
        await ws.accept()
        from backend.registry import run_ptt_ws_session
        await run_ptt_ws_session(ws, "user:operator")

    app.include_router(router)
    return app
