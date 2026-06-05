"""Per-call WS state machine.

Lifted from demo/server.py's Conn class, but parameterized by:
- a CallConfig (which STT + TTS + LLM engine to use)
- an LLM binding (where the LLM lives — today only InlineBinding works)

The orchestrator never imports awm-core. It talks only to engines
returned by `voice.engines` and to the binding it was handed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import wave
from pathlib import Path

from fastapi import WebSocket

from voice import engines as engines_registry
from voice.config import CallConfig
from voice.llm_binding import AwmRoomBinding, InlineBinding

from text_clean import clean_for_tts

log = logging.getLogger("voice.orchestrator")


_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?\n]+|\Z", re.DOTALL)


def _pretty(text: str) -> str:
    s = text.strip()
    if not s:
        return s
    if s == s.upper() and any(c.isalpha() for c in s):
        s = s.lower()
        return s[:1].upper() + s[1:]
    return s


def _split_sentences(buf: str) -> tuple[list[str], str]:
    out: list[str] = []
    pos = 0
    for m in _SENTENCE_RE.finditer(buf):
        chunk = m.group(0)
        if not chunk or m.end() == m.start():
            continue
        if not re.search(r"[.!?\n]", chunk):
            break
        out.append(chunk.strip())
        pos = m.end()
    return out, buf[pos:]


class TurnState:
    def __init__(self, turn_id: int):
        self.turn_id = turn_id
        self.cancelled = False
        self.text_buffer = ""
        self.first_token_ts: float | None = None
        self.first_audio_ts: float | None = None


class Conn:
    """One voice call. Bound to one WebSocket."""

    def __init__(
        self,
        ws: WebSocket,
        config: CallConfig,
        binding: InlineBinding | AwmRoomBinding,
    ):
        self.ws = ws
        self.config = config
        self.binding = binding

        self.stt = engines_registry.make_stt(config.stt)
        self.tts = engines_registry.make_tts(config.tts)

        if isinstance(binding, AwmRoomBinding):
            # The /call/start gate should have blocked this with a 501;
            # if we got here, fail loud.
            raise NotImplementedError(
                "awm_room binding not supported by this version of voice/service"
            )
        self.session = binding.connect()

        self.pcm_chunks: list[bytes] = []
        self.recording = False
        self.turn: TurnState | None = None
        self.turn_seq = 0
        self.stt_session = None
        self._last_partial = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self.reader_task: asyncio.Task | None = None
        self.send_lock = asyncio.Lock()
        self._session_error: str | None = None

    # ---------- WS helpers ----------

    async def send_json(self, payload: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(payload))

    async def send_binary(self, data: bytes) -> None:
        async with self.send_lock:
            await self.ws.send_bytes(data)

    async def _status(self, stage: str, text: str = "") -> None:
        await self.send_json({"type": "status", "stage": stage, "text": text})

    # ---------- lifecycle ----------

    async def start(self) -> None:
        try:
            await self.session.start()
        except Exception as exc:
            # Don't crash the WS — surface as an error frame and keep the
            # socket open so the user can see what went wrong, swap the
            # binding, and apply&reconnect. STT (and text input) still
            # work; the LLM turn just fails fast with a clean message.
            log.exception("session.start() failed (engine=%s)", self.binding.engine if hasattr(self.binding, "engine") else "?")
            self._session_error = f"{type(exc).__name__}: {exc}"
            await self.send_json({"type": "error", "message": f"llm session failed: {self._session_error}"})
        else:
            self.reader_task = asyncio.create_task(self._reader_loop())
        await self.send_json({
            "type": "ready",
            "config": {
                "stt": self.config.stt.model_dump(),
                "tts": self.config.tts.model_dump(),
                "llm": self.config.llm.model_dump() if self.config.llm else None,
            },
            "binding": self.binding.model_dump(),
            "session_ok": self._session_error is None,
            "session_error": self._session_error,
        })

    async def stop(self) -> None:
        if self.reader_task is not None:
            self.reader_task.cancel()
        if self.session is not None:
            try:
                await self.session.stop()
            except Exception:
                log.exception("session.stop() failed")

    # ---------- input handlers ----------

    async def handle_start(self) -> None:
        self.pcm_chunks.clear()
        self._last_partial = ""
        self._loop = asyncio.get_running_loop()
        if getattr(self.stt, "streaming", False):
            on_partial = self._partial_from_thread if getattr(self.stt, "push_partials", False) else None
            self.stt_session = self.stt.new_session(on_partial=on_partial)
        self.recording = True
        if self.turn is not None:
            self.turn.cancelled = True
        await self._status("listening", "listening…")

    def _partial_from_thread(self, text: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._enqueue_partial, text)

    def _enqueue_partial(self, text: str) -> None:
        if text == self._last_partial:
            return
        self._last_partial = text
        asyncio.create_task(self.send_json({"type": "stt_partial", "text": _pretty(text)}))

    async def add_audio(self, data: bytes) -> None:
        if not self.recording:
            return
        self.pcm_chunks.append(data)
        sess = self.stt_session
        if sess is None:
            return
        # sherpa-style: synchronous accept returns partial inline.
        # whisper-stream: accept() is fire-and-forget; partials come via callback.
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, sess.accept, data)
        if isinstance(result, str) and result and result != self._last_partial:
            self._last_partial = result
            await self.send_json({"type": "stt_partial", "text": _pretty(result)})

    async def handle_end(self) -> None:
        self.recording = False
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

        # Dump captured PCM for diagnosis (matches existing demo behavior).
        dump_path = Path(__file__).parent.parent / "demo" / "last_utterance.wav"
        try:
            with wave.open(str(dump_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(pcm)
            import numpy as _np
            arr = _np.frombuffer(pcm, dtype=_np.int16)
            rms = float(_np.sqrt(_np.mean((arr.astype(_np.float32)) ** 2)))
            peak = int(_np.max(_np.abs(arr))) if arr.size else 0
            log.info(
                "utterance: %d samples (%.2fs) rms=%.1f peak=%d → %s",
                arr.size, arr.size / 16000, rms, peak, dump_path,
            )
        except Exception as exc:
            log.warning("pcm dump failed: %s", exc)

        t0 = time.monotonic()
        loop = asyncio.get_running_loop()
        if self.stt_session is not None:
            sess, self.stt_session = self.stt_session, None
            text = await loop.run_in_executor(None, sess.finalize)
            text = _pretty(text)
        else:
            text = await loop.run_in_executor(None, self.stt.transcribe, pcm, 16000)
        stt_ms = int((time.monotonic() - t0) * 1000)
        log.info("STT [%s] (%dms): %r", self.config.stt.engine, stt_ms, text)
        await self.send_json({"type": "latency", "stage": "stt", "ms": stt_ms})
        if not text:
            await self.send_json({"type": "transcript", "text": ""})
            await self._status("idle", "nothing transcribed")
            return
        await self.send_json({"type": "transcript", "text": text})
        await self._kickoff_turn(text)

    async def handle_text(self, text: str) -> None:
        """Inject text as if STT had finalized it. Bypasses mic capture.

        Used by the dev SPA's text box to test the same phrase across
        TTS/LLM changes without re-recording.
        """
        text = _pretty((text or "").strip())
        if not text:
            return
        # Cancel any in-flight turn (mirrors PTT-start behaviour).
        if self.turn is not None:
            self.turn.cancelled = True
        await self.send_json({"type": "transcript", "text": text})
        await self._kickoff_turn(text)

    async def _kickoff_turn(self, text: str) -> None:
        if self._session_error is not None:
            await self.send_json({
                "type": "error",
                "message": f"llm not started: {self._session_error}",
            })
            await self._status("idle", "llm offline")
            return
        self.turn_seq += 1
        self.turn = TurnState(self.turn_seq)
        await self._status("thinking", "agent thinking…")
        await self.session.send(text)

    async def handle_cancel(self) -> None:
        if self.turn is not None:
            self.turn.cancelled = True

    async def handle_config(self, payload: dict) -> None:
        # Per-call config overrides — the most useful one historically is
        # toggling RVC on/off mid-call, which the current TTS-engine plugin
        # model handles by swapping engine instead. Echo an ack for parity
        # with the existing dev SPA.
        await self.send_json({"type": "config_ack", "payload": payload})

    # ---------- LLM event loop ----------

    async def _reader_loop(self) -> None:
        try:
            async for kind, body in self.session.events():
                turn = self.turn
                if kind == "text":
                    if turn is None:
                        continue
                    if turn.first_token_ts is None:
                        turn.first_token_ts = time.monotonic()
                        await self.send_json({"type": "latency", "stage": "first_token", "ms": 0})
                        await self._status("responding", "agent responding…")
                    await self.send_json({"type": "agent_text", "delta": body})
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
                    await self.send_json({
                        "type": "show",
                        "content": payload.get("content", ""),
                        "kind": payload.get("kind", "text"),
                    })
                elif kind == "tool_use":
                    await self.send_json({"type": "tool_use", "body": body})
                    await self._status("tool", f"tool call: {body}")
                elif kind == "tool_result":
                    await self.send_json({"type": "tool_result", "body": body})
                elif kind == "result":
                    if turn is not None and turn.text_buffer.strip() and not turn.cancelled:
                        await self._speak(turn, turn.text_buffer.strip())
                        turn.text_buffer = ""
                    await self.send_json({"type": "agent_turn_end"})
                    await self._status("idle", "")
                    self.turn = None
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.exception("reader loop failed")
            try:
                await self.send_json({"type": "error", "message": str(exc)})
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
        try:
            pcm = await loop.run_in_executor(None, self.tts.synth, cleaned)
        except Exception as exc:
            log.warning("TTS synth failed (engine=%s): %s", self.config.tts.engine, exc)
            await self.send_json({"type": "error", "message": f"tts: {exc}"})
            return
        if turn.cancelled or not pcm:
            return
        sample_rate = getattr(self.tts, "sample_rate", 24_000)
        if turn.first_audio_ts is None:
            turn.first_audio_ts = time.monotonic()
            await self.send_json({
                "type": "latency",
                "stage": "first_audio",
                "ms": int((turn.first_audio_ts - t0) * 1000),
            })
            await self._status("speaking", "speaking…")
        await self.send_json({"type": "audio", "sample_rate": sample_rate})
        await self.send_binary(pcm)
