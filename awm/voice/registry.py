"""Per-user voice-agent registry.

One ``VoiceAgent`` per identified user (bearer-token user-id). Each
agent owns a single persistent ``ClaudeSession`` subprocess and a set
of connected WebSocket clients. Events from the reader loop fan out to
every connected client (live cross-window sync); inputs from any
client drive the same shared session.

The registry persists agents across tab close/reload so conversation
context is preserved. An idle reaper drops agents whose last client
disconnected more than ``IDLE_TIMEOUT_SEC`` ago.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import WebSocket

from awm.voice.session import ClaudeSession
from awm.voice.stt import get_transcriber
from awm.voice.text_clean import clean_for_tts
from awm.voice.tts import get_synthesizer


log = logging.getLogger("awm.voice")


IDLE_TIMEOUT_SEC = int(__import__("os").environ.get("VOICE_IDLE_SEC", "1800"))


_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?\n]+|\Z", re.DOTALL)


def _split_sentences(buf: str) -> tuple[list[str], str]:
    out: list[str] = []
    pos = 0
    for m in _SENTENCE_RE.finditer(buf):
        chunk = m.group(0)
        if not chunk:
            continue
        if m.end() == m.start():
            continue
        if not re.search(r"[.!?\n]", chunk):
            break
        out.append(chunk.strip())
        pos = m.end()
    tail = buf[pos:]
    return out, tail


@dataclass
class TurnState:
    turn_id: int
    cancelled: bool = False
    text_buffer: str = ""
    first_token_ts: Optional[float] = None
    first_audio_ts: Optional[float] = None


class VoiceAgent:
    """Per-user state. One ClaudeSession, many WS clients."""

    def __init__(self, user_id: str, log_dir: Path):
        self.user_id = user_id
        self.log_dir = log_dir
        self.session = ClaudeSession(
            log_path=log_dir / f"voice-{_safe(user_id)}.claude.log",
        )
        self.clients: set[WebSocket] = set()
        self.joined_rooms: set[str] = set()
        self.pcm_chunks: list[bytes] = []
        self.recording_client: Optional[WebSocket] = None
        self.turn: Optional[TurnState] = None
        self.turn_seq = 0
        self.stt = get_transcriber()
        self.tts = get_synthesizer()
        self.reader_task: Optional[asyncio.Task] = None
        self.send_lock = asyncio.Lock()
        self.last_active = time.monotonic()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self.session.start()
        self.reader_task = asyncio.create_task(self._reader_loop())
        self._started = True

    async def stop(self) -> None:
        if self.reader_task is not None:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self.reader_task = None
        await self.session.stop()
        self._started = False

    # ---- client management ----

    async def attach(self, ws: WebSocket) -> None:
        self.clients.add(ws)
        self.last_active = time.monotonic()
        await self._send_one(ws, {"type": "ready", "user": self.user_id})
        await self._send_one(ws, {
            "type": "joined_rooms",
            "rooms": sorted(self.joined_rooms),
        })

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

    async def broadcast_binary(self, data: bytes, meta: dict) -> None:
        """Send a JSON prefix and then the binary frame to all clients."""
        async with self.send_lock:
            text = json.dumps(meta)
            dead: list[WebSocket] = []
            for ws in list(self.clients):
                try:
                    await ws.send_text(text)
                    await ws.send_bytes(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)

    async def _status(self, stage: str, text: str = "") -> None:
        await self.broadcast_json({"type": "status", "stage": stage, "text": text})

    # ---- input handlers (any client) ----

    async def handle_start(self, ws: WebSocket) -> None:
        # Latest "start" wins — barge-in across clients.
        self.pcm_chunks.clear()
        self.recording_client = ws
        if self.turn is not None:
            self.turn.cancelled = True
        self.last_active = time.monotonic()
        await self._status("listening", "listening…")

    async def handle_end(self, ws: WebSocket) -> None:
        if self.recording_client is not ws:
            return
        self.recording_client = None
        if not self.pcm_chunks:
            await self._status("idle", "no audio captured")
            return
        pcm = b"".join(self.pcm_chunks)
        self.pcm_chunks.clear()
        await self._status(
            "audio_received",
            f"audio received ({len(pcm) // 2} samples, {len(pcm) / 2 / 16000:.1f}s)",
        )
        await self._status("transcribing", "transcribing…")

        dump_path = self.log_dir / f"voice-{_safe(self.user_id)}.last.wav"
        try:
            with wave.open(str(dump_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm)
        except Exception:  # noqa: BLE001
            log.exception("pcm dump failed")

        t0 = time.monotonic()
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(
            None, self.stt.transcribe, pcm, 16000
        )
        stt_ms = int((time.monotonic() - t0) * 1000)
        log.info("STT (%dms) for %s: %r", stt_ms, self.user_id, text)
        await self.broadcast_json({"type": "latency", "stage": "stt", "ms": stt_ms})
        if not text:
            await self.broadcast_json({"type": "transcript", "text": ""})
            await self._status("idle", "nothing transcribed")
            return
        await self.broadcast_json({"type": "transcript", "text": text})

        self.turn_seq += 1
        self.turn = TurnState(self.turn_seq)
        await self._status("thinking", "claude thinking…")
        await self.session.send(text)
        self.last_active = time.monotonic()

    async def handle_cancel(self) -> None:
        if self.turn is not None:
            self.turn.cancelled = True

    async def handle_text(self, body: str) -> None:
        """Direct typed input (no STT pass) — useful for tab-based testing."""
        if not body.strip():
            return
        await self.broadcast_json({"type": "transcript", "text": body})
        self.turn_seq += 1
        self.turn = TurnState(self.turn_seq)
        await self._status("thinking", "claude thinking…")
        await self.session.send(body)
        self.last_active = time.monotonic()

    async def add_audio(self, ws: WebSocket, data: bytes) -> None:
        # Accept audio only from the currently-recording client.
        if self.recording_client is ws:
            self.pcm_chunks.append(data)

    # ---- reader loop (one per agent) ----

    async def _reader_loop(self) -> None:
        try:
            async for kind, body in self.session.events():
                turn = self.turn
                if kind == "text":
                    if turn is None:
                        continue
                    if turn.first_token_ts is None:
                        turn.first_token_ts = time.monotonic()
                        await self.broadcast_json({
                            "type": "latency",
                            "stage": "first_token",
                            "ms": 0,
                        })
                        await self._status("responding", "claude responding…")
                    await self.broadcast_json({"type": "agent_text", "delta": body})
                    if turn.cancelled:
                        continue
                    turn.text_buffer += body
                    sentences, tail = _split_sentences(turn.text_buffer)
                    turn.text_buffer = tail
                    for sent in sentences:
                        if turn.cancelled:
                            break
                        await self._speak(turn, sent)
                elif kind == "show":
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:
                        payload = {"content": body, "kind": "text"}
                    await self.broadcast_json({
                        "type": "show",
                        "content": payload.get("content", ""),
                        "kind": payload.get("kind", "text"),
                    })
                elif kind == "tool_use":
                    await self.broadcast_json({"type": "tool_use", "body": body})
                    await self._status("tool", f"tool call: {body}")
                elif kind == "tool_result":
                    await self.broadcast_json({"type": "tool_result", "body": body})
                elif kind == "result":
                    if turn is not None and turn.text_buffer.strip() and not turn.cancelled:
                        await self._speak(turn, turn.text_buffer.strip())
                        turn.text_buffer = ""
                    await self.broadcast_json({"type": "agent_turn_end"})
                    await self._status("idle", "")
                    self.turn = None
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("voice reader loop failed for %s", self.user_id)
            try:
                await self.broadcast_json({"type": "error", "message": str(exc)})
            except Exception:
                pass

    async def _speak(self, turn: TurnState, text: str) -> None:
        if turn.cancelled:
            return
        cleaned = clean_for_tts(text)
        if not cleaned:
            return
        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        pcm = await loop.run_in_executor(None, self.tts.synth, cleaned)
        if turn.cancelled or not pcm:
            return
        if turn.first_audio_ts is None:
            turn.first_audio_ts = time.monotonic()
            await self.broadcast_json({
                "type": "latency",
                "stage": "first_audio",
                "ms": int((turn.first_audio_ts - t0) * 1000),
            })
            await self._status("speaking", "speaking…")
        await self.broadcast_binary(
            pcm,
            {"type": "audio", "sample_rate": self.tts.sample_rate},
        )


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:64]


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
            if not agent._started:
                await agent.start()
            return agent

    async def attach(self, agent: VoiceAgent, ws: WebSocket) -> None:
        await agent.attach(ws)

    def detach(self, agent: VoiceAgent, ws: WebSocket) -> None:
        agent.detach(ws)

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper_task = None
        async with self._lock:
            agents = list(self._agents.values())
            self._agents.clear()
        for agent in agents:
            try:
                await agent.stop()
            except Exception:  # noqa: BLE001
                log.exception("error stopping agent %s", agent.user_id)

    def start_reaper(self) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(60)
                now = time.monotonic()
                async with self._lock:
                    to_drop = [
                        uid for uid, a in self._agents.items() if a.is_idle(now)
                    ]
                    drops = [self._agents.pop(uid) for uid in to_drop]
                for agent in drops:
                    log.info("reaping idle voice agent: %s", agent.user_id)
                    try:
                        await agent.stop()
                    except Exception:  # noqa: BLE001
                        log.exception("error reaping %s", agent.user_id)
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
