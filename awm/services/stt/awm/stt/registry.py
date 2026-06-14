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
    ``{"type":"status","stage":"recording"|"transcribing"|"refining"|"idle", "text":"..."}``
                                              (convo emits "refining" while the
                                              LLM cleans a silence-cut)
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
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:  # annotation-only; the bridge passes a duck-typed WS object,
    from fastapi import WebSocket  # so fastapi is not a runtime dependency.


log = logging.getLogger("awm.services.stt.registry")


IDLE_TIMEOUT_SEC = int(os.environ.get("PTT_IDLE_SEC", os.environ.get("VOICE_IDLE_SEC", "1800")))

# Partial-streaming cadence. 2.0s lets each whisper pass on the tail finish
# comfortably on small.en int8 CPU before the next one starts, and gives the
# user enough new audio per pass that segment boundaries actually appear (so
# the splicer commits forward rather than re-transcribing the same tail).
# Min-tail keeps us from running whisper on sub-word slivers.
PARTIAL_MIN_GAP_SEC = float(os.environ.get("PTT_PARTIAL_GAP", "2.0"))
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
# Continuous/convo mode: after a silence-cut the LLM judges complete, we wait
# this many seconds of CONTINUED silence before actually sending. Any new speech
# (a fresh partial) in this window aborts the send — this is the "AND no new STT
# raw" half of the submit gate. Keep it >= PARTIAL_MIN_GAP_SEC so at least one
# partial pass can observe resumed speech inside the window (a same-pass resume
# would otherwise slip through and submit prematurely).
SUBMIT_CONFIRM_SEC = float(os.environ.get("PTT_SUBMIT_CONFIRM", "2.5"))
# Continuous mode: cadence of the fast VAD-only silence poll. Silero VAD is
# ~100x cheaper than a whisper pass, so silence is detected on its own tick
# (decoupled from PARTIAL_MIN_GAP_SEC) — a short utterance that ends between
# whisper passes is cut promptly instead of waiting out the next 2s pass.
VAD_POLL_SEC = float(os.environ.get("PTT_VAD_POLL", "0.4"))
SAMPLE_RATE = 16000
SAMPLE_BYTES = 2  # int16 LE


