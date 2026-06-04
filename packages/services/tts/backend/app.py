"""TTS-only call protocol.

Three peer-gated routes plus ``/healthz``:

* ``GET  /engines``                — JSON Schema + defaults per engine.
* ``POST /call/start``             — allocate a call bound to an engine.
* ``WS   /call/{call_id}``         — speak text in, PCM frames back.

The hub serves this under ``<prefix>/_api/`` after stripping the prefix
(see ``awm/exposed.py::_dispatch_stripe``). The frontend uses relative
URLs (``./_api/engines``) so the bundle is location-agnostic.

WS protocol:

  client → server (JSON text frames):
    {"type": "speak", "text": "..."}
    {"type": "reconfigure", "engine": "<id>", "params": {...}}

  server → client:
    bytes (PCM, raw little-endian int16 — engine's sample_rate)
    {"type": "done", "sample_rate": <hz>}
    {"type": "error", "message": "..."}
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

import httpx

from awm.middleware_auth import authenticate_websocket, require_peer_bearer
from .text_clean import clean_for_tts
from tts.backend import presets, state
from voice import engines as engines_registry

log = logging.getLogger("tts.backend")

# Engines we surface in the UI. The registry holds more (f5tts, gptsovits,
# pocket); they stay loadable from code, but the stripe UI only exposes
# the production-path set.
EXPOSED_TTS_ENGINES = ("kokoro_rvc", "piper", "sbv2")

# Per-call state. Calls are minted by /call/start and consumed by the
# matching WS. Unclaimed calls expire after CALL_TTL_S; the reaper
# evicts them so a slow client doesn't pin engine state.
CALL_TTL_S = 60.0


@dataclass
class _Call:
    call_id: str
    engine_id: str
    params: dict[str, Any]
    instance: Any
    expires_at: float
    claimed: bool = field(default=False)


_pending: dict[str, _Call] = {}
_active: dict[str, _Call] = {}
_lock = asyncio.Lock()


class CallStartRequest(BaseModel):
    engine: str = Field(..., description="TTS engine id (from /engines).")
    params: dict[str, Any] = Field(default_factory=dict)


class PresetBody(BaseModel):
    engine: str
    params: dict[str, Any] = Field(default_factory=dict)


class StateBody(BaseModel):
    value: Any


async def _reaper_loop() -> None:
    while True:
        await asyncio.sleep(5)
        now = time.monotonic()
        async with _lock:
            stale = [cid for cid, c in _pending.items() if c.expires_at < now]
            for cid in stale:
                log.info("evicting unclaimed call %s", cid)
                _pending.pop(cid, None)


def build_app() -> FastAPI:
    app = FastAPI(title="awm-tts")

    @app.on_event("startup")
    async def _startup() -> None:
        asyncio.create_task(_reaper_loop())
        try:
            presets.seed_builtin_presets()
        except Exception as exc:
            log.warning("preset seed skipped: %s", exc)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    router = APIRouter(tags=["tts"])

    @router.get("/engines", dependencies=[Depends(require_peer_bearer)])
    async def list_engines() -> dict[str, dict[str, Any]]:
        all_tts = engines_registry.list_engines()["tts"]
        exposed = {eid: all_tts[eid] for eid in EXPOSED_TTS_ENGINES if eid in all_tts}
        if "kokoro_rvc" in exposed:
            await _enrich_kokoro_rvc_enums(exposed["kokoro_rvc"])
        return exposed

    @router.post("/call/start", dependencies=[Depends(require_peer_bearer)])
    async def call_start(body: CallStartRequest) -> dict[str, Any]:
        if body.engine not in EXPOSED_TTS_ENGINES:
            raise HTTPException(
                400,
                f"engine {body.engine!r} not exposed; available: {list(EXPOSED_TTS_ENGINES)}",
            )
        try:
            info = engines_registry.load("tts", body.engine, body.params)
        except Exception as exc:
            raise HTTPException(400, f"engine load failed: {exc}") from exc
        instance = engines_registry.current_instance("tts")
        assert instance is not None
        try:
            presets.set_last_used(info["id"], info["params"])
        except Exception as exc:
            log.debug("set_last_used failed: %s", exc)
        call_id = uuid.uuid4().hex
        expires_at = time.monotonic() + CALL_TTL_S
        async with _lock:
            _pending[call_id] = _Call(
                call_id=call_id,
                engine_id=info["id"],
                params=info["params"],
                instance=instance,
                expires_at=expires_at,
            )
        # The frontend opens this relative to the page (./_api/call/<id>);
        # hub forwards <prefix>/_api/call/<id> → /call/<id> on us.
        return {
            "call_id": call_id,
            "ws_url": f"./_api/call/{call_id}",
            "expires_at": expires_at,
            "engine": info["id"],
            "instance_id": info["instance_id"],
        }

    @router.get("/presets", dependencies=[Depends(require_peer_bearer)])
    async def get_presets() -> dict[str, Any]:
        return {
            "presets": presets.list_presets(),
            "last_used": presets.get_last_used(),
        }

    @router.put("/presets/{name}", status_code=204, dependencies=[Depends(require_peer_bearer)])
    async def put_preset(name: str, body: PresetBody) -> None:
        if not name.strip() or len(name) > 64:
            raise HTTPException(400, "preset name must be 1–64 chars")
        try:
            presets.save_preset(name, body.engine, body.params)
        except presets.BuiltinConflict:
            raise HTTPException(409, f"{name!r} is a built-in preset and can't be overwritten")

    @router.delete("/presets/{name}", status_code=204, dependencies=[Depends(require_peer_bearer)])
    async def del_preset(name: str) -> None:
        try:
            presets.delete_preset(name)
        except presets.BuiltinConflict:
            raise HTTPException(409, f"{name!r} is a built-in preset and can't be deleted")

    @router.get("/state", dependencies=[Depends(require_peer_bearer)])
    async def get_all_state() -> dict[str, Any]:
        return state.list_state()

    @router.get("/state/{key}", dependencies=[Depends(require_peer_bearer)])
    async def get_one_state(key: str) -> dict[str, Any]:
        v = state.get_state(key)
        if v is None:
            raise HTTPException(404, f"state key {key!r} not set")
        return {"value": v}

    @router.put("/state/{key}", status_code=204, dependencies=[Depends(require_peer_bearer)])
    async def put_one_state(key: str, body: StateBody) -> None:
        if not key.strip() or len(key) > 64:
            raise HTTPException(400, "state key must be 1–64 chars")
        state.set_state(key, body.value)

    @router.delete("/state/{key}", status_code=204, dependencies=[Depends(require_peer_bearer)])
    async def del_one_state(key: str) -> None:
        state.delete_state(key)

    app.include_router(router)

    @app.websocket("/call/{call_id}")
    async def call_ws(ws: WebSocket, call_id: str) -> None:
        # Validate inbound auth — proxy already authenticated, but the
        # supervised process is reachable on localhost too.
        chosen_sub = await authenticate_websocket(ws)
        async with _lock:
            call = _pending.pop(call_id, None)
        if call is None:
            await ws.close(code=4404, reason="unknown or expired call_id")
            return
        call.claimed = True
        _active[call_id] = call
        await ws.accept(subprotocol=chosen_sub)
        try:
            while True:
                msg = await ws.receive()
                t = msg.get("type")
                if t == "websocket.disconnect":
                    break
                if "text" not in msg or msg["text"] is None:
                    continue
                import json
                try:
                    payload = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                kind = payload.get("type")
                if kind == "speak":
                    text = payload.get("text", "")
                    await _handle_speak(ws, call, text)
                elif kind == "reconfigure":
                    await _handle_reconfigure(ws, call, payload)
                else:
                    await ws.send_json({"type": "error", "message": f"unknown frame type {kind!r}"})
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("tts ws handler crashed")
        finally:
            _active.pop(call_id, None)

    return app


_voices_cache: dict[str, Any] = {"at": 0.0, "data": None}
_VOICES_TTL_S = 30.0


async def _fetch_sidecar_voices() -> dict[str, Any] | None:
    """Pull {tts:[...], rvc:[{label,...}]} from the kokoro_rvc sidecar.

    Cached briefly so /engines stays cheap on repeated UI mounts. Errors
    are swallowed — schema renders without enums and the user can still
    type a value freely.
    """
    now = time.monotonic()
    if _voices_cache["data"] is not None and now - _voices_cache["at"] < _VOICES_TTL_S:
        return _voices_cache["data"]
    # Sidecar URL + TLS verify are infra knobs read from env (mirrors
    # voice.engines.tts.kokoro_rvc) so they don't leak into the user-
    # facing config schema.
    url = os.environ.get("TTS_RVC_URL", "https://127.0.0.1:12123")
    verify_raw = os.environ.get("TTS_RVC_VERIFY_SSL", "").strip().lower()
    verify = verify_raw in {"1", "true", "yes", "on"}
    try:
        async with httpx.AsyncClient(verify=verify, timeout=2.0) as cli:
            r = await cli.get(f"{url}/voices")
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        log.info("sidecar /voices unreachable, schema enums skipped: %s", exc)
        return None
    _voices_cache["at"] = now
    _voices_cache["data"] = data
    return data


async def _enrich_kokoro_rvc_enums(entry: dict[str, Any]) -> None:
    voices = await _fetch_sidecar_voices()
    if not voices:
        return
    props = entry.get("schema", {}).get("properties") or {}
    tts_list = voices.get("tts") or []
    rvc_list = [v.get("label") for v in (voices.get("rvc") or []) if v.get("label")]
    if tts_list and "tts_voice" in props:
        props["tts_voice"]["enum"] = list(tts_list)
    if rvc_list and "rvc_label" in props:
        # rvc_label is Optional[str] → anyOf [str, null]. Stamping enum on
        # the outer field is what DynamicConfigForm's fieldEnum() reads.
        props["rvc_label"]["enum"] = list(rvc_list)


async def _handle_speak(ws: WebSocket, call: _Call, text: str) -> None:
    cleaned = clean_for_tts(text)
    loop = asyncio.get_running_loop()
    try:
        pcm = await loop.run_in_executor(None, call.instance.synth, cleaned)
    except Exception as exc:
        log.warning("tts synth failed (engine=%s): %s", call.engine_id, exc)
        await ws.send_json({"type": "error", "message": f"synth: {exc}"})
        return
    sample_rate = getattr(call.instance, "sample_rate", 24_000)
    # PCM is bytes already (raw int16 little-endian per engine convention).
    # Stream in one binary frame; chunked streaming is overkill for the
    # short-utterance UX we ship today.
    await ws.send_bytes(pcm)
    await ws.send_json({"type": "done", "sample_rate": sample_rate})


async def _handle_reconfigure(ws: WebSocket, call: _Call, payload: dict[str, Any]) -> None:
    engine_id = payload.get("engine") or call.engine_id
    params = payload.get("params") or {}
    available = engines_registry.list_engine_ids()["tts"]
    if engine_id not in available:
        await ws.send_json({"type": "error", "message": f"unknown engine {engine_id!r}"})
        return
    try:
        info = engines_registry.load("tts", engine_id, params)
    except Exception as exc:
        await ws.send_json({"type": "error", "message": f"engine load failed: {exc}"})
        return
    instance = engines_registry.current_instance("tts")
    assert instance is not None
    call.engine_id = info["id"]
    call.params = info["params"]
    call.instance = instance
    await ws.send_json({"type": "reconfigured", "engine": info["id"], "instance_id": info["instance_id"]})
