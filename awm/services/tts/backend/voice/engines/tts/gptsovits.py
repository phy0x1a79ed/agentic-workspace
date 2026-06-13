"""GPT-SoVITS engine.

Thin HTTP client of `tts_gptsovits_service.py` (default :7841). Uses
the official api_v2 `/tts` endpoint shape (body differs from the other
sidecars; returns a WAV).
"""

from __future__ import annotations

import os

import httpx
from pydantic import BaseModel, Field

ENGINE_ID = "gptsovits"


class GPTSoVITSConfig(BaseModel):
    """Voice-shaping knobs for the gpt-sovits sidecar.

    Infra (sidecar URL) lives in env vars rather than here so the
    user-facing config form only shows voice-shaping fields.
    """

    ref_audio_path: str | None = Field(default=None)
    prompt_text: str | None = Field(default=None)
    text_lang: str = Field(default="en")
    prompt_lang: str = Field(default="en")
    top_k: int = Field(default=5, ge=1, le=100)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    text_split_method: str = Field(default="cut5")
    batch_size: int = Field(default=1, ge=1, le=16)


CONFIG_SCHEMA = GPTSoVITSConfig


class GPTSoVITSEngine:
    live = True

    def __init__(self, cfg: GPTSoVITSConfig):
        self.cfg = cfg
        self._url = os.environ.get("GPTSOVITS_URL", "http://127.0.0.1:7841")
        self._client = httpx.Client(timeout=300.0)
        self._sample_rate = 32_000

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
        if not self.cfg.ref_audio_path or not self.cfg.prompt_text:
            raise RuntimeError(
                "gptsovits requires ref_audio_path + prompt_text in engine params"
            )
        body = {
            "text": text,
            "text_lang": self.cfg.text_lang,
            "ref_audio_path": self.cfg.ref_audio_path,
            "prompt_text": self.cfg.prompt_text,
            "prompt_lang": self.cfg.prompt_lang,
            "top_k": self.cfg.top_k,
            "top_p": self.cfg.top_p,
            "temperature": self.cfg.temperature,
            "text_split_method": self.cfg.text_split_method,
            "batch_size": self.cfg.batch_size,
            "streaming_mode": False,
            "media_type": "wav",
        }
        r = self._client.post(f"{self._url}/tts", json=body)
        r.raise_for_status()
        from voice.engines.tts.f5tts import _wav_to_pcm
        return _wav_to_pcm(r.content, self)


def make(cfg: GPTSoVITSConfig) -> GPTSoVITSEngine:
    return GPTSoVITSEngine(cfg)
