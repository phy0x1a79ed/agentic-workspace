"""Hub adapter for the ptt service.

Mirrors tts/backend/hub_adapter.py shape. Exposes one direct session
kind (``stream``) that bridges to the existing PTT session handler in
``backend.registry.run_ptt_ws_session``, plus one RPC function
(``transcribe``) for the HTTP-fallback path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import sys

import httpx
import websockets

log = logging.getLogger("ptt.hub_adapter")

API_MANIFEST: dict = {
    "functions": [
        {"name": "transcribe"},
    ],
    "emitters": [],
    "sessions": [
        {"kind": "stream", "transport": "direct"},
    ],
}


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _register(hub_url: str, token: str, name: str) -> dict:
    payload = {
        "name": name,
        "prefix": f"/svc/{name}",
        "pid": os.getpid(),
        "start": ["bash", "start.sh"],
        "cwd": os.getcwd(),
    }
    async with httpx.AsyncClient(verify=False, timeout=15) as cli:
        r = await cli.post(
            f"{hub_url}/hub/service/register",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
    return r.json()


async def _dispatch_call(fn: str, args) -> object:
    if fn == "transcribe":
        from backend.stt import get_transcriber
        pcm_b64 = args.get("pcm_b64") if isinstance(args, dict) else None
        if not pcm_b64:
            raise RuntimeError("pcm_b64 required")
        import base64
        pcm = base64.b64decode(pcm_b64)
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(
            None, get_transcriber().transcribe, pcm, 16000,
        )
        return {"text": text or ""}
    raise RuntimeError(f"unknown function {fn!r}")


async def _open_bridge(hub_url: str, token: str, sid: str, bid: str):
    ws_base = hub_url.replace("https://", "wss://").replace("http://", "ws://")
    return await websockets.connect(
        f"{ws_base}/hub/service/bridge/{sid}/{bid}",
        subprotocols=[f"bearer.{token}"],
        ssl=_ssl_ctx() if ws_base.startswith("wss://") else None,
        max_size=None,
        open_timeout=10,
    )


class _BridgeWs:
    """Minimal adapter exposing send_bytes / send_text / receive_bytes
    / send_json on top of a websockets.connect() object, so the existing
    backend.registry.run_ptt_ws_session handler (which expects a FastAPI
    WebSocket) runs unchanged."""

    def __init__(self, ws):
        self._ws = ws
        self._headers = {"x-awm-as": os.environ.get("AWM_HUB_USER", "")}

    @property
    def headers(self):
        return self._headers

    async def accept(self, subprotocol=None):
        return None  # bridge already accepted on the upstream side

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

    async def receive(self) -> dict:
        try:
            raw = await self._ws.recv()
        except websockets.ConnectionClosed:
            return {"type": "websocket.disconnect"}
        if isinstance(raw, (bytes, bytearray)):
            return {"type": "websocket.receive", "bytes": bytes(raw)}
        return {"type": "websocket.receive", "text": raw}

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send(data)

    async def send_text(self, data: str) -> None:
        await self._ws.send(data)

    async def send_json(self, obj) -> None:
        await self._ws.send(json.dumps(obj))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self._ws.close()
        except Exception:
            pass


async def _run_stream_session(hub_url: str, token: str, sid: str,
                              session_id: str, bridge_id: str,
                              as_: str) -> None:
    bridge = await _open_bridge(hub_url, token, sid, bridge_id)
    try:
        from backend.registry import run_ptt_ws_session
        adapter = _BridgeWs(bridge)
        await run_ptt_ws_session(adapter, as_ or "anonymous")
    except websockets.WebSocketException:
        pass
    except Exception:
        log.exception("ptt stream session crashed")
    finally:
        try:
            await bridge.close()
        except Exception:
            pass


async def _serve(hub_url: str, token: str, name: str, sid: str) -> None:
    ws_base = hub_url.replace("https://", "wss://").replace("http://", "ws://")
    async with websockets.connect(
        f"{ws_base}/hub/service/control/{sid}",
        subprotocols=[f"bearer.{token}"],
        ssl=_ssl_ctx() if ws_base.startswith("wss://") else None,
        max_size=None,
        open_timeout=10,
    ) as ws:
        await ws.send(json.dumps({"kind": "ready", "api": API_MANIFEST}))
        log.info("ptt hub_adapter: control WS open, ready sent")

        async for raw in ws:
            try:
                env = json.loads(raw) if isinstance(raw, str) else None
            except json.JSONDecodeError:
                continue
            if env is None:
                continue
            kind = env.get("kind")
            if kind == "call":
                async def _reply(env=env):
                    try:
                        result = await _dispatch_call(env.get("fn"), env.get("args") or {})
                        await ws.send(json.dumps({
                            "kind": "reply", "id": env.get("id"),
                            "ok": True, "result": result,
                        }))
                    except Exception as exc:
                        await ws.send(json.dumps({
                            "kind": "reply", "id": env.get("id"),
                            "ok": False, "error": str(exc),
                        }))
                asyncio.create_task(_reply())
            elif kind == "session.open":
                async def _open(env=env):
                    try:
                        await ws.send(json.dumps({
                            "kind": "session.opened",
                            "session_id": env.get("session_id"),
                            "ok": True,
                        }))
                        await _run_stream_session(
                            hub_url, token, sid,
                            env.get("session_id"),
                            env.get("bridge_id"),
                            env.get("as") or "",
                        )
                    except Exception as exc:
                        log.warning("session open failed: %s", exc)
                        try:
                            await ws.send(json.dumps({
                                "kind": "session.opened",
                                "session_id": env.get("session_id"),
                                "ok": False, "error": str(exc),
                            }))
                        except Exception:
                            pass
                asyncio.create_task(_open())


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    hub_url = os.environ.get("AWM_HUB_URL", "").rstrip("/")
    token = os.environ.get("AWM_HUB_TOKEN", "")
    name = os.environ.get("AWM_SERVICE_NAME", "ptt")
    sid = os.environ.get("AWM_SERVICE_ID", "")
    if not hub_url or not token:
        log.error("AWM_HUB_URL / AWM_HUB_TOKEN not set; cannot run")
        sys.exit(2)
    backoff = 1.0
    while True:
        try:
            if not sid:
                body = await _register(hub_url, token, name)
                sid = body["service_id"]
                log.info("registered: service_id=%s", sid)
            await _serve(hub_url, token, name, sid)
            backoff = 1.0
        except Exception as exc:
            log.warning("control WS lost (%s); retry in %.1fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


if __name__ == "__main__":
    asyncio.run(main())
