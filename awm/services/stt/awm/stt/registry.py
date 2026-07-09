"""Per-user PTT/STT WebSocket registry.

Direct port of ``awm/voice/registry.py`` for the V2 svc — same wire
protocol, same singleton-Transcriber reuse via ``awm.voice.stt``. Once
parity is confirmed end-to-end, V1's ``awm/voice/router.py`` will delegate
to this module and the V1 registry/router can be removed.

Wire protocol:

  text frames up:
    ``{"type":"start", "mode":"ptt"|"continuous"?, "context":"..."?, "telemetry":bool?}``
                            begin recording (latest wins, barge-in). In
                            continuous mode the convo inner loop runs; an
                            optional ``context`` seeds its 2k chat-history buffer.
                            ``telemetry:true`` opts this session into the dev
                            ``metric`` timing stream (see below); default off.
    ``{"type":"context", "text":"..."}``  update the convo context buffer
    ``{"type":"end"}``      finalize → whisper → broadcast stt_result
    ``{"type":"cancel"}``   drop the current buffer
  binary frames up: raw int16 LE 16 kHz mono PCM chunks from the worklet.

  broadcast down (to every tab of the same user):
    ``{"type":"ready", "user":"..."}``         on attach
    ``{"type":"status","stage":"recording"|"transcribing"|"refining"|"idle", "text":"..."}``
                                              (convo emits "refining" while the
                                              LLM cleans a silence-cut)
    ``{"type":"partial","text":"..."}``        rolling STT while recording
    ``{"type":"stt_result","text":"..."}``     PTT: after whisper completes
    ``{"type":"composer","text":"..."}``       convo: LLM-cleaned message so far
    ``{"type":"submit","text":"..."}``         convo: message judged complete
    ``{"type":"error","message":"..."}``       on whisper failure
    ``{"type":"metric","event":"...","t":ms, ...}``  DEV ONLY (telemetry opt-in):
                                              high-res timing of each pipeline
                                              stage — partial_pass, vad_poll,
                                              silence_cut, refine_preview,
                                              whisper_repass, llm_refine,
                                              submit_confirm, ptt_whisper. Each
                                              carries a ``perf_counter`` ms stamp
                                              ``t`` and per-event fields (e.g.
                                              ``dur_ms``). Ignored by clients that
                                              don't render it.

Partial streaming uses faster-whisper's per-segment timestamps to splice
the buffer: each pass transcribes only the audio after the last committed
segment, so cost is bounded by the unstable tail rather than the full
utterance. The final ``stt_result`` always runs a fresh end-to-end pass on
the complete PCM — committed/tail bookkeeping exists purely to keep the
mid-recording partials cheap.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:  # annotation-only; the bridge passes a duck-typed WS object,
    from fastapi import WebSocket  # so fastapi is not a runtime dependency.


log = logging.getLogger("awm.services.stt.registry")


IDLE_TIMEOUT_SEC = int(os.environ.get("PTT_IDLE_SEC", os.environ.get("VOICE_IDLE_SEC", "1800")))

# Partial-streaming cadence. This is now a *floor* on the period between pass
# STARTS, not a fixed dead-wait before each pass: the loop streams whisper output
# as fast as a tail pass completes, only napping out the remainder of the floor
# when a pass finished quicker (see ``_partial_loop``). The cosmetic stream is the
# user's primary, fast feed — so we keep it tight. Because each pass is awaited,
# the loop can never outrun the model: a long pauseless tail self-throttles to its
# own (growing) pass duration rather than piling passes up. Min-tail keeps us off
# sub-word slivers. (Was a fixed 2.0s gap; lowered + redefined for responsiveness.)
PARTIAL_MIN_GAP_SEC = float(os.environ.get("PTT_PARTIAL_GAP", "0.3"))
PARTIAL_MIN_TAIL_SEC = float(os.environ.get("PTT_PARTIAL_MIN_TAIL", "0.4"))
# Continuous mode: trailing-silence hang time. A silence-cut fires once the
# rolling tail ends with at least this many seconds of non-speech AFTER the last
# transcribed word, measured from whisper's segment timestamps (tail duration
# minus the last segment's end time). Those timestamps stay on the original
# timeline even with vad_filter on, so the trailing gap is visible even though
# the held final segment keeps transcribing.
#
# This REPLACES the old "N consecutive empty-segment passes" detector, which
# could never fire for a real end-of-utterance: the last segment is deliberately
# left uncommitted, so the tail always retained the final words and never
# transcribed empty — convo never cut, never refined, never submitted.
SILENCE_HANG_SEC = float(os.environ.get("PTT_SILENCE_HANG", "1.2"))
# Continuous/convo mode: total trailing silence after which the accumulated
# message AUTO-SUBMITS. The silence poll re-derives the trailing-silence span
# from the live audio every tick (a fixed trailing window, see _silence_loop), so
# this is a GUARANTEE — once the mic stays genuinely silent this long, it submits;
# there is no one-shot confirm timer that a transient blip can strand. Must be
# >= SILENCE_HANG_SEC so the finalize-cut populates the composer before submit.
SUBMIT_SILENCE_SEC = float(os.environ.get("PTT_SUBMIT_SILENCE", "2.0"))
# Extra audio (beyond SUBMIT_SILENCE_SEC) the submit window includes so the last
# real speech region is visible inside it, letting us measure trailing silence as
# (window_dur - last_speech_end) rather than only "is the whole window silent".
SUBMIT_VAD_PAD_SEC = float(os.environ.get("PTT_SUBMIT_VAD_PAD", "0.6"))
# Blip floor (ms) for the silence/submit clock: speech regions shorter than this
# are ignored when locating the last real speech, so a momentary noise (click,
# cough, breath, VAD false-positive) does NOT reset the submit timer. Applies
# only to the silence-timing VAD; the cosmetic partial stream stays sensitive.
VAD_BLIP_MS = int(os.environ.get("PTT_VAD_BLIP_MS", "250"))
# Continuous mode: cadence of the fast VAD-only silence poll. Silero VAD is
# ~100x cheaper than a whisper pass, so silence is detected on its own tick
# (decoupled from PARTIAL_MIN_GAP_SEC) — a short utterance that ends between
# whisper passes is cut promptly instead of waiting out the next 2s pass.
VAD_POLL_SEC = float(os.environ.get("PTT_VAD_POLL", "0.4"))
SAMPLE_RATE = 16000
SAMPLE_BYTES = 2  # int16 LE


def _transcribe_segments(
    pcm_bytes: bytes,
    vad_filter: bool = False,
    initial_prompt: Optional[str] = None,
) -> list[tuple[str, float, float]]:
    """Run whisper on PCM, return ``[(text, start_s, end_s), ...]``.

    Reuses the vendored ``awm.stt.stt`` singleton so the model is loaded
    once per process. The singleton's public ``.transcribe()`` joins and
    discards segment timestamps; we need them for splicing, so we drive
    ``model.transcribe(...)`` directly here.

    ``initial_prompt`` (optional) biases decoding toward domain vocabulary — the
    dictation caller passes a short glossary of custom terms so whisper spells
    them right (e.g. project names). ``None`` (the default) leaves behaviour
    exactly as before, so PTT/convo callers that pass nothing are unaffected.
    """
    from awm.stt.stt import get_transcriber

    if not pcm_bytes:
        return []
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    model = get_transcriber()._ensure_loaded()
    segments, _info = model.transcribe(
        audio,
        language="en",
        beam_size=1,
        vad_filter=vad_filter,
        initial_prompt=initial_prompt or None,
    )
    return [(seg.text.strip(), seg.start, seg.end) for seg in segments]


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:64]


class SttAgent:
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
        # Fast VAD-only silence poll (continuous mode), and a lock serializing the
        # whisper model across the streaming pass and the cut's own pass (the
        # underlying model is not safe to call concurrently). ``_cut_epoch`` bumps
        # on every silence-cut so an in-flight streaming pass whose splice offsets
        # the cut just invalidated can detect it and drop its result.
        self._silence_task: Optional[asyncio.Task] = None
        self._whisper_lock = asyncio.Lock()
        self._cut_epoch: int = 0
        # Continuous-mode state. ``continuous=True`` enables silero-vad on the
        # rolling tail; a silence-cut fires when the tail ends with at least
        # ``SILENCE_HANG_SEC`` of trailing non-speech (gap between the last
        # segment's end and the tail duration). ``_last_partial`` is the most
        # recent merged text we broadcast, used as the cut payload (it matches
        # what the user has been watching on screen). ``_silent_passes`` is
        # retained only for the rare all-silence-tail path's bookkeeping.
        self.continuous: bool = False
        self._silent_passes: int = 0
        self._last_partial: str = ""
        # Convo inner-loop session, set only in continuous mode (PTT leaves it
        # None and keeps the raw stt_result path). Typed loosely to keep the
        # convo/agent/opencode import chain off the PTT-only code path.
        self.convo = None  # Optional[ConvoSession]
        # Submit is owned by the silence poll (_silence_loop): it re-derives the
        # trailing-silence span from the live audio each tick and fires once it
        # reaches SUBMIT_SILENCE_SEC. No separate confirm task to strand. ``True``
        # once we've submitted the current composer, cleared when a new cut builds
        # the next message — guards against re-submitting an already-sent message.
        self._submitted: bool = False
        # In-flight silence-cut smoothing task (accurate whisper re-pass),
        # dispatched off the cut's critical path. Tracked so a stop / cancel /
        # barge-in can abort the backend work mid-flight instead of letting a
        # stale re-pass land after the user cleared.
        self._cut_task: Optional[asyncio.Task] = None
        # Dev telemetry opt-in (set from the start frame's ``telemetry`` flag). When
        # True, ``_metric`` emits high-res timing frames for each pipeline stage;
        # default False so a normal session produces none. See :meth:`_metric`.
        self.telemetry: bool = False
        # Optional whisper decoding bias (a short glossary of custom terms) set
        # from the start frame's ``initial_prompt``. None = default whisper
        # behaviour. Used by the notes dictation to spell domain vocab correctly.
        self.initial_prompt: Optional[str] = None

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
            self._cancel_silence_task()
            self._cancel_cut_task()
            self.pcm_chunks.clear()
            self.committed_text = ""
            self.committed_bytes = 0
            self.continuous = False
            self.convo = None
            self._silent_passes = 0
            self._last_partial = ""
            self.telemetry = False
            self.initial_prompt = None
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

    async def _metric(self, event: str, **fields) -> None:
        """Dev-only high-resolution timing frame.

        No-op unless this session opted into telemetry (the start frame carried
        ``telemetry: true``). Goes through the same ``broadcast_json`` chokepoint as
        every other frame, so it must be emitted OUTSIDE ``_whisper_lock`` — never
        inside the lock, or a metric send could serialize against a decode. ``t`` is
        a ``perf_counter`` millisecond stamp on the backend's own clock (a different
        epoch than the browser's ``performance.now()``); the client keys row order
        off arrival time and uses the per-event ``dur_ms`` for the measured
        duration. Pure observation — an emit never alters control flow."""
        if not self.telemetry:
            return
        await self.broadcast_json(
            {
                "type": "metric",
                "event": event,
                "t": round(time.perf_counter() * 1000, 3),
                **fields,
            }
        )

    def _cancel_cut_task(self) -> None:
        task = self._cut_task
        self._cut_task = None
        if task is not None and not task.done():
            task.cancel()

    def _dispatch_convo_cut(
        self, preview: str, tail: bytes, committed_prefix: str, cut_byte: int
    ) -> None:
        """Finalize the just-cut utterance: show it immediately, then re-transcribe
        it accurately in the background. ``preview`` (raw text the cosmetic stream
        already produced) is broadcast at once so the panel updates the instant the
        cut fires; the accurate whisper re-pass then runs OFF the poll's critical
        path and replaces it in place, accumulating into ``convo.composer``. This
        path does NOT submit — the silence poll owns submit timing (see
        :meth:`_silence_loop`). ``cut_byte`` is retained for signature stability."""
        convo = self.convo
        if convo is None:
            return
        # A new cut means the user is still building this message → it isn't a
        # sent message anymore; let the silence poll re-evaluate submit.
        self._submitted = False

        async def _run() -> None:
            # 1. INSTANT: surface the raw preview now. Keep the already-accumulated
            #    prefix (convo.composer) and show only the new chunk raw appended,
            #    so nothing visibly jumps when the re-passed text lands next.
            if preview:
                await self.broadcast_json(
                    {"type": "composer", "text": (convo.composer + " " + preview).strip()}
                )
            await self._metric("cut_preview", preview_len=len(preview))
            await self._status("recording", "transcribing…")
            # 2. ACCURATE re-pass: re-transcribe the tail off the critical path and
            #    splice onto the committed prefix. Falls back to the streamed
            #    preview if the pass comes up empty (e.g. an ultra-short utterance).
            #    The lock serializes model use with the cosmetic loop (the model is
            #    not concurrency-safe).
            loop = asyncio.get_running_loop()
            repass_ms = 0.0
            try:
                async with self._whisper_lock:
                    t0 = time.perf_counter()
                    segments = await loop.run_in_executor(
                        None, _transcribe_segments, tail, self.continuous,
                        self.initial_prompt,
                    )
                    repass_ms = (time.perf_counter() - t0) * 1000
            except Exception:  # noqa: BLE001
                log.exception("cut whisper pass failed for %s", self.user_id)
                segments = []
            await self._metric(
                "whisper_repass", dur_ms=round(repass_ms, 1), segs=len(segments)
            )
            tail_text = " ".join(s[0] for s in segments if s[0]).strip()
            cut_text = (committed_prefix + " " + tail_text).strip() or preview
            if not cut_text:
                await self._status("recording", "listening…")
                return
            # 3. Accumulate the raw text into the composer and show it.
            composer = await convo.on_silence_cut(cut_text)
            await self._metric("convo_cut", composer_len=len(composer))
            await self.broadcast_json({"type": "composer", "text": composer})
            await self._status("recording", "listening…")

        # A fresh cut supersedes any still-running re-pass.
        self._cancel_cut_task()
        self._cut_task = asyncio.create_task(_run())

    async def _silence_cut(self, joined: bytes, tail: bytes, committed_prefix: str) -> None:
        """Fire one silence-cut and reset the splice window so the next utterance
        transcribes from a fresh tail.

        The critical path stays whisper-free: instead of transcribing here, we hand
        off a ``preview`` built from text the cosmetic stream has ALREADY produced
        (committed prefix + last rolling tail), so the just-finished words hit the
        UI the instant the cut fires. The accurate re-pass and the LLM cleanup run
        off this path, inside the dispatched task, and replace the preview in place.

        Called by the fast VAD poll once ``tail`` ends with ``SILENCE_HANG_SEC`` of
        non-speech. ``joined``/``tail``/``committed_prefix`` are snapshotted together
        by the poll (before its VAD await), so they are mutually consistent:
        ``committed_prefix`` covers exactly the audio before ``tail``; the task's
        accurate pass transcribes ``tail`` and splices it onto that prefix.
        """
        # Invalidate any in-flight streaming pass (its splice offsets are about to
        # be stale) and reset the window NOW so a concurrent pass / the next poll
        # immediately sees post-cut state.
        self._cut_epoch += 1
        cut_byte = len(joined)
        self.committed_bytes = cut_byte
        self.committed_text = ""
        last_partial = self._last_partial
        self._last_partial = ""
        self._silent_passes = 0

        # Preview from already-streamed text — no whisper pass on the critical path.
        # Empty only for an utterance so short the cosmetic loop never transcribed it
        # (rare with the fast stream); the task's accurate pass still recovers it.
        preview = (committed_prefix + " " + last_partial).strip()
        log.debug(
            "stt silence-cut for %s: cut_byte=%d epoch=%d preview=%r",
            self.user_id, cut_byte, self._cut_epoch, preview,
        )
        await self._metric(
            "silence_cut", cut_byte=cut_byte, epoch=self._cut_epoch,
            preview_len=len(preview),
        )
        if self.convo is not None:
            self._dispatch_convo_cut(preview, tail, committed_prefix, cut_byte)
        elif preview:
            # PTT-continuous without a convo session (defensive — continuous always
            # builds one today): no LLM smoothing, so the streamed preview IS the
            # result.
            await self.broadcast_json({"type": "stt_result", "text": preview})

    # ---- input handlers ----

    async def handle_start(
        self,
        ws: WebSocket,
        mode: Optional[str] = None,
        context: Optional[str] = None,
        telemetry: bool = False,
        initial_prompt: Optional[str] = None,
    ) -> None:
        # Latest "start" wins — barge-in across clients.
        self._cancel_partial_task()
        self._cancel_silence_task()
        self._cancel_cut_task()
        self.pcm_chunks.clear()
        self.committed_text = ""
        self.committed_bytes = 0
        self.continuous = (mode == "continuous")
        self._silent_passes = 0
        self._last_partial = ""
        self._submitted = False
        self.telemetry = telemetry
        self.initial_prompt = initial_prompt or None
        self.recording_client = ws
        self.last_active = time.monotonic()
        # Continuous mode runs the convo inner loop: a per-session raw-transcript
        # accumulator fed by each silence-cut. PTT mode leaves convo None. (The
        # ``context`` field is still accepted from the frontend but unused now that
        # there is no LLM prompt to feed it into.)
        self.convo = None
        if self.continuous:
            from awm.stt.convo import ConvoSession

            self.convo = ConvoSession()
        await self._status("recording", "listening…" if self.continuous else "recording…")
        self._partial_task = asyncio.create_task(self._partial_loop(ws))
        if self.continuous:
            # Fast VAD poll owns silence-cut timing; the streaming loop above is
            # now cosmetic (visible partials only).
            self._silence_task = asyncio.create_task(self._silence_loop(ws))

    async def add_audio(self, ws: WebSocket, data: bytes) -> None:
        if self.recording_client is ws:
            self.pcm_chunks.append(data)

    async def handle_cancel(self) -> None:
        self.recording_client = None
        self._cancel_partial_task()
        self._cancel_silence_task()
        self._cancel_cut_task()
        self.pcm_chunks.clear()
        self.committed_text = ""
        self.committed_bytes = 0
        self.continuous = False
        self.convo = None
        self._silent_passes = 0
        self._last_partial = ""
        self.telemetry = False
        await self._status("idle", "")

    async def handle_end(self, ws: WebSocket) -> None:
        if self.recording_client is not ws:
            return
        self.recording_client = None
        self._cancel_partial_task()
        self._cancel_silence_task()
        self._cancel_cut_task()
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
        dump_path = self.log_dir / f"stt-{_safe(self.user_id)}.last.wav"
        try:
            with wave.open(str(dump_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(pcm)
        except Exception:  # noqa: BLE001
            log.exception("pcm dump failed")

        await self._status("transcribing", "transcribing…")
        from awm.stt.stt import get_transcriber

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
        await self._metric("ptt_whisper", dur_ms=stt_ms)
        await self.broadcast_json({"type": "stt_result", "text": text or ""})
        await self._status("idle", "")
        self.last_active = time.monotonic()

    # ---- rolling partial transcription ----

    def _cancel_partial_task(self) -> None:
        task = self._partial_task
        self._partial_task = None
        if task is not None and not task.done():
            task.cancel()

    def _cancel_silence_task(self) -> None:
        task = self._silence_task
        self._silence_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _partial_loop(self, ws: WebSocket) -> None:
        """While ``ws`` is the recording client, periodically transcribe the
        audio since the last commit point and broadcast a visible partial.

        This loop is **cosmetic**: it produces the rolling on-screen text and
        keeps ``committed_text`` / ``committed_bytes`` advancing for cheap
        splicing. It no longer decides silence-cuts — that moved to
        :meth:`_silence_loop`, the fast VAD poll (continuous mode). The
        authoritative cut text is (re)transcribed by :meth:`_silence_cut` itself.

        Splicing strategy: faster-whisper returns segments with timestamps
        relative to the start of the audio we hand it. We always hand it only the
        *tail* (everything past ``committed_bytes``). Of the segments it returns,
        all but the last get committed — the last is kept "rolling" because it may
        revise once whisper sees more. ``committed_bytes`` advances by the
        duration of the last committed segment so the next pass slices further in.
        """
        loop = asyncio.get_running_loop()
        min_tail_bytes = int(PARTIAL_MIN_TAIL_SEC * SAMPLE_RATE * SAMPLE_BYTES)

        async def _one_pass() -> bool:
            """Run one cosmetic partial pass. Returns ``False`` when the recording
            client went away (caller should stop the loop), ``True`` otherwise —
            including the no-op cases (tail too short, stale epoch, empty result),
            so the caller still applies the cadence floor between attempts."""
            tail = b"".join(self.pcm_chunks)[self.committed_bytes:]
            if len(tail) < min_tail_bytes:
                return True
            # Snapshot the cut epoch before the (slow) whisper pass; if a
            # silence-cut bumps it meanwhile, our splice offsets are stale and
            # this pass must be dropped. The lock serializes model use with the
            # cut's own pass (the model is not concurrency-safe).
            epoch = self._cut_epoch
            decode_ms = 0.0
            try:
                async with self._whisper_lock:
                    t0 = time.perf_counter()
                    segments = await loop.run_in_executor(
                        None, _transcribe_segments, tail, self.continuous,
                        self.initial_prompt,
                    )
                    decode_ms = (time.perf_counter() - t0) * 1000
            except Exception:  # noqa: BLE001
                log.exception("partial whisper pass failed for %s", self.user_id)
                return True
            # Emit after releasing the lock (never serialize a metric send against a
            # decode). The decode happened regardless of whether we keep its result.
            await self._metric(
                "partial_pass", dur_ms=round(decode_ms, 1), segs=len(segments)
            )
            if self.recording_client is not ws:
                return False
            if epoch != self._cut_epoch:
                # A silence-cut reset the splice window mid-pass; drop this
                # result — the next pass starts from the post-cut tail.
                return True
            if not segments:
                return True
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
                return True
            self._last_partial = tail_text
            log.debug(
                "stt partial for %s: committed_bytes=%d segs=%d text=%r",
                self.user_id, self.committed_bytes, len(segments), merged,
            )
            await self.broadcast_json({"type": "partial", "text": merged})
            return True

        try:
            while self.recording_client is ws:
                pass_start = loop.time()
                if not await _one_pass():
                    return
                # Floor the cadence on pass *starts*: nap only the leftover of the
                # floor when the pass beat it; if the pass already ran past the
                # floor (a long tail), loop immediately — as fast as the model goes.
                nap = PARTIAL_MIN_GAP_SEC - (loop.time() - pass_start)
                if nap > 0:
                    await asyncio.sleep(nap)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            log.exception("partial loop crashed for %s", self.user_id)

    async def _silence_loop(self, ws: WebSocket) -> None:
        """Fast VAD-only poll (continuous mode) owning silence-cut timing.

        Every ``VAD_POLL_SEC`` it runs Silero VAD on the raw tail and fires one
        silence-cut once the tail ends with ``SILENCE_HANG_SEC`` of non-speech
        AFTER real voice — measured directly from the audio, so neither whisper's
        cadence nor its hallucinations on trailing silence can move the decision.
        The "voice then silence" test (``voice_end is not None and gap >= hang``)
        is what stops it from re-cutting the silent tail it leaves behind: after a
        cut the window resets to post-cut audio, which is silence → unvoiced → no
        cut, until the user speaks again.
        """
        loop = asyncio.get_running_loop()
        min_tail_bytes = int(PARTIAL_MIN_TAIL_SEC * SAMPLE_RATE * SAMPLE_BYTES)
        try:
            while self.recording_client is ws:
                await asyncio.sleep(VAD_POLL_SEC)
                if self.recording_client is not ws:
                    return
                joined = b"".join(self.pcm_chunks)
                # Snapshot the splice prefix WITH the tail (before the VAD await)
                # so the cut stays self-consistent if the cosmetic loop commits
                # during the await — see _silence_cut's docstring.
                committed_prefix = self.committed_text
                tail = joined[self.committed_bytes:]
                from awm.stt import vad

                # --- 1. finalize-cut decision (off the splice tail) ---
                # Needs a long-enough splice tail; the submit check below runs
                # regardless so a short post-cut tail can't stall the guarantee.
                if len(tail) >= min_tail_bytes:
                    t0 = time.perf_counter()
                    try:
                        # Blip-tolerant: short noise regions are ignored, so the
                        # measured trailing gap reflects the last REAL word.
                        voice_end = await loop.run_in_executor(
                            None,
                            functools.partial(
                                vad.last_speech_end_s, tail, min_region_ms=VAD_BLIP_MS
                            ),
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("vad poll failed for %s", self.user_id)
                        voice_end = None
                    else:
                        vad_ms = (time.perf_counter() - t0) * 1000
                        if self.recording_client is not ws:
                            return
                        tail_dur = len(tail) / SAMPLE_BYTES / SAMPLE_RATE
                        gap = (tail_dur - voice_end) if voice_end is not None else None
                        # Heartbeat row (silent no-ops too, where gap is None).
                        await self._metric(
                            "vad_poll",
                            dur_ms=round(vad_ms, 1),
                            gap=round(gap, 3) if gap is not None else None,
                            voice_end=round(voice_end, 3) if voice_end is not None else None,
                        )
                        if voice_end is not None and gap >= SILENCE_HANG_SEC:
                            log.debug(
                                "stt vad cut for %s: tail_dur=%.2f voice_end=%.2f gap=%.2f",
                                self.user_id, tail_dur, voice_end, gap,
                            )
                            await self._silence_cut(joined, tail, committed_prefix)

                # --- 2. submit decision (the guarantee), owned here ---
                await self._maybe_submit(joined, loop)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            log.exception("silence loop crashed for %s", self.user_id)

    async def _maybe_submit(self, joined: bytes, loop) -> None:
        """Auto-submit the accumulated composer once the mic has been genuinely
        silent for ``SUBMIT_SILENCE_SEC``. Called every poll: it re-derives the
        trailing-silence span from a FIXED trailing window of the live audio
        (independent of the cut's splice window, which resets on each cut), so the
        decision is self-correcting and sustained silence is GUARANTEED to cross
        the threshold — there is no one-shot timer a transient blip can strand. A
        lone blip is below ``VAD_BLIP_MS`` and ignored; genuine resumed speech puts
        voice back in the window and naturally holds the submit (the next cut folds
        it into the same message)."""
        convo = self.convo
        if convo is None or self._submitted or not convo.composer:
            return
        # A fixed trailing window long enough to *prove* SUBMIT_SILENCE_SEC of
        # silence (plus pad so the last real word sits inside it for the gap math).
        win_bytes = int((SUBMIT_SILENCE_SEC + SUBMIT_VAD_PAD_SEC) * SAMPLE_RATE * SAMPLE_BYTES)
        sub_win = joined[-win_bytes:] if len(joined) >= win_bytes else joined
        win_dur = len(sub_win) / SAMPLE_BYTES / SAMPLE_RATE
        if win_dur < SUBMIT_SILENCE_SEC:
            return  # not enough audio yet to demonstrate the full silence span
        from awm.stt import vad

        try:
            end = await loop.run_in_executor(
                None,
                functools.partial(vad.last_speech_end_s, sub_win, min_region_ms=VAD_BLIP_MS),
            )
        except Exception:  # noqa: BLE001
            log.exception("submit vad failed for %s", self.user_id)
            return
        # Trailing silence = time after the last real speech in the window (or the
        # whole window when it holds no speech at all → fully silent).
        trailing = (win_dur - end) if end is not None else win_dur
        if trailing < SUBMIT_SILENCE_SEC:
            return
        text = await convo.take_submission()
        self._submitted = True
        # Park the splice window at the current end so the post-submit silence
        # isn't re-cut; a fresh utterance starts a new tail.
        self.committed_bytes = len(b"".join(self.pcm_chunks))
        self.committed_text = ""
        self._last_partial = ""
        await self._metric("submit", trailing_silence=round(trailing, 3), submitted=bool(text))
        if text:
            log.info("convo auto-submit for %s: %r", self.user_id, text[:120])
            await self.broadcast_json({"type": "submit", "text": text})
        await self._status("recording", "listening…")


class SttRegistry:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._agents: dict[str, SttAgent] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: Optional[asyncio.Task] = None

    async def get_or_create(self, user_id: str) -> SttAgent:
        async with self._lock:
            agent = self._agents.get(user_id)
            if agent is None:
                agent = SttAgent(user_id, self.log_dir)
                self._agents[user_id] = agent
            return agent

    async def attach(self, agent: SttAgent, ws: WebSocket) -> None:
        await agent.attach(ws)

    def detach(self, agent: SttAgent, ws: WebSocket) -> None:
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
                    log.info("reaping idle stt agent: %s", uid)
        except asyncio.CancelledError:
            return


_registry: Optional[SttRegistry] = None


def get_registry() -> SttRegistry:
    global _registry
    if _registry is None:
        from awm import config
        log_dir = Path(getattr(config, "AWM_DIR", Path.home() / ".awm")) / "voice"
        _registry = SttRegistry(log_dir)
    return _registry


async def run_stt_ws_session(websocket: WebSocket, user_as: str) -> None:
    """Drive a PTT WS connection end-to-end.

    Owns the per-user agent acquisition, audio/text frame demux, and
    detach-on-exit. Drives a duck-typed WS exposing ``receive()`` (ASGI-style
    ``{"type": ...}`` dicts) — both a FastAPI ``WebSocket`` and the bridge
    adapter the hub_adapter wraps the upstream bridge in. A FastAPI disconnect
    raises ``WebSocketDisconnect``; the bridge path surfaces it as a
    ``{"type": "websocket.disconnect"}`` frame, so the exception import is
    best-effort (fastapi is not a runtime dependency of this service).
    """
    try:
        from fastapi import WebSocketDisconnect  # type: ignore
    except Exception:  # noqa: BLE001 — bridge path never raises it
        class WebSocketDisconnect(Exception):  # type: ignore
            pass

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
                ip = payload.get("initial_prompt")
                await agent.handle_start(
                    websocket,
                    mode=mode if isinstance(mode, str) else None,
                    context=ctx if isinstance(ctx, str) else None,
                    telemetry=bool(payload.get("telemetry")),
                    initial_prompt=ip if isinstance(ip, str) else None,
                )
            elif t == "context":
                # Frontend still pushes a recent-chat-history buffer; it only ever
                # fed the (now-removed) convo cleanup LLM, so it's accepted and
                # ignored. Left wired so no frontend change is required.
                pass
            elif t == "end":
                await agent.handle_end(websocket)
            elif t == "cancel":
                await agent.handle_cancel()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("stt ws handler crashed for %s", user_as)
    finally:
        reg.detach(agent, websocket)
