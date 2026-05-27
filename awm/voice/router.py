"""FastAPI router for the voice/STT side channel.

Endpoints:
  WS   /voice/ws    per-user PTT/STT WebSocket (status, stt_result, PCM up)
  POST /voice/stt   HTTP fallback: raw int16 PCM body → ``{ "text": "..." }``

The WS surface is intentionally minimal — it carries PTT chunks up and
broadcasts STT results to every tab of the same user. There is no
embedded Claude session anymore; the SPA composes prompts and addresses
recipients on its own.

Auth: HTTP routes use ``Depends(require_bearer)``; WS uses the
``bearer.<token>`` subprotocol via ``authenticate_room_ws``.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket

from awm.middleware_auth import require_bearer
from awm.ws_auth import authenticate_room_ws


log = logging.getLogger("awm.voice.router")


router = APIRouter(prefix="/voice", tags=["voice"])


@router.post("/stt", dependencies=[Depends(require_bearer)])
async def stt_http(request: Request) -> dict:
    """Transcribe a raw int16 LE 16 kHz mono PCM body to text.

    Fallback when the client can't keep the WebSocket open (e.g. while
    backgrounded). For the active foreground flow the WS path delivers
    the same payload via ``stt_result`` so other tabs see it too.
    """
    body = await request.body()
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


@router.websocket("/ws")
async def voice_ws(websocket: WebSocket) -> None:
    auth = await authenticate_room_ws(websocket)
    if not auth.ok:
        return
    await websocket.accept(subprotocol=auth.subprotocol)
    from awm.voice.registry import run_voice_ws_session
    await run_voice_ws_session(websocket, auth.user_as)
