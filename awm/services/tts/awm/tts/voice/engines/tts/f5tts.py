"""F5-TTS engine.

Thin HTTP client of `tts_f5tts_service.py` (default :7842). Zero-shot
voice cloning: requires a reference WAV + transcript. Designed for
the native-TTS eval; in the live PTT loop it works if a sensible
default ref is configured.
"""

from __future__ import annotations

import io
import os
import wave

import httpx
import numpy as np
from pydantic import BaseModel, Field

ENGINE_ID = "f5tts"


class F5TTSConfig(BaseModel):
    """Voice-shaping knobs for the f5tts sidecar.

    Infra (sidecar URL) lives in env vars rather than here so the
    user-facing config form only shows voice-shaping fields.
    """

    ref_wav_path: str | None = Field(default=None)
    ref_text: str | None = Field(default=None)
    language: str = Field(default="en")


CONFIG_SCHEMA = F5TTSConfig


class F5TTSEngine:
    live = True

    def __init__(self, cfg: F5TTSConfig):
        self.cfg = cfg
        self._url = os.environ.get("F5TTS_URL", "http://127.0.0.1:7842")
        self._client = httpx.Client(timeout=300.0)
        self._sample_rate = 24_000

    def warmup(self) -> None:
        try:
            self._client.get(f"{self._url}/health", timeout=5.0)
        except Exception:
            pass

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def synth(self, text: str) -> bytes:
        if not text.strip():
            return b""
        if not self.cfg.ref_wav_path or not self.cfg.ref_text:
            raise RuntimeError(
                "f5tts requires ref_wav_path + ref_text in engine params"
            )
        body = {
            "text": text,
            "ref_wav_path": self.cfg.ref_wav_path,
            "ref_text": self.cfg.ref_text,
            "language": self.cfg.language,
        }
        r = self._client.post(f"{self._url}/synth", json=body)
        r.raise_for_status()
        return _wav_to_pcm(r.content, self)


def _wav_to_pcm(wav_bytes: bytes, engine) -> bytes:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        sr = w.getframerate()
        frames = w.readframes(w.getnframes())
        sw = w.getsampwidth()
        nch = w.getnchannels()
    engine._sample_rate = sr
    arr = np.frombuffer(frames, dtype=np.int16 if sw == 2 else np.int32)
    if nch == 2:
        arr = arr.reshape(-1, 2).mean(axis=1).astype(arr.dtype)
    return arr.astype(np.int16).tobytes()


def make(cfg: F5TTSConfig) -> F5TTSEngine:
    return F5TTSEngine(cfg)
