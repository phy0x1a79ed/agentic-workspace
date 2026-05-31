"""Streaming STT via sherpa-onnx OnlineRecognizer (streaming zipformer).

Per WS connection, code does:
    session = StreamingSession(get_streaming_recognizer())
    session.start()
    for chunk in incoming_pcm_chunks:    # int16 LE @ 16kHz
        partial = session.accept(chunk)  # may be ""; same value when no growth
    final = session.finalize()           # flushes encoder; returns final text
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

import numpy as np


log = logging.getLogger("voice-demo.stt_stream")

_MODEL_DIR = Path(
    os.environ.get(
        "SHERPA_MODEL_DIR",
        str(Path(__file__).parent / "models" / "sherpa-onnx-streaming-zipformer-en-2023-06-26"),
    )
)
_CHUNK_TAG = os.environ.get("SHERPA_CHUNK_TAG", "chunk-16-left-128")
_NUM_THREADS = int(os.environ.get("SHERPA_NUM_THREADS", "2"))


def _model_files() -> dict[str, str]:
    enc = _MODEL_DIR / f"encoder-epoch-99-avg-1-{_CHUNK_TAG}.int8.onnx"
    dec = _MODEL_DIR / f"decoder-epoch-99-avg-1-{_CHUNK_TAG}.onnx"
    joi = _MODEL_DIR / f"joiner-epoch-99-avg-1-{_CHUNK_TAG}.onnx"
    tok = _MODEL_DIR / "tokens.txt"
    for p in (enc, dec, joi, tok):
        if not p.exists():
            raise FileNotFoundError(f"sherpa model file missing: {p}")
    return {"encoder": str(enc), "decoder": str(dec), "joiner": str(joi), "tokens": str(tok)}


class StreamingRecognizer:
    def __init__(self):
        self._recognizer = None
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        if self._recognizer is None:
            with self._lock:
                if self._recognizer is None:
                    import sherpa_onnx  # type: ignore
                    files = _model_files()
                    self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                        tokens=files["tokens"],
                        encoder=files["encoder"],
                        decoder=files["decoder"],
                        joiner=files["joiner"],
                        num_threads=_NUM_THREADS,
                        sample_rate=16000,
                        feature_dim=80,
                        decoding_method="greedy_search",
                        provider="cpu",
                        enable_endpoint_detection=False,
                    )
                    log.info("sherpa-onnx streaming recognizer ready (%s, %d threads)",
                             _CHUNK_TAG, _NUM_THREADS)
        return self._recognizer

    def new_session(self) -> "StreamingSession":
        rec = self._ensure_loaded()
        return StreamingSession(rec)


class StreamingSession:
    """One push-to-talk turn. Not thread-safe — caller serializes."""

    def __init__(self, recognizer):
        self._rec = recognizer
        self._stream = recognizer.create_stream()
        self._last_text = ""
        self._finalized = False

    def accept(self, pcm_bytes: bytes) -> str:
        """Feed one PCM chunk (int16 LE @ 16k). Returns current partial text.

        The return value is the cumulative transcript so far, not just the
        new delta. Compare against the previous return to detect growth.
        """
        if self._finalized or not pcm_bytes:
            return self._last_text
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        self._stream.accept_waveform(16000, samples)
        while self._rec.is_ready(self._stream):
            self._rec.decode_stream(self._stream)
        self._last_text = self._rec.get_result(self._stream).strip()
        return self._last_text

    def finalize(self) -> str:
        """Pad with trailing silence to flush encoder right-context, then
        signal input_finished and drain. Returns final transcript."""
        if self._finalized:
            return self._last_text
        # 0.5s of trailing silence so the encoder's right-context drains.
        pad = np.zeros(8000, dtype=np.float32)
        self._stream.accept_waveform(16000, pad)
        self._stream.input_finished()
        while self._rec.is_ready(self._stream):
            self._rec.decode_stream(self._stream)
        self._last_text = self._rec.get_result(self._stream).strip()
        self._finalized = True
        return self._last_text


_singleton: Optional[StreamingRecognizer] = None


def get_streaming_recognizer() -> StreamingRecognizer:
    global _singleton
    if _singleton is None:
        _singleton = StreamingRecognizer()
    return _singleton


# ─── whisper rolling-window streaming ─────────────────────────────────────────
#
# Re-transcribes the accumulated PCM every ~800ms in a worker thread, emits
# partials via callback. Trades CPU for whisper-quality partials.

_WHISPER_STREAM_MODEL = os.environ.get("WHISPER_STREAM_MODEL", os.environ.get("WHISPER_MODEL", "base.en"))
_WHISPER_STREAM_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
_WHISPER_STREAM_INTERVAL_S = float(os.environ.get("WHISPER_STREAM_INTERVAL", "0.8"))
_WHISPER_STREAM_MIN_GROWTH_S = float(os.environ.get("WHISPER_STREAM_MIN_GROWTH", "0.4"))


class WhisperStreamingRecognizer:
    def __init__(self):
        self._model = None
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from faster_whisper import WhisperModel  # type: ignore
                    self._model = WhisperModel(
                        _WHISPER_STREAM_MODEL,
                        device="cpu",
                        compute_type=_WHISPER_STREAM_COMPUTE,
                    )
                    log.info("whisper streaming model ready (%s, %s)",
                             _WHISPER_STREAM_MODEL, _WHISPER_STREAM_COMPUTE)
        return self._model

    def new_session(self, on_partial=None) -> "WhisperStreamingSession":
        model = self._ensure_loaded()
        return WhisperStreamingSession(model, on_partial)


class WhisperStreamingSession:
    """Background-thread whisper transcriber for one PTT turn.

    Caller pushes PCM via accept(); a worker thread re-transcribes the
    accumulated buffer at fixed cadence and invokes on_partial(text).
    finalize() stops the worker, runs one last full-buffer transcribe,
    returns the final transcript.
    """

    def __init__(self, model, on_partial=None):
        self._model = model
        self._on_partial = on_partial
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._final_requested = threading.Event()
        self._final_done = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._last_text = ""
        self._last_transcribed_bytes = 0
        self._started = False
        self._finalized = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._worker.start()

    def accept(self, pcm_bytes: bytes) -> None:
        if self._finalized or not pcm_bytes:
            return
        with self._lock:
            self._buffer.extend(pcm_bytes)
        self._wake.set()

    def _transcribe(self, pcm: bytes) -> str:
        import numpy as np
        if not pcm:
            return ""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segs, _ = self._model.transcribe(
            audio, language="en", beam_size=1, vad_filter=False
        )
        return " ".join(s.text.strip() for s in segs).strip()

    def _run(self) -> None:
        """Single worker thread: serializes all whisper inferences.

        Partial cadence: poll every INTERVAL seconds, transcribe if buffer
        grew by at least MIN_GROWTH seconds since the last pass. When
        finalize is requested, do one more pass on the full buffer and
        exit — guaranteeing no two inferences run concurrently.
        """
        min_growth_bytes = int(16000 * 2 * _WHISPER_STREAM_MIN_GROWTH_S)
        while True:
            self._wake.wait(_WHISPER_STREAM_INTERVAL_S)
            self._wake.clear()

            is_final = self._final_requested.is_set()
            with self._lock:
                buf_len = len(self._buffer)
                if not is_final and buf_len < self._last_transcribed_bytes + min_growth_bytes:
                    continue
                snapshot = bytes(self._buffer)
                self._last_transcribed_bytes = buf_len

            try:
                text = self._transcribe(snapshot)
            except Exception:
                log.exception("whisper-stream transcribe failed (final=%s)", is_final)
                if is_final:
                    self._final_done.set()
                    return
                continue

            if text:
                self._last_text = text
                if not is_final and self._on_partial is not None:
                    try:
                        self._on_partial(text)
                    except Exception:
                        log.exception("on_partial callback failed")

            if is_final:
                self._final_done.set()
                return

    def finalize(self) -> str:
        if self._finalized:
            return self._last_text
        self._finalized = True
        if not self._started:
            # No worker spun up — fall back to a direct synchronous transcribe.
            with self._lock:
                snapshot = bytes(self._buffer)
            try:
                self._last_text = self._transcribe(snapshot)
            except Exception:
                log.exception("whisper-stream sync finalize failed")
            return self._last_text
        self._final_requested.set()
        self._wake.set()
        self._final_done.wait(timeout=30)
        return self._last_text


_whisper_stream_singleton: Optional[WhisperStreamingRecognizer] = None


def get_whisper_streaming_recognizer() -> WhisperStreamingRecognizer:
    global _whisper_stream_singleton
    if _whisper_stream_singleton is None:
        _whisper_stream_singleton = WhisperStreamingRecognizer()
    return _whisper_stream_singleton