def _transcribe_segments(pcm_bytes: bytes, vad_filter: bool = False) -> list[tuple[str, float, float]]:
    """Run whisper on PCM, return ``[(text, start_s, end_s), ...]``.

    Reuses the vendored ``awm.stt.stt`` singleton so the model is loaded
    once per process. The singleton's public ``.transcribe()`` joins and
    discards segment timestamps; we need them for splicing, so we drive
    ``model.transcribe(...)`` directly here.
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
        # Convo submit debounce. A pending submit captures ``cut_byte`` (the PCM
        # offset at its cut); when its confirm window elapses it runs the VAD on
        # the audio captured since that offset and only fires if that window is
        # silent (the user stayed quiet). No text/seq proxy — ground-truth audio.
        self._pending_submit: Optional[asyncio.Task] = None

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
            self._cancel_pending_submit()
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

    def _cancel_pending_submit(self) -> None:
        task = self._pending_submit
        self._pending_submit = None
        if task is not None and not task.done():
            task.cancel()

    def _arm_pending_submit(self, convo, cut_byte: int) -> None:
        """Defer the actual send until the user has stayed silent for
        ``SUBMIT_CONFIRM_SEC`` after a complete-thought cut. When the window
        elapses, run the VAD on the audio captured since ``cut_byte`` (the cut
        point) and submit only if that window is silent — the "AND no new speech"
        gate, read from the raw audio rather than whisper text. Any voiced audio
        in the window aborts; a later cut folds the resumed speech in. A newer cut
        re-arms this."""
        self._cancel_pending_submit()

        async def _confirm() -> None:
            try:
                await asyncio.sleep(SUBMIT_CONFIRM_SEC)
            except asyncio.CancelledError:
                return
            from awm.stt import vad

            window = b"".join(self.pcm_chunks)[cut_byte:]
            loop = asyncio.get_running_loop()
            try:
                voiced = await loop.run_in_executor(None, vad.has_speech, window)
            except Exception:  # noqa: BLE001
                log.exception("submit-confirm vad failed for %s", self.user_id)
                voiced = True  # fail safe: never submit on an uncertain gate
            if voiced:
                # User resumed during the confirm window → not done; a later cut
                # folds it into the same message and re-arms.
                return
            text = await convo.take_submission()
            if text:
                await self.broadcast_json({"type": "submit", "text": text})
            await self._status("recording", "listening…")

        self._pending_submit = asyncio.create_task(_confirm())

    def _dispatch_convo_cut(self, cut_text: str, cut_byte: int) -> None:
        """Run the convo inner loop for one silence-cut off the poll's critical
        path, then broadcast the cleaned composer. A "complete" verdict does NOT
        submit immediately — it arms a debounce that fires only if the user stays
        silent (see :meth:`_arm_pending_submit`). ``cut_byte`` is the PCM offset
        at the cut; the debounce VADs the audio after it."""
        convo = self.convo
        if convo is None:
            return
        # A real (non-empty) cut means new speech happened — drop any submit that
        # was waiting out an earlier complete cut; this cut's verdict re-arms it.
        self._cancel_pending_submit()

        async def _run() -> None:
            # Surface the LLM refine step — without this the indicator sits on
            # "listening…" through the whole cut→clean→submit round-trip.
            await self._status("refining", "refining…")
            try:
                res = await convo.on_silence_cut(cut_text)
            except Exception:  # noqa: BLE001 — on_silence_cut self-handles; belt-and-suspenders
                log.exception("convo cleanup crashed for %s", self.user_id)
                await self._status("recording", "listening…")
                return
            await self.broadcast_json({"type": "composer", "text": res.cleaned_text})
            if res.should_submit and not res.fallback:
                # Complete thought — but only send once the user actually stops.
                self._arm_pending_submit(convo, cut_byte)
                await self._status("recording", "listening…")
            elif res.fallback:
                # LLM unavailable: composer shows raw text, no submit. Flag it so
                # a stuck convo is visibly "cleanup offline" rather than silent.
                await self._status("recording", "cleanup offline · listening…")
            else:
                await self._status("recording", "listening…")

        asyncio.create_task(_run())

    async def _silence_cut(self, joined: bytes, tail: bytes, committed_prefix: str) -> None:
        """Fire one silence-cut: finalize the utterance text, dispatch it (convo →
        LLM inner loop; PTT-continuous → raw stt_result), and reset the splice
        window so the next utterance transcribes from a fresh tail.

        Called by the fast VAD poll once ``tail`` ends with ``SILENCE_HANG_SEC`` of
        non-speech. The cut runs its OWN whisper pass on ``tail`` so the cut text
        is current even if the cosmetic streaming loop hasn't transcribed the
        final words yet. ``joined``/``tail``/``committed_prefix`` are snapshotted
        together by the poll (before its VAD await), so they are mutually
        consistent: ``committed_prefix`` covers exactly the audio before ``tail``,
        and ``cut_text = committed_prefix + transcribe(tail)`` is the full
        utterance with no overlap — even if the cosmetic loop committed the same
        tail audio meanwhile (that commit is discarded by the window reset below).
        """
        # Invalidate any in-flight streaming pass (its splice offsets are about to
        # be stale) and reset the window NOW so a concurrent pass / the next poll
        # immediately sees post-cut state.
        self._cut_epoch += 1
        cut_byte = len(joined)
        self.committed_bytes = cut_byte
        self.committed_text = ""
        self._last_partial = ""
        self._silent_passes = 0

        loop = asyncio.get_running_loop()
        try:
            async with self._whisper_lock:
                segments = await loop.run_in_executor(
                    None, _transcribe_segments, tail, self.continuous,
                )
        except Exception:  # noqa: BLE001
            log.exception("cut whisper pass failed for %s", self.user_id)
            segments = []
        tail_text = " ".join(s[0] for s in segments if s[0]).strip()
        cut_text = (committed_prefix + " " + tail_text).strip()
        if not cut_text:
            return
        log.debug(
            "stt silence-cut for %s: cut_byte=%d epoch=%d text=%r",
            self.user_id, cut_byte, self._cut_epoch, cut_text,
        )
        if self.convo is not None:
            self._dispatch_convo_cut(cut_text, cut_byte)
        else:
            await self.broadcast_json({"type": "stt_result", "text": cut_text})

    # ---- input handlers ----

    async def handle_start(
        self,
        ws: WebSocket,
        mode: Optional[str] = None,
        context: Optional[str] = None,
    ) -> None:
        # Latest "start" wins — barge-in across clients.
        self._cancel_partial_task()
        self._cancel_silence_task()
        self._cancel_pending_submit()
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
            from awm.stt.convo import get_convo_manager

            mgr = get_convo_manager()
            self.convo = mgr.new_session()
            if isinstance(context, str):
                self.convo.set_context(context)
            # Warm the opencode server ahead of the first cut (best-effort).
            asyncio.create_task(mgr.ensure_started())
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
        self._cancel_pending_submit()
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
        self._cancel_silence_task()
        self._cancel_pending_submit()
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
        try:
            while self.recording_client is ws:
                await asyncio.sleep(PARTIAL_MIN_GAP_SEC)
                if self.recording_client is not ws:
                    return
                tail = b"".join(self.pcm_chunks)[self.committed_bytes:]
                if len(tail) < min_tail_bytes:
                    continue
                # Snapshot the cut epoch before the (slow) whisper pass; if a
                # silence-cut bumps it meanwhile, our splice offsets are stale and
                # this pass must be dropped. The lock serializes model use with
                # the cut's own pass (the model is not concurrency-safe).
                epoch = self._cut_epoch
                try:
                    async with self._whisper_lock:
                        segments = await loop.run_in_executor(
                            None, _transcribe_segments, tail, self.continuous,
                        )
                except Exception:  # noqa: BLE001
                    log.exception("partial whisper pass failed for %s", self.user_id)
                    continue
                if self.recording_client is not ws:
                    return
                if epoch != self._cut_epoch:
                    # A silence-cut reset the splice window mid-pass; drop this
                    # result — the next pass starts from the post-cut tail.
                    continue
                if not segments:
                    continue
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
                self._last_partial = tail_text
                log.debug(
                    "stt partial for %s: committed_bytes=%d segs=%d text=%r",
                    self.user_id, self.committed_bytes, len(segments), merged,
                )
                await self.broadcast_json({"type": "partial", "text": merged})
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
                if len(tail) < min_tail_bytes:
                    continue
                from awm.stt import vad

                try:
                    voice_end = await loop.run_in_executor(
                        None, vad.last_speech_end_s, tail,
                    )
                except Exception:  # noqa: BLE001
                    log.exception("vad poll failed for %s", self.user_id)
                    continue
                if self.recording_client is not ws:
                    return
                tail_dur = len(tail) / SAMPLE_BYTES / SAMPLE_RATE
                if voice_end is None:
                    continue  # no voice in the tail → nothing to cut
                gap = tail_dur - voice_end
                if gap >= SILENCE_HANG_SEC:
                    log.debug(
                        "stt vad cut for %s: tail_dur=%.2f voice_end=%.2f gap=%.2f",
                        self.user_id, tail_dur, voice_end, gap,
                    )
                    await self._silence_cut(joined, tail, committed_prefix)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            log.exception("silence loop crashed for %s", self.user_id)


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
        log.exception("stt ws handler crashed for %s", user_as)
    finally:
        reg.detach(agent, websocket)
