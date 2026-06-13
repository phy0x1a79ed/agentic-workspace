"""Per-user PTT/STT WebSocket registry.

Direct port of ``awm/voice/registry.py`` for the V2 svc — same wire
protocol, same singleton-Transcriber reuse via ``awm.voice.stt``. Once
parity is confirmed end-to-end, V1's ``awm/voice/router.py`` will delegate
to this module and the V1 registry/router can be removed.

Wire protocol:

  text frames up:
    ``{"type":"start", "mode":"ptt"|"continuous"?, "context":"..."?}``
                            begin recording (latest wins, barge-in). In
                            continuous mode the convo inner loop runs; an
                            optional ``context`` seeds its 2k chat-history buffer.
    ``{"type":"context", "text":"..."}``  update the convo context buffer
    ``{"type":"end"}``      finalize → whisper → broadcast stt_result
    ``{"type":"cancel"}``   drop the current buffer
  binary frames up: raw int16 LE 16 kHz mono PCM chunks from the worklet.

  broadcast down (to every tab of the same user):
    ``{"type":"ready", "user":"..."}``         on attach
    ``{"type":"status","stage":"recording"|"transcribing"|"idle", "text":"..."}``
    ``{"type":"partial","text":"..."}``        rolling STT while recording
    ``{"type":"stt_result","text":"..."}``     PTT: after whisper completes
    ``{"type":"composer","text":"..."}``       convo: LLM-cleaned message so far
    ``{"type":"submit","text":"..."}``         convo: message judged complete
    ``{"type":"error","message":"..."}``       on whisper failure

Partial streaming uses faster-whisper's per-segment timestamps to splice
the buffer: each pass transcribes only the audio after the last committed
segment, so cost is bounded by the unstable tail rather than the full
utterance. The final ``stt_result`` always runs a fresh end-to-end pass on
the complete PCM — committed/tail bookkeeping exists purely to keep the
mid-recording partials cheap.
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

import numpy as np
from fastapi import WebSocket


log = logging.getLogger("awm.services.ptt.registry")


IDLE_TIMEOUT_SEC = int(os.environ.get("PTT_IDLE_SEC", os.environ.get("VOICE_IDLE_SEC", "1800")))

# Partial-streaming cadence. 2.0s lets each whisper pass on the tail finish
# comfortably on small.en int8 CPU before the next one starts, and gives the
# user enough new audio per pass that segment boundaries actually appear (so
# the splicer commits forward rather than re-transcribing the same tail).
# Min-tail keeps us from running whisper on sub-word slivers.
PARTIAL_MIN_GAP_SEC = float(os.environ.get("PTT_PARTIAL_GAP", "2.0"))
PARTIAL_MIN_TAIL_SEC = float(os.environ.get("PTT_PARTIAL_MIN_TAIL", "0.4"))
# Continuous mode: number of consecutive empty-segment passes (i.e. tail
# is all silence according to whisper+silero-vad) that triggers committing
# the current utterance as an stt_result and resetting the splicing window.
SILENCE_PASSES = int(os.environ.get("PTT_SILENCE_PASSES", "2"))
SAMPLE_RATE = 16000
SAMPLE_BYTES = 2  # int16 LE


def _transcribe_segments(pcm_bytes: bytes, vad_filter: bool = False) -> list[tuple[str, float, float]]:
    """Run whisper on PCM, return ``[(text, start_s, end_s), ...]``.

    Reuses the vendored ``backend.stt`` singleton so the model is loaded
    once per process. The singleton's public ``.transcribe()`` joins and
    discards segment timestamps; we need them for splicing, so we drive
    ``model.transcribe(...)`` directly here.
    """
    from backend.stt import get_transcriber

    if not pcm_bytes:
        return []
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    model = get_transcriber()._ensure_loaded()
    segments, _info = model.transcribe(
        audio,
        language="en",
        beam_size=1,
        vad_filter=vad_filter,
    )
    return [(seg.text.strip(), seg.start, seg.end) for seg in segments]


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:64]


class PttAgent:
    """Per-user PTT/STT state. Multi-tab fanned-out via ``clients``."""

    def __init__(self, user_id: str, log_dir: Path):
        self.user_id = user_id
        self.log_dir = log_dir
        self.clients: set[WebSocket] = set()
        self.recording_client: Optional[WebSocket] = None
        self.pcm_chunks: list[bytes] = []
        self.send_lock = asyncio.Lock()
        self.last_active = time.monotonic()
        # Splicing state for rolling partial transcription. ``committed_text``
        # is the finalized prefix (all whisper segments that have already had
        # post-context); ``committed_bytes`` is the byte offset into the
        # concatenated PCM where the unstable tail begins.
        self.committed_text: str = ""
        self.committed_bytes: int = 0
        self._partial_task: Optional[asyncio.Task] = None
        # Continuous-mode state. ``continuous=True`` enables silero-vad on the
        # rolling tail; consecutive empty-segment passes accumulate in
        # ``_silent_passes`` and trigger an stt_result cut + reset once they
        # cross SILENCE_PASSES. ``_last_partial`` is the most recent merged
        # text we broadcast, used as the cut payload (it matches what the
        # user has been watching on screen).
        self.continuous: bool = False
        self._silent_passes: int = 0
        self._last_partial: str = ""
        # Convo inner-loop session, set only in continuous mode (PTT leaves it
        # None and keeps the raw stt_result path). Typed loosely to keep the
        # convo/agent/opencode import chain off the PTT-only code path.
        self.convo = None  # Optional[ConvoSession]

    # ---- client management ----

    async def attach(self, ws: WebSocket) -> None:
        self.clients.add(ws)
        self.last_active = time.monotonic()
        await self._send_one(ws, {"type": "ready", "user": self.user_id})

    def detach(self, ws: WebSocket) -> None:
        self.clients.discard(ws)
        if self.recording_client is ws:
            self.recording_client = None
            self._cancel_partial_task()
            self.pcm_chunks.clear()
            self.committed_text = ""
            self.committed_bytes = 0
            self.continuous = False
            self.convo = None
            self._silent_passes = 0
            self._last_partial = ""
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

    def _dispatch_convo_cut(self, cut_text: str) -> None:
        """Run the convo inner loop for one silence-cut off the partial loop's
        critical path, then broadcast the cleaned composer (and a submit frame
        when the model judges the message complete)."""
        convo = self.convo
        if convo is None:
            return

        async def _run() -> None:
            try:
                res = await convo.on_silence_cut(cut_text)
            except Exception:  # noqa: BLE001
                log.exception("convo cleanup crashed for %s", self.user_id)
                return
            await self.broadcast_json({"type": "composer", "text": res.cleaned_text})
            if res.should_submit:
                await self.broadcast_json({"type": "submit", "text": res.cleaned_text})

        asyncio.create_task(_run())

    # ---- input handlers ----

    async def handle_start(
        self,
        ws: WebSocket,
        mode: Optional[str] = None,
        context: Optional[str] = None,
    ) -> None:
        # Latest "start" wins — barge-in across clients.
        self._cancel_partial_task()
        self.pcm_chunks.clear()
        self.committed_text = ""
        self.committed_bytes = 0
        self.continuous = (mode == "continuous")
        self._silent_passes = 0
        self._last_partial = ""
        self.recording_client = ws
        self.last_active = time.monotonic()
        # Continuous mode runs the convo inner loop: a per-session cleanup
        # agent fed by each silence-cut. PTT mode leaves convo None.
        self.convo = None
        if self.continuous:
            from backend.convo import get_convo_manager

            mgr = get_convo_manager()
            self.convo = mgr.new_session()
            if isinstance(context, str):
                self.convo.set_context(context)
            # Warm the opencode server ahead of the first cut (best-effort).
            asyncio.create_task(mgr.ensure_started())
        await self._status("recording", "recording…")
        self._partial_task = asyncio.create_task(self._partial_loop(ws))

    async def add_audio(self, ws: WebSocket, data: bytes) -> None:
        if self.recording_client is ws:
            self.pcm_chunks.append(data)

    async def handle_cancel(self) -> None:
        self.recording_client = None
        self._cancel_partial_task()
        self.pcm_chunks.clear()
        self.committed_text = ""
        self.committed_bytes = 0
        self.continuous = False
        self.convo = None
        self._silent_passes = 0
        self._last_partial = ""
        await self._status("idle", "")

    async def handle_end(self, ws: WebSocket) -> None:
        if self.recording_client is not ws:
            return
        self.recording_client = None
        self._cancel_partial_task()
        if self.continuous or self.convo is not None:
            # Convo mode emits a result per silence-cut; stopping just tears the
            # session down. No final full-PCM pass — in continuous mode
            # pcm_chunks holds ALL audio since start, so a full pass would
            # re-transcribe the whole conversation. A residual tail spoken
            # without a trailing pause is dropped (v1 limitation).
            self.continuous = False
            self.convo = None
            self.pcm_chunks.clear()
            self.committed_text = ""
            self.committed_bytes = 0
            self._silent_passes = 0
            self._last_partial = ""
            await self._status("idle", "")
            return
        if not self.pcm_chunks:
            self.committed_text = ""
            self.committed_bytes = 0
            await self._status("idle", "no audio captured")
            return
        pcm = b"".join(self.pcm_chunks)
        self.pcm_chunks.clear()
        # Reset splicing state — final pass runs on the full PCM, the
        # committed prefix is only an optimization for the rolling partials.
        self.committed_text = ""
        self.committed_bytes = 0

        # Dump for debugging (last PTT only).
        dump_path = self.log_dir / f"ptt-{_safe(self.user_id)}.last.wav"
        try:
            with wave.open(str(dump_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(pcm)
        except Exception:  # noqa: BLE001
            log.exception("pcm dump failed")

        await self._status("transcribing", "transcribing…")
        from backend.stt import get_transcriber

        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        try:
            text = await loop.run_in_executor(
                None, get_transcriber().transcribe, pcm, SAMPLE_RATE,
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

    # ---- rolling partial transcription ----

    def _cancel_partial_task(self) -> None:
        task = self._partial_task
        self._partial_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _partial_loop(self, ws: WebSocket) -> None:
        """While ``ws`` is the recording client, periodically transcribe
        the audio since the last commit point and broadcast a partial.

        Splicing strategy: faster-whisper returns segments with timestamps
        relative to the start of the audio we hand it. We always hand it
        only the *tail* (everything past ``committed_bytes``). Of the
        segments it returns, all but the last get committed — the last is
        kept as "rolling" because it may revise once whisper sees more.
        ``committed_bytes`` advances by the duration of the last committed
        segment so the next pass slices further into the buffer.
        """
        loop = asyncio.get_running_loop()
        min_tail_bytes = int(PARTIAL_MIN_TAIL_SEC * SAMPLE_RATE * SAMPLE_BYTES)
        try:
            while self.recording_client is ws:
                await asyncio.sleep(PARTIAL_MIN_GAP_SEC)
                if self.recording_client is not ws:
                    return
                joined = b"".join(self.pcm_chunks)
                tail = joined[self.committed_bytes:]
                if len(tail) < min_tail_bytes:
                    continue
                try:
                    segments = await loop.run_in_executor(
                        None, _transcribe_segments, tail, self.continuous,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("partial whisper pass failed for %s", self.user_id)
                    continue
                if self.recording_client is not ws:
                    return
                if not segments:
                    if self.continuous:
                        self._silent_passes += 1
                        if self._silent_passes >= SILENCE_PASSES:
                            cut_text = (
                                (self.committed_text + " " + self._last_partial).strip()
                                if self._last_partial
                                else self.committed_text.strip()
                            )
                            if cut_text:
                                log.debug(
                                    "ptt silence-cut for %s: text=%r",
                                    self.user_id, cut_text,
                                )
                                # PHASE 2 SEAM: in convo (continuous) mode the
                                # finalized utterance goes through the LLM inner
                                # loop (faithful cleanup + submit decision)
                                # instead of a raw stt_result. Dispatched as a
                                # task so the partial loop never blocks on the
                                # LLM round-trip.
                                if self.convo is not None:
                                    self._dispatch_convo_cut(cut_text)
                                else:
                                    await self.broadcast_json(
                                        {"type": "stt_result", "text": cut_text},
                                    )
                            # Skip past everything we've accumulated so the
                            # next utterance transcribes from fresh tail.
                            self.committed_bytes = len(joined)
                            self.committed_text = ""
                            self._last_partial = ""
                            self._silent_passes = 0
                    continue
                # Non-empty segments — reset the silence counter.
                self._silent_passes = 0
                if len(segments) >= 2:
                    stable = segments[:-1]
                    stable_text = " ".join(s[0] for s in stable if s[0]).strip()
                    if stable_text:
                        self.committed_text = (
                            self.committed_text + " " + stable_text
                        ).strip()
                    last_stable_end_s = stable[-1][2]
                    self.committed_bytes += int(
                        last_stable_end_s * SAMPLE_RATE * SAMPLE_BYTES
                    )
                tail_text = segments[-1][0]
                merged = (self.committed_text + " " + tail_text).strip()
                if not merged:
                    continue
                log.debug(
                    "ptt partial for %s: committed_bytes=%d segs=%d text=%r",
                    self.user_id, self.committed_bytes, len(segments), merged,
                )
                self._last_partial = tail_text
                await self.broadcast_json({"type": "partial", "text": merged})
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            log.exception("partial loop crashed for %s", self.user_id)


class PttRegistry:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._agents: dict[str, PttAgent] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: Optional[asyncio.Task] = None

    async def get_or_create(self, user_id: str) -> PttAgent:
        async with self._lock:
            agent = self._agents.get(user_id)
            if agent is None:
                agent = PttAgent(user_id, self.log_dir)
                self._agents[user_id] = agent
            return agent

    async def attach(self, agent: PttAgent, ws: WebSocket) -> None:
        await agent.attach(ws)

    def detach(self, agent: PttAgent, ws: WebSocket) -> None:
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
                    log.info("reaping idle ptt agent: %s", uid)
        except asyncio.CancelledError:
            return


_registry: Optional[PttRegistry] = None


def get_registry() -> PttRegistry:
    global _registry
    if _registry is None:
        from awm import config
        log_dir = Path(getattr(config, "AWM_DIR", Path.home() / ".awm")) / "voice"
        _registry = PttRegistry(log_dir)
    return _registry


async def run_ptt_ws_session(websocket: WebSocket, user_as: str) -> None:
    """Drive a PTT WS connection end-to-end.

    Owns the per-user agent acquisition, audio/text frame demux, and
    detach-on-exit. The API handler shrinks to ``auth + accept + delegate``.
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
                mode = payload.get("mode")
                ctx = payload.get("context")
                await agent.handle_start(
                    websocket,
                    mode=mode if isinstance(mode, str) else None,
                    context=ctx if isinstance(ctx, str) else None,
                )
            elif t == "context":
                # Frontend pushing an updated recent-chat-history buffer for the
                # convo cleanup LLM. Ignored outside continuous mode.
                ctx = payload.get("text")
                if isinstance(ctx, str) and agent.convo is not None:
                    agent.convo.set_context(ctx)
            elif t == "end":
                await agent.handle_end(websocket)
            elif t == "cancel":
                await agent.handle_cancel()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("ptt ws handler crashed for %s", user_as)
    finally:
        reg.detach(agent, websocket)
