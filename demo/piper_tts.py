"""Piper TTS wrapper. Selected when TTS_BACKEND=piper.

Output is clean, low-prosody, 22050 Hz mono int16 PCM — ideal as RVC
feedstock. Voice model lives at demo/voices/piper/{name}.onnx with the
sibling .onnx.json config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import numpy as np


_DEMO_DIR = Path(__file__).parent
_DEFAULT_VOICE_PATH = _DEMO_DIR / "voices" / "piper" / "en_US-lessac-medium.onnx"

_VOICE_PATH = Path(os.environ.get("PIPER_VOICE", str(_DEFAULT_VOICE_PATH)))


class PiperSynthesizer:
    def __init__(self, voice_path: Optional[Path] = None):
        self.voice_path = Path(voice_path or _VOICE_PATH)
        self._voice = None
        self._sample_rate: Optional[int] = None

    def _ensure_loaded(self):
        if self._voice is None:
            from piper import PiperVoice

            self._voice = PiperVoice.load(str(self.voice_path))
            self._sample_rate = int(self._voice.config.sample_rate)
        return self._voice

    @property
    def sample_rate(self) -> int:
        if self._sample_rate is None:
            self._ensure_loaded()
        assert self._sample_rate is not None
        return self._sample_rate

    def synth(self, text: str) -> bytes:
        """Synth text → raw int16 mono PCM at ``self.sample_rate``."""
        if not text.strip():
            return b""
        voice = self._ensure_loaded()
        parts: list[np.ndarray] = []
        for chunk in voice.synthesize(text):
            parts.append(chunk.audio_int16_array)
        if not parts:
            return b""
        return np.concatenate(parts).astype(np.int16).tobytes()


_singleton: Optional[PiperSynthesizer] = None


def get_piper_synthesizer() -> PiperSynthesizer:
    global _singleton
    if _singleton is None:
        _singleton = PiperSynthesizer()
    return _singleton
