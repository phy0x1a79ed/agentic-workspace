"""Pocket-TTS engine (kyutai). Slower than piper, more natural."""

from __future__ import annotations

from pydantic import BaseModel, Field

ENGINE_ID = "pocket"


class PocketConfig(BaseModel):
    voice: str | None = Field(default=None, description="Pocket-tts voice name, path, or hf:// URI.")


CONFIG_SCHEMA = PocketConfig


class PocketEngine:
    live = True

    def __init__(self, cfg: PocketConfig):
        from tts import Synthesizer, get_synthesizer
        if cfg.voice:
            self._synth = Synthesizer(cfg.voice)
        else:
            self._synth = get_synthesizer()

    def warmup(self) -> None:
        self._synth._ensure_loaded()

    @property
    def sample_rate(self) -> int:
        return self._synth.sample_rate

    def synth(self, text: str) -> bytes:
        return self._synth.synth(text)


def make(cfg: PocketConfig) -> PocketEngine:
    return PocketEngine(cfg)
