"""Per-user voice/STT WebSocket registry.

One ``VoiceAgent`` per identified user (bearer-token user-id). Each
agent owns a set of WebSocket clients (one per browser tab) and the
in-flight PCM buffer for the currently-recording client.

There is no embedded Claude session anymore. The agent's only job is:

  1. Receive PCM chunks from the active recording client (PTT-down →
     binary frames → PTT-up).
  2. On PTT-up, run faster-whisper STT and broadcast a single
     ``{"type":"stt_result","text":"..."}`` frame to every connected
     client of that user. The SPA appends the text to the focus
     composer.
  3. Surface status pings (``recording``/``transcribing``/``idle``)
     so the side-panel status chip reflects mic state across tabs.

Idle agents (no connected clients for ``IDLE_TIMEOUT_SEC``) are reaped
to free resources; reconnects spin up a fresh one transparently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import wave
from pathlib import Path
from typing import Optional

from fastapi import WebSocket


log = logging.getLogger("awm.voice")


IDLE_TIMEOUT_SEC = int(os.environ.get("VOICE_IDLE_SEC", "1800"))


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:64]


class VoiceAgent:
    """Per-user PTT/STT state. Multi-tab fanned-out via ``clients``."""

    def __init__(self, user_id: str, log_dir: Path):
        self.user_id = user_id
        self.log_dir = log_dir
        self.clients: set[WebSocket] = set()
        self.recording_client: Optional[WebSocket] = None
        self.pcm_chunks: list[bytes] = []
        self.send_lock = asyncio.Lock()
        self.last_active = time.monotonic()

    # ---- client management ----

    async def attach(self, ws: WebSocket) -> None:
        self.clients.add(ws)
        self.last_active = time.monotonic()
        await self._send_one(ws, {"type": "ready", "user": self.user_id})

    def detach(self, ws: WebSocket) -> None:
        self.clients.discard(ws)
        if self.recording_client is ws:
            self.recording_client = None
            self.pcm_chunks.clear()
        self.last_active = time.monotonic()

    def is_idle(self, now: float) -> bool:
        return not self.clients and (now - self.last_active) > IDLE_TIMEOUT_SEC

    # ---- fan-out ----

    async def _send_one(self, ws: WebSocket, payload: dict) -> None:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            self.clients.discard(ws)

    async def broadcast_json(self, payload: dict) -> None:
        async with self.send_lock:
            text = json.dumps(payload)
            dead: list[WebSocket] = []
            for ws in list(self.clients):
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)

    async def _status(self, stage: str, text: str = "") -> None:
        await self.broadcast_json({"type": "status", "stage": stage, "text": text})

    # ---- input handlers ----

    async def handle_start(self, ws: WebSocket) -> None:
        # Latest "start" wins — barge-in across clients.
        self.pcm_chunks.clear()
        self.recording_client = ws
        self.last_active = time.monotonic()
        await self._status("recording", "recording…")

    async def add_audio(self, ws: WebSocket, data: bytes) -> None:
        if self.recording_client is ws:
            self.pcm_chunks.append(data)

    async def handle_cancel(self) -> None:
        self.recording_client = None
        self.pcm_chunks.clear()
        await self._status("idle", "")

    async def handle_end(self, ws: WebSocket) -> None:
        if self.recording_client is not ws:
            return
        self.recording_client = None
        if not self.pcm_chunks:
            await self._status("idle", "no audio captured")
            return
        pcm = b"".join(self.pcm_chunks)
        self.pcm_chunks.clear()

        # Dump for debugging (last PTT only).
        dump_path = self.log_dir / f"voice-{_safe(self.user_id)}.last.wav"
        try:
            with wave.open(str(dump_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm)
        except Exception:  # noqa: BLE001
            log.exception("pcm dump failed")

        await self._status("transcribing", "transcribing…")
        from awm.voice.stt import get_transcriber

        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        try:
            text = await loop.run_in_executor(
                None, get_transcriber().transcribe, pcm, 16000,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("STT failed for %s", self.user_id)
            await self.broadcast_json({"type": "error", "message": f"stt: {exc}"})
            await self._status("idle", "")
            return
        stt_ms = int((time.monotonic() - t0) * 1000)
        log.info("STT (%dms) for %s: %r", stt_ms, self.user_id, text)
        await self.broadcast_json({"type": "stt_result", "text": text or ""})
        await self._status("idle", "")
        self.last_active = time.monotonic()


class VoiceRegistry:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._agents: dict[str, VoiceAgent] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: Optional[asyncio.Task] = None

    async def get_or_create(self, user_id: str) -> VoiceAgent:
        async with self._lock:
            agent = self._agents.get(user_id)
            if agent is None:
                agent = VoiceAgent(user_id, self.log_dir)
                self._agents[user_id] = agent
            return agent

    async def attach(self, agent: VoiceAgent, ws: WebSocket) -> None:
        await agent.attach(ws)

    def detach(self, agent: VoiceAgent, ws: WebSocket) -> None:
        agent.detach(ws)

    def start_reaper(self) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reap_loop())

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper_task = None
        async with self._lock:
            self._agents.clear()

    async def _reap_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                now = time.monotonic()
                async with self._lock:
                    to_drop = [
                        uid for uid, a in self._agents.items() if a.is_idle(now)
                    ]
                    for uid in to_drop:
                        self._agents.pop(uid, None)
                for uid in to_drop:
                    log.info("reaping idle voice agent: %s", uid)
        except asyncio.CancelledError:
            return


_registry: Optional[VoiceRegistry] = None


def get_registry() -> VoiceRegistry:
    global _registry
    if _registry is None:
        from awm import config
        log_dir = Path(getattr(config, "AWM_DIR", Path.home() / ".awm")) / "voice"
        _registry = VoiceRegistry(log_dir)
    return _registry


async def run_voice_ws_session(websocket: WebSocket, user_as: str) -> None:
    """Drive a voice WS connection end-to-end.

    Owns the per-user agent acquisition, audio/text frame demux, and
    detach-on-exit. The API handler shrinks to ``auth + accept +
    delegate``.
    """
    from fastapi import WebSocketDisconnect

    reg = get_registry()
    agent = await reg.get_or_create(user_as)
    await reg.attach(agent, websocket)
    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"] is not None:
                await agent.add_audio(websocket, msg["bytes"])
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            t = payload.get("type")
            if t == "start":
                await agent.handle_start(websocket)
            elif t == "end":
                await agent.handle_end(websocket)
            elif t == "cancel":
                await agent.handle_cancel()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("voice ws handler crashed for %s", user_as)
    finally:
        reg.detach(agent, websocket)
