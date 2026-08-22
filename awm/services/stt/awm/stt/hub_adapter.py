"""Hub adapter for the stt service (STT-only).

Boots the stt service as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve →
reconnect). Replaces the former hand-rolled, ``AWM_HUB_TOKEN``-era control loop;
the gateway injects only ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — there is no token, ever.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.stt.hub_adapter

Functions (the STT surface):
  - transcribe — HTTP-fallback STT. ``{"pcm_b64": <base64 int16 LE 16 kHz mono>}``
    → ``{"text": ...}``. The live path is the ``stream`` session below; this is
    the one-shot fallback.

Sessions:
  - kind="stream", transport="direct" — the per-user PTT/STT session. The
    browser opens a direct WS that byte-relays through the hub bridge; binary
    PCM frames + JSON control frames in, ``status`` / ``partial`` /
    ``stt_result`` / ``composer`` / ``submit`` frames back. In continuous mode
    the convo dictation-cleanup inner loop runs (an ``awm.agentcore`` opencode
    one-shot per silence-cut). This is the only session the service exposes; the
    mock ``chat`` partner was dropped — the real agent (agents service) replaces
    it.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, SessionContext

from awm.stt.registry import run_stt_ws_session

log = logging.getLogger("awm.stt.hub_adapter")


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "transcribe",
            "description": (
                "HTTP-fallback STT: base64 int16 LE 16 kHz mono PCM in, "
                "transcribed text out."
            ),
            "params": [
                {"name": "pcm_b64", "type": "string", "required": True},
            ],
        },
    ],
    "emitters": [],
    "sessions": [
        {"kind": "stream", "transport": "direct"},
    ],
}


# -- function handlers ------------------------------------------------------


async def _h_transcribe(args: dict) -> dict:
    pcm_b64 = args.get("pcm_b64") if isinstance(args, dict) else None
    if not pcm_b64:
        raise RuntimeError("pcm_b64 required")
    pcm = base64.b64decode(pcm_b64)
    from awm.stt.stt import get_transcriber

    loop = asyncio.get_running_loop()
    text = await loop.run_in_executor(
        None, get_transcriber().transcribe, pcm, 16000,
    )
    return {"text": text or ""}


HANDLERS = {
    "transcribe": _h_transcribe,
}


# -- stream session ---------------------------------------------------------


class _BridgeWs:
    """Adapts the upstream hub bridge (a ``websockets`` connection) to the
    duck-typed WS surface ``run_stt_ws_session`` drives — ``accept`` /
    ``receive`` (ASGI-style ``{"type": ...}`` dicts) / ``send_text`` /
    ``send_bytes`` / ``send_json`` — so the registry handler runs unchanged
    against either a FastAPI ``WebSocket`` or this bridge."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def accept(self, subprotocol=None) -> None:
        return None  # the bridge is already accepted on the upstream side

    async def receive(self) -> dict:
        import websockets

        try:
            raw = await self._ws.recv()
        except websockets.ConnectionClosed:
            return {"type": "websocket.disconnect"}
        if isinstance(raw, (bytes, bytearray)):
            return {"type": "websocket.receive", "bytes": bytes(raw)}
        return {"type": "websocket.receive", "text": raw}

    async def receive_bytes(self) -> bytes:
        while True:
            raw = await self._ws.recv()
            if isinstance(raw, (bytes, bytearray)):
                return bytes(raw)

    async def receive_text(self) -> str:
        while True:
            raw = await self._ws.recv()
            if isinstance(raw, str):
                return raw

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send(data)

    async def send_text(self, data: str) -> None:
        await self._ws.send(data)

    async def send_json(self, obj: Any) -> None:
        import json
        await self._ws.send(json.dumps(obj))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass


async def _run_stream_session(ctx: SessionContext) -> None:
    """Direct ``stream`` PTT/STT session.

    Opens the upstream bridge, wraps it so the existing
    ``run_stt_ws_session`` handler runs unchanged, and drives the per-user PTT
    loop. The caller identity (``init.as``) keys the per-user registry agent.
    """
    user_as = (ctx.init or {}).get("as") or "user:operator"
    try:
        bridge = await ctx.open_bridge()
    except Exception as exc:  # noqa: BLE001
        log.warning("stt stream bridge open failed: %s", exc)
        return
    adapter = _BridgeWs(bridge)
    try:
        await run_stt_ws_session(adapter, user_as)
    except Exception:  # noqa: BLE001
        log.exception("stt stream session crashed")
    finally:
        try:
            await bridge.close()
        except Exception:  # noqa: BLE001
            pass


SESSION_HANDLERS = {"stream": _run_stream_session}


# -- boot -------------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "stt",
        API_MANIFEST,
        HANDLERS,
        session_handlers=SESSION_HANDLERS,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
