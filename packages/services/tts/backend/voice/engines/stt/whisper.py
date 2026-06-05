"""faster-whisper non-streaming STT.

Wraps the existing demo.stt.Transcriber for the plugin contract.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

ENGINE_ID = "whisper"


class WhisperConfig(BaseModel):
    model: str = Field(default="small.en", description="faster-whisper model name.")
    compute_type: str = Field(default="int8")


CONFIG_SCHEMA = WhisperConfig


class WhisperEngine:
    """Non-streaming whisper: caller buffers PCM, calls finalize_pcm()."""

    streaming = False

    def __init__(self, cfg: WhisperConfig):
        # Reuse the demo singleton so model load cost is shared across calls.
        from stt import Transcriber, get_transcriber
        existing = get_transcriber()
        if existing.model_name == cfg.model and existing.compute_type == cfg.compute_type:
            self._transcriber = existing
        else:
            self._transcriber = Transcriber(cfg.model, cfg.compute_type)

    def warmup(self) -> None:
        self._transcriber._ensure_loaded()

    def transcribe(self, pcm: bytes, sample_rate: int = 16000) -> str:
        return self._transcriber.transcribe(pcm, sample_rate)


def make(cfg: WhisperConfig) -> WhisperEngine:
    return WhisperEngine(cfg)
