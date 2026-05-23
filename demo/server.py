"""FastAPI backend for the voice-conversation demo.

One WS connection = one claude subprocess + one STT/TTS pipeline. The
browser handles audio I/O; this server runs the offline models and the
claude CLI.

WS protocol (see plan):
  client → server
    binary  : 16-bit LE PCM, mono, 16kHz (raw frames between start/end)
    {"type":"start"}        push-to-talk down
    {"type":"end"}          push-to-talk up — triggers STT + claude turn
    {"type":"cancel"}       barge-in: stop emitting current TTS turn

  server → client
    {"type":"ready"}                       startup done
    {"type":"transcript","text":"..."}     after STT
    {"type":"agent_text","delta":"..."}    streaming claude text
    {"type":"agent_turn_end"}              result event from claude
    {"type":"audio","sample_rate":N}       prefix before a binary PCM blob
    binary                                 raw int16 PCM (mono, sample_rate per prefix)
    {"type":"latency","stage":"...","ms":N}
    {"type":"error","message":"..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import wave
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from contextlib import asynccontextmanager

from claude_session import ClaudeSession
from openrouter_session import OpenRouterSession
from rvc import get_rvc_client
from stt import get_transcriber
from stt_streaming import get_streaming_recognizer, get_whisper_streaming_recognizer
from text_clean import clean_for_tts


import os

# "whisper" — non-streaming faster-whisper, transcribe on PTT release (most accurate; default)
# "sherpa" — sherpa-onnx streaming zipformer (fast partials, lower accuracy on conversational)
# "whisper-stream" — rolling-window faster-whisper (partials are slow on CPU; see README caveat)
STT_BACKEND = os.environ.get("STT_BACKEND", "whisper").lower()

# "piper" — fast neural TTS, clean low-prosody output (good RVC feedstock; default)
# "pocket" — kyutai pocket-tts, more natural but slower
TTS_BACKEND = os.environ.get("TTS_BACKEND", "piper").lower()

# "openrouter" — OpenRouter chat completions (free models by default; needs OPENROUTER_API_KEY)
# "claude" — persistent `claude` CLI subprocess
LLM_BACKEND = os.environ.get("LLM_BACKEND", "openrouter").lower()


def _new_llm_session(log_path: Path):
    if LLM_BACKEND == "claude":
        s = ClaudeSession()
        s.log_path = log_path
        return s
    if LLM_BACKEND == "openrouter":
        return OpenRouterSession(log_path=log_path)
    raise ValueError(f"unknown LLM_BACKEND={LLM_BACKEND!r}")


def _load_tts():
    if TTS_BACKEND == "pocket":
        from tts import get_synthesizer
        return get_synthesizer()
    if TTS_BACKEND == "piper":
        from piper_tts import get_piper_synthesizer
        return get_piper_synthesizer()
    raise ValueError(f"unknown TTS_BACKEND={TTS_BACKEND!r}")


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice-demo")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    log.info("loading TTS + STT models (tts=%s, stt=%s)…", TTS_BACKEND, STT_BACKEND)
    t0 = time.monotonic()
    await loop.run_in_executor(None, _load_tts()._ensure_loaded)
    # Best-effort probe of the RVC sidecar — non-fatal if absent.
    await loop.run_in_executor(None, get_rvc_client().check)
    if STT_BACKEND == "sherpa":
        await loop.run_in_executor(None, get_streaming_recognizer()._ensure_loaded)
    elif STT_BACKEND == "whisper-stream":
        await loop.run_in_executor(None, get_whisper_streaming_recognizer()._ensure_loaded)
    else:
        await loop.run_in_executor(None, get_transcriber()._ensure_loaded)
    log.info("models ready in %.1fs", time.monotonic() - t0)
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# Sentence boundary — split on terminal punctuation followed by whitespace
# or end-of-buffer. Keeps the punctuation with the sentence.
_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?\n]+|\Z", re.DOTALL)


def _pretty(text: str) -> str:
    """Normalize STT output. Sherpa zipformer emits ALL CAPS no punctuation;
    whisper emits already-cased text. Detect and only fix the former."""
    s = text.strip()
    if not s:
        return s
    if s == s.upper() and any(c.isalpha() for c in s):
        s = s.lower()
        return s[:1].upper() + s[1:]
    return s


def _split_sentences(buf: str) -> tuple[list[str], str]:
    """Return (complete_sentences, leftover_tail).

    A sentence is text up to and including [.!?\\n]. Anything trailing
    without a terminator stays in the buffer.
    """
    out: list[str] = []
    pos = 0
    for m in _SENTENCE_RE.finditer(buf):
        chunk = m.group(0)
        if not chunk:
            continue
        # The trailing empty \Z match shouldn't be emitted.
        if m.end() == m.start():
            continue
        # If this chunk has no terminator, it's the tail.
        if not re.search(r"[.!?\n]", chunk):
            break
        out.append(chunk.strip())
        pos = m.end()
    tail = buf[pos:]
    return out, tail


class TurnState:
    """Per-WS turn state. One TurnState lives for the duration of a single
    claude reply (between user utterance and the result event)."""

    def __init__(self, turn_id: int):
        self.turn_id = turn_id
        self.cancelled = False
        self.text_buffer = ""
        self.first_token_ts: float | None = None
        self.first_audio_ts: float | None = None


class Conn:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        # Concrete session is built in start() once we know the log path.
        self.session = None  # type: ignore[assignment]
        self.pcm_chunks: list[bytes] = []
        self.recording = False
        self.turn: TurnState | None = None
        self.turn_seq = 0
        self.tts = _load_tts()
        self.rvc = get_rvc_client()
        self.rvc_enabled = False
        self.stt = get_transcriber()
        if STT_BACKEND == "sherpa":
            self.stt_stream = get_streaming_recognizer()
        elif STT_BACKEND == "whisper-stream":
            self.stt_stream = get_whisper_streaming_recognizer()
        else:
            self.stt_stream = None
        self.stt_session = None
        self._last_partial = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self.reader_task: asyncio.Task | None = None
        self.send_lock = asyncio.Lock()  # serialize WS sends (binary + json)

    async def send_json(self, payload: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(payload))

    async def send_binary(self, data: bytes) -> None:
        async with self.send_lock:
            await self.ws.send_bytes(data)

    async def start(self) -> None:
        log_path = Path(__file__).parent / (
            "claude.log" if LLM_BACKEND == "claude" else "llm.log"
        )
        self.session = _new_llm_session(log_path)
        await self.session.start()
        self.reader_task = asyncio.create_task(self._reader_loop())
        await self.send_json({
            "type": "ready",
            "rvc_available": self.rvc.available,
            "llm_backend": LLM_BACKEND,
        })

    async def stop(self) -> None:
        if self.reader_task is not None:
            self.reader_task.cancel()
        if self.session is not None:
            await self.session.stop()

    async def _status(self, stage: str, text: str = "") -> None:
        await self.send_json({"type": "status", "stage": stage, "text": text})

    async def handle_start(self) -> None:
        self.pcm_chunks.clear()
        self._last_partial = ""
        self._loop = asyncio.get_running_loop()
        if STT_BACKEND == "sherpa":
            self.stt_session = self.stt_stream.new_session()
        elif STT_BACKEND == "whisper-stream":
            self.stt_session = self.stt_stream.new_session(on_partial=self._partial_from_thread)
            self.stt_session.start()
        self.recording = True
        # Cancel any in-flight TTS turn — user is barging in.
        if self.turn is not None:
            self.turn.cancelled = True
        await self._status("listening", "listening…")

    def _partial_from_thread(self, text: str) -> None:
        """Bridge worker-thread partial callbacks onto the asyncio loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._enqueue_partial, text)

    def _enqueue_partial(self, text: str) -> None:
        if text == self._last_partial:
            return
        self._last_partial = text
        asyncio.create_task(self.send_json({"type": "stt_partial", "text": _pretty(text)}))

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

        # Dump captured PCM for diagnosis.
        dump_path = Path(__file__).parent / "last_utterance.wav"
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
        except Exception as exc:  # noqa: BLE001
            log.warning("pcm dump failed: %s", exc)

        t0 = time.monotonic()
        loop = asyncio.get_running_loop()
        if self.stt_session is not None:
            sess, self.stt_session = self.stt_session, None
            text = await loop.run_in_executor(None, sess.finalize)
            text = _pretty(text)
        else:
            text = await loop.run_in_executor(
                None, self.stt.transcribe, pcm, 16000
            )
        stt_ms = int((time.monotonic() - t0) * 1000)
        log.info("STT [%s] (%dms): %r", STT_BACKEND, stt_ms, text)
        await self.send_json({"type": "latency", "stage": "stt", "ms": stt_ms})
        if not text:
            await self.send_json({"type": "transcript", "text": ""})
            await self._status("idle", "nothing transcribed")
            return
        await self.send_json({"type": "transcript", "text": text})

        # Start a new turn — reader_loop will route output here.
        self.turn_seq += 1
        self.turn = TurnState(self.turn_seq)
        await self._status("thinking", "claude thinking…")
        await self.session.send(text)

    async def handle_cancel(self) -> None:
        if self.turn is not None:
            self.turn.cancelled = True

    async def handle_config(self, payload: dict) -> None:
        if "rvc" in payload:
            want = bool(payload["rvc"])
            if want and not self.rvc.available:
                # Re-probe in case the sidecar came up after server start.
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.rvc.check)
            self.rvc_enabled = want and self.rvc.available
            await self.send_json({
                "type": "config_ack",
                "rvc": self.rvc_enabled,
                "rvc_available": self.rvc.available,
            })

    async def add_audio(self, data: bytes) -> None:
        if not self.recording:
            return
        self.pcm_chunks.append(data)
        sess = self.stt_session
        if sess is None:
            return
        if STT_BACKEND == "whisper-stream":
            # Buffer append is O(1) — worker thread handles decoding.
            sess.accept(data)
            return
        # Sherpa path: synchronous decode in executor, partials returned inline.
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, sess.accept, data)
        if text and text != self._last_partial:
            self._last_partial = text
            await self.send_json({"type": "stt_partial", "text": _pretty(text)})

    async def _reader_loop(self) -> None:
        try:
            async for kind, body in self.session.events():
                turn = self.turn
                if kind == "text":
                    if turn is None:
                        continue
                    if turn.first_token_ts is None:
                        turn.first_token_ts = time.monotonic()
                        await self.send_json({
                            "type": "latency",
                            "stage": "first_token",
                            "ms": 0,
                        })
                        await self._status("responding", "claude responding…")
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
                # "raw" ignored
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
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
        pcm = await loop.run_in_executor(None, self.tts.synth, cleaned)
        if turn.cancelled or not pcm:
            return
        sample_rate = self.tts.sample_rate
        if self.rvc_enabled:
            try:
                pcm = await loop.run_in_executor(
                    None, self.rvc.convert, pcm, sample_rate, 0
                )
                sample_rate = self.rvc.sample_rate
            except Exception as exc:  # noqa: BLE001
                log.warning("RVC convert failed, falling back to dry TTS: %s", exc)
        if turn.cancelled or not pcm:
            return
        if turn.first_audio_ts is None:
            turn.first_audio_ts = time.monotonic()
            await self.send_json({
                "type": "latency",
                "stage": "first_audio",
                "ms": int((turn.first_audio_ts - t0) * 1000),
            })
            await self._status("speaking", "speaking…")
        await self.send_json({
            "type": "audio",
            "sample_rate": sample_rate,
        })
        await self.send_binary(pcm)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    conn = Conn(ws)
    try:
        await conn.start()
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"] is not None:
                await conn.add_audio(msg["bytes"])
            elif "text" in msg and msg["text"] is not None:
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
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("ws handler crashed")
    finally:
        await conn.stop()
