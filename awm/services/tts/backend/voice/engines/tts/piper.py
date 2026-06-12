"""Piper TTS engine (fast, clean, ideal RVC feedstock)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

ENGINE_ID = "piper"


class PiperConfig(BaseModel):
    voice_path: str | None = Field(default=None, description="Override PIPER_VOICE.")


CONFIG_SCHEMA = PiperConfig


class PiperEngine:
    live = True

    def __init__(self, cfg: PiperConfig):
        from piper_tts import PiperSynthesizer, get_piper_synthesizer
        if cfg.voice_path:
            self._synth = PiperSynthesizer(Path(cfg.voice_path))
        else:
            self._synth = get_piper_synthesizer()

    def warmup(self) -> None:
        self._synth._ensure_loaded()

    @property
    def sample_rate(self) -> int:
        return self._synth.sample_rate

    def synth(self, text: str) -> bytes:
        return self._synth.synth(text)


def make(cfg: PiperConfig) -> PiperEngine:
    return PiperEngine(cfg)
