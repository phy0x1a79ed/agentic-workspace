"""Speech-to-text wrapper around faster-whisper.

Lazy-loads the model on first use. PCM input is expected as int16 LE
mono at 16 kHz (anything else still works but matches the mic worklet).
"""

from __future__ import annotations

import os
from typing import Optional

import numpy as np


_MODEL_NAME = os.environ.get("WHISPER_MODEL", "small.en")
_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE", "int8")


class Transcriber:
    def __init__(self, model_name: str = _MODEL_NAME, compute_type: str = _COMPUTE_TYPE):
        self.model_name = model_name
        self.compute_type = compute_type
        self._model = None

    def _ensure_loaded(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # type: ignore

            self._model = WhisperModel(
                self.model_name,
                device="cpu",
                compute_type=self.compute_type,
            )
        return self._model

    def transcribe(self, pcm_bytes: bytes, sample_rate: int, vad_filter: bool = False) -> str:
        if not pcm_bytes:
            return ""
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        model = self._ensure_loaded()
        segments, _info = model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=vad_filter,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()


_singleton: Optional[Transcriber] = None


def get_transcriber() -> Transcriber:
    global _singleton
    if _singleton is None:
        _singleton = Transcriber()
    return _singleton
