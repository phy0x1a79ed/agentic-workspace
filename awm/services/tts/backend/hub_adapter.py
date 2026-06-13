"""Hub adapter for the tts service.

Reads ``AWM_HUB_URL`` / ``AWM_HUB_TOKEN`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` from env. POSTs ``/hub/service/register`` to claim
``/svc/tts``, opens the control WS, sends a ``ready`` frame with the
api manifest, then dispatches inbound ``call`` / ``notify`` /
``session.open`` envelopes against the underlying service code.

Functions (REST equivalents):
  - listEngines       (was GET  /engines)
  - listPresets       (was GET  /presets)
  - savePreset        (was PUT  /presets/{name})
  - deletePreset      (was DELETE /presets/{name})
  - getAllState       (was GET  /state)
  - getState          (was GET  /state/{key})
  - setState          (was PUT  /state/{key})
  - delState          (was DELETE /state/{key})

Sessions:
  - kind="call", transport="direct" — bridge to the existing per-call
    handler at tts.backend.app.call_ws. PCM bytes go byte-relayed.

On control WS close, the adapter retries with exponential backoff so
hub restarts don't drop the service.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import sys
import time
from typing import Any

import httpx
import websockets

log = logging.getLogger("tts.hub_adapter")

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {"name": "listEngines"},
        {"name": "listPresets"},
        {"name": "savePreset"},
        {"name": "deletePreset"},
        {"name": "getAllState"},
        {"name": "getState"},
        {"name": "setState"},
        {"name": "delState"},
    ],
    "emitters": [],
    "sessions": [
        {"kind": "call", "transport": "direct"},
    ],
}


def _ssl_ctx() -> ssl.SSLContext | None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _register(hub_url: str, token: str, name: str, sid: str) -> dict:
    payload = {
        "name": name,
        "prefix": f"/svc/{name}",
        "pid": os.getpid(),
        "start": ["bash", "start.sh"],
        "cwd": os.getcwd(),
    }
    # Auth was ripped (loopback-only hub); only send a bearer header if a
    # token is actually present. An empty token yields the header value
    # "Bearer " which httpx rejects as an illegal header value.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(verify=False, timeout=15) as cli:
        r = await cli.post(
            f"{hub_url}/hub/service/register",
            json=payload,
            headers=headers,
        )
        r.raise_for_status()
    return r.json()


async def _dispatch_call(fn: str, args: Any) -> Any:
    """Run one function call. Imports are local so service start doesn't
    pay the cost when the function is never invoked."""
    if fn == "listEngines":
        from app import EXPOSED_TTS_ENGINES, _enrich_kokoro_rvc_enums
        from voice import engines as reg
        all_tts = reg.list_engines()["tts"]
        exposed = {eid: all_tts[eid] for eid in EXPOSED_TTS_ENGINES if eid in all_tts}
        if "kokoro_rvc" in exposed:
            await _enrich_kokoro_rvc_enums(exposed["kokoro_rvc"])
        return exposed
    if fn == "listPresets":
        import presets
        return {"presets": presets.list_presets(),
                "last_used": presets.get_last_used()}
    if fn == "savePreset":
        import presets
        name = args.get("name")
        presets.save_preset(name, args.get("engine"), args.get("params", {}))
        return None
    if fn == "deletePreset":
        import presets
        presets.delete_preset(args.get("name"))
        return None
    if fn == "getAllState":
        import state
        return state.list_state()
    if fn == "getState":
        import state
        v = state.get_state(args.get("key"))
        return {"value": v}
    if fn == "setState":
        import state
        state.set_state(args.get("key"), args.get("value"))
        return None
    if fn == "delState":
        import state
        state.delete_state(args.get("key"))
        return None
    raise RuntimeError(f"unknown function {fn!r}")


async def _open_bridge(hub_url: str, token: str, sid: str, bid: str) -> Any:
    ws_base = hub_url.replace("https://", "wss://").replace("http://", "ws://")
    return await websockets.connect(
        f"{ws_base}/hub/service/bridge/{sid}/{bid}",
        subprotocols=[f"bearer.{token}"] if token else None,
        ssl=_ssl_ctx() if ws_base.startswith("wss://") else None,
        max_size=None,
        open_timeout=10,
    )


async def _run_call_session(hub_url: str, token: str, sid: str,
                            session_id: str, bridge_id: str,
                            init: dict[str, Any]) -> None:
    """Open the upstream bridge and run the existing call_ws handler on it.

    The handler at tts.backend.app expects a FastAPI WebSocket; we adapt
    the websockets connection minimally — it already supports send_bytes/
    send_text/receive, and most engine code only calls those.
    """
    from app import _Call, _active, _handle_speak, _handle_reconfigure
    from voice import engines as reg
    import uuid

    engine_id = init.get("engine") or "piper"
    params = init.get("params") or {}
    try:
        info = reg.load("tts", engine_id, params)
    except Exception as exc:
        log.warning("session engine load failed: %s", exc)
        return
    instance = reg.current_instance("tts")
    if instance is None:
        log.warning("session: no current instance after load")
        return
    call = _Call(
        call_id=uuid.uuid4().hex,
        engine_id=info["id"],
        params=info["params"],
        instance=instance,
        expires_at=time.monotonic() + 60.0,
        claimed=True,
    )
    _active[call.call_id] = call

    bridge = await _open_bridge(hub_url, token, sid, bridge_id)
    try:
        while True:
            raw = await bridge.recv()
            if isinstance(raw, (bytes, bytearray)):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = payload.get("type")
            if kind == "speak":
                text = payload.get("text", "")
                pcm = await asyncio.get_running_loop().run_in_executor(
                    None, call.instance.synth, text)
                await bridge.send(pcm)
                await bridge.send(json.dumps({
                    "type": "done",
                    "sample_rate": getattr(call.instance, "sample_rate", 24000),
                }))
            elif kind == "reconfigure":
                eng = payload.get("engine") or call.engine_id
                params = payload.get("params") or {}
                try:
                    info2 = reg.load("tts", eng, params)
                    inst2 = reg.current_instance("tts")
                    call.engine_id = info2["id"]
                    call.params = info2["params"]
                    if inst2 is not None:
                        call.instance = inst2
                    await bridge.send(json.dumps({
                        "type": "reconfigured",
                        "engine": info2["id"],
                        "instance_id": info2["instance_id"],
                    }))
                except Exception as exc:
                    await bridge.send(json.dumps({
                        "type": "error", "message": str(exc),
                    }))
    except websockets.WebSocketException:
        pass
    finally:
        _active.pop(call.call_id, None)
        try:
            await bridge.close()
        except Exception:
            pass


async def _serve(hub_url: str, token: str, name: str, sid: str) -> None:
    ws_base = hub_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = f"{ws_base}/hub/service/control/{sid}"
    async with websockets.connect(
        ws_url,
        subprotocols=[f"bearer.{token}"] if token else None,
        ssl=_ssl_ctx() if ws_base.startswith("wss://") else None,
        max_size=None,
        open_timeout=10,
    ) as ws:
        await ws.send(json.dumps({"kind": "ready", "api": API_MANIFEST}))
        log.info("tts hub_adapter: control WS open, ready sent")

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
                            "kind": "reply",
                            "id": env.get("id"),
                            "ok": True,
                            "result": result,
                        }))
                    except Exception as exc:
                        await ws.send(json.dumps({
                            "kind": "reply",
                            "id": env.get("id"),
                            "ok": False,
                            "error": str(exc),
                        }))
                asyncio.create_task(_reply())
            elif kind == "notify":
                async def _fire(env=env):
                    try:
                        await _dispatch_call(env.get("fn"), env.get("args") or {})
                    except Exception as exc:
                        log.warning("notify %s failed: %s", env.get("fn"), exc)
                asyncio.create_task(_fire())
            elif kind == "session.open":
                async def _open(env=env):
                    try:
                        await ws.send(json.dumps({
                            "kind": "session.opened",
                            "session_id": env.get("session_id"),
                            "ok": True,
                        }))
                        await _run_call_session(
                            hub_url, token, sid,
                            env.get("session_id"),
                            env.get("bridge_id"),
                            env.get("init") or {},
                        )
                    except Exception as exc:
                        log.warning("session open failed: %s", exc)
                        try:
                            await ws.send(json.dumps({
                                "kind": "session.opened",
                                "session_id": env.get("session_id"),
                                "ok": False,
                                "error": str(exc),
                            }))
                        except Exception:
                            pass
                asyncio.create_task(_open())


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    hub_url = os.environ.get("AWM_HUB_URL", "").rstrip("/")
    token = os.environ.get("AWM_HUB_TOKEN", "")
    name = os.environ.get("AWM_SERVICE_NAME", "tts")
    sid = os.environ.get("AWM_SERVICE_ID", "")
    if not hub_url:
        log.error("AWM_HUB_URL not set; cannot run")
        sys.exit(2)

    backoff = 1.0
    while True:
        try:
            if not sid:
                body = await _register(hub_url, token, name, sid)
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
