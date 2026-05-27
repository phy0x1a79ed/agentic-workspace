"""Voice service FastAPI app.

Single HTTP API for both the dev SPA and any future caller.

- `GET /engines`         → {stt:[...], tts:[...], llm:[...]}
- `POST /call/start`     → {call_id, ws_url, expires_at}
- `WS  /call/{call_id}`  → per-call WebSocket (see orchestrator.Conn)
- `POST /call/{call_id}/end`
- `GET /`                → dev SPA

`POST /call/start` accepts:

    {
      "config": {
        "stt": {"engine": "...", "params": {...}},
        "tts": {"engine": "...", "params": {...}},
        "llm": {"engine": "...", "params": {...}}    # optional
      },
      "llm_binding": {
        "kind": "inline",
        "engine": "...", "params": {...}
      }
    }

`llm_binding.kind="awm_room"` is reserved (returns 501).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from voice import engines as engines_registry
from voice.config import CallConfig, default_config, from_env
from voice.llm_binding import AwmRoomBinding, InlineBinding, parse_binding
from voice.orchestrator import Conn

log = logging.getLogger("voice.service")

STATIC_DIR = Path(__file__).parent.parent / "demo" / "static"
NATIVE_EVAL_DIR = Path(__file__).parent.parent / "demo" / "voices" / "native-eval"
RVC_SAMPLES_DIR = Path(__file__).parent.parent / "demo" / "voices" / "rvc" / "samples"


CALL_TTL_SEC = 60  # client must claim the WS within this window
IDLE_TIMEOUT_SEC = 30 * 60  # reaper drops calls with no live WS after this


class PendingCall:
    """Reserved /call/start slot. Becomes a Conn once the WS attaches."""

    def __init__(self, config: CallConfig, binding: InlineBinding | AwmRoomBinding):
        self.config = config
        self.binding = binding
        self.created_at = time.monotonic()
        self.claimed = False


_calls: dict[str, PendingCall] = {}
_active: dict[str, Conn] = {}
_reaper_task: asyncio.Task | None = None


async def _reaper_loop() -> None:
    while True:
        try:
            await asyncio.sleep(15)
            now = time.monotonic()
            stale = [
                cid for cid, p in _calls.items()
                if not p.claimed and (now - p.created_at) > CALL_TTL_SEC
            ]
            for cid in stale:
                log.info("reaping unclaimed call %s", cid)
                _calls.pop(cid, None)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("reaper loop iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _reaper_task
    log.info("voice service starting; engines=%s", engines_registry.list_engine_ids())
    # Warm the default engines so the first call doesn't pay model-load latency.
    cfg = from_env(default_config())
    loop = asyncio.get_running_loop()
    try:
        stt = engines_registry.make_stt(cfg.stt)
        tts = engines_registry.make_tts(cfg.tts)
        log.info("warming default engines stt=%s tts=%s", cfg.stt.engine, cfg.tts.engine)
        await loop.run_in_executor(None, getattr(stt, "warmup", lambda: None))
        await loop.run_in_executor(None, getattr(tts, "warmup", lambda: None))
    except Exception:
        log.exception("default-engine warmup failed (non-fatal)")
    _reaper_task = asyncio.create_task(_reaper_loop())
    yield
    if _reaper_task:
        _reaper_task.cancel()


app = FastAPI(lifespan=lifespan)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
if NATIVE_EVAL_DIR.exists():
    app.mount("/native-eval", StaticFiles(directory=NATIVE_EVAL_DIR, html=True),
              name="native-eval")
if RVC_SAMPLES_DIR.exists():
    app.mount("/voices", StaticFiles(directory=RVC_SAMPLES_DIR, html=True),
              name="rvc-samples")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/engines")
async def list_engines() -> dict[str, dict[str, dict[str, Any]]]:
    return engines_registry.list_engines()


class CallStartRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    llm_binding: dict[str, Any] = Field(default_factory=dict)


@app.post("/call/start")
async def call_start(request: Request, body: CallStartRequest) -> dict[str, Any]:
    # 1. Resolve config: defaults → env → POST body overrides.
    base = from_env(default_config())
    try:
        cfg = base.merge(body.config) if body.config else base
    except Exception as exc:
        raise HTTPException(400, f"bad config: {exc}")

    # 2. Validate engine selections by trying to look them up.
    available = engines_registry.list_engine_ids()
    if cfg.stt.engine not in available["stt"]:
        raise HTTPException(400, f"unknown stt engine {cfg.stt.engine!r}; available: {available['stt']}")
    if cfg.tts.engine not in available["tts"]:
        raise HTTPException(400, f"unknown tts engine {cfg.tts.engine!r}; available: {available['tts']}")

    # 3. Parse llm_binding. Default to inline+config.llm if not provided.
    if body.llm_binding:
        try:
            binding = parse_binding(body.llm_binding)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    else:
        if cfg.llm is None:
            raise HTTPException(400, "must provide llm_binding or config.llm")
        binding = InlineBinding(engine=cfg.llm.engine, params=cfg.llm.params)

    # 4. awm_room is reserved — explicit 501 + future-pointer message.
    if isinstance(binding, AwmRoomBinding):
        raise HTTPException(
            status_code=501,
            detail=(
                "llm_binding.kind='awm_room' is reserved for a future plan. "
                "See voice/llm_binding.py:AwmRoomBinding for the seam. "
                "Implementations should add voice/engines/llm/awm_room.py "
                "and flip the NotImplementedError to instantiate it."
            ),
        )

    # 5. Mint call_id, reserve.
    call_id = secrets.token_urlsafe(12)
    _calls[call_id] = PendingCall(cfg, binding)
    ws_scheme = "wss" if request.url.scheme == "https" else "ws"
    ws_url = f"{ws_scheme}://{request.url.netloc}/call/{call_id}"
    return {
        "call_id": call_id,
        "ws_url": ws_url,
        "expires_at": time.time() + CALL_TTL_SEC,
    }


@app.post("/call/{call_id}/end")
async def call_end(call_id: str) -> dict[str, str]:
    pending = _calls.pop(call_id, None)
    conn = _active.pop(call_id, None)
    if conn is not None:
        await conn.stop()
        return {"status": "ended"}
    if pending is not None:
        return {"status": "unclaimed_cancelled"}
    raise HTTPException(404, f"call {call_id!r} not found")


@app.websocket("/call/{call_id}")
async def call_ws(ws: WebSocket, call_id: str) -> None:
    pending = _calls.pop(call_id, None)
    if pending is None:
        await ws.close(code=4404, reason="unknown or expired call_id")
        return
    pending.claimed = True
    await ws.accept()
    conn = Conn(ws, pending.config, pending.binding)
    _active[call_id] = conn
    try:
        await conn.start()
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"] is not None:
                await conn.add_audio(msg["bytes"])
            elif "text" in msg and msg["text"] is not None:
                import json
                try:
                    payload = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                t = payload.get("type")
                if t == "start":
                    await conn.handle_start()
                elif t == "end":
                    await conn.handle_end()
                elif t == "cancel":
                    await conn.handle_cancel()
                elif t == "config":
                    await conn.handle_config(payload)
                elif t == "text":
                    await conn.handle_text(payload.get("text", ""))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws handler crashed")
    finally:
        await conn.stop()
        _active.pop(call_id, None)


# ──────────────────────────────────────────────────────────────────────
# Back-compat shim: the existing dev SPA opens /ws directly without
# going through /call/start. Keep that working by minting a pending
# call from defaults+env on demand. The SPA upgrade lands in a parallel
# diff; both code paths can coexist.

@app.websocket("/ws")
async def legacy_ws(ws: WebSocket) -> None:
    cfg = from_env(default_config())
    if cfg.llm is None:
        await ws.close(code=4400, reason="no default llm configured")
        return
    binding = InlineBinding(engine=cfg.llm.engine, params=cfg.llm.params)
    await ws.accept()
    conn = Conn(ws, cfg, binding)
    call_id = uuid.uuid4().hex
    _active[call_id] = conn
    try:
        await conn.start()
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"] is not None:
                await conn.add_audio(msg["bytes"])
            elif "text" in msg and msg["text"] is not None:
                import json
                try:
                    payload = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                t = payload.get("type")
                if t == "start":
                    await conn.handle_start()
                elif t == "end":
                    await conn.handle_end()
                elif t == "cancel":
                    await conn.handle_cancel()
                elif t == "config":
                    await conn.handle_config(payload)
                elif t == "text":
                    await conn.handle_text(payload.get("text", ""))
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("legacy ws handler crashed")
    finally:
        await conn.stop()
        _active.pop(call_id, None)
