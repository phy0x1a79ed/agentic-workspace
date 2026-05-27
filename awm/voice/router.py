"""FastAPI router for the voice/STT side channel.

Endpoints:
  WS   /voice/ws            per-user PTT/STT WebSocket (status, stt_result, PCM up)
  POST /voice/stt           HTTP fallback: raw int16 PCM body → ``{ "text": "..." }``
  POST /voice/tts/speak     synth ``{text}`` via the loaded TTS engine → audio/wav

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

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel, Field

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


# ── Engine registry surface ─────────────────────────────────────────────
# Bridges to the orchestrator's pluggable engine registry (the `voice`
# package at the repo root). Import is deferred to first hit so the awm
# process doesn't pay engine-bootstrap latency at startup.


def _engines_registry():
    import voice.engines as registry  # type: ignore[import-not-found]
    return registry


@router.get("/engines", dependencies=[Depends(require_bearer)])
async def engines_list() -> dict[str, Any]:
    """`{kind: {engine_id: {schema, defaults}}}` for dynamic UI forms."""
    return _engines_registry().list_engines()


@router.get("/engines/loaded", dependencies=[Depends(require_bearer)])
async def engines_loaded() -> dict[str, Any]:
    return _engines_registry().loaded()


class EngineLoadRequest(BaseModel):
    id: str
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/engines/{kind}/load", dependencies=[Depends(require_bearer)])
async def engines_load(kind: str, body: EngineLoadRequest) -> dict[str, Any]:
    try:
        return _engines_registry().load(kind, body.id, body.params)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/engines/{kind}/unload", dependencies=[Depends(require_bearer)])
async def engines_unload(kind: str) -> dict[str, bool]:
    try:
        return {"unloaded": _engines_registry().unload(kind)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ── TTS speak (one-shot synth) ──────────────────────────────────────────


class TtsSpeakRequest(BaseModel):
    text: str


def _pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw int16 LE mono PCM in a minimal RIFF/WAVE container."""
    import struct

    byte_rate = sample_rate * 2
    block_align = 2
    data_size = len(pcm)
    riff_size = 36 + data_size
    header = b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
    fmt = (
        b"fmt " + struct.pack("<I", 16)
        + struct.pack("<HHIIHH", 1, 1, sample_rate, byte_rate, block_align, 16)
    )
    data = b"data" + struct.pack("<I", data_size) + pcm
    return header + fmt + data


@router.post("/tts/speak", dependencies=[Depends(require_bearer)])
async def tts_speak(body: TtsSpeakRequest) -> Response:
    """Synthesize ``text`` via the currently loaded TTS engine.

    Returns ``audio/wav`` so the browser ``Audio`` element plays it directly.
    Requires ``POST /voice/engines/tts/load`` first; 409 otherwise.
    """
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    registry = _engines_registry()
    cur = registry._LOADED.get("tts", {})  # type: ignore[attr-defined]
    instance = cur.get("instance")
    if instance is None:
        raise HTTPException(409, "no tts engine loaded; POST /voice/engines/tts/load first")
    try:
        pcm = await asyncio.get_running_loop().run_in_executor(None, instance.synth, text)
    except Exception as exc:  # noqa: BLE001
        log.exception("tts synth failed")
        raise HTTPException(500, f"tts: {exc}")
    if not pcm:
        return Response(content=b"", media_type="audio/wav")
    sample_rate = int(getattr(instance, "sample_rate", 16000) or 16000)
    return Response(content=_pcm16_to_wav(pcm, sample_rate), media_type="audio/wav")
