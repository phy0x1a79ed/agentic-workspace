"""Style-Bert-VITS2 engine.

Thin HTTP client of `tts_sbv2_service.py` (default :7843). Same
{text, ref_wav_path, ref_text, language} shape as f5tts.
"""

from __future__ import annotations

import os

import httpx
from pydantic import BaseModel, Field

ENGINE_ID = "sbv2"


class SBV2Config(BaseModel):
    url: str = Field(default_factory=lambda: os.environ.get("SBV2_URL", "http://127.0.0.1:7843"))
    ref_wav_path: str | None = Field(default=None)
    ref_text: str | None = Field(default=None)
    language: str = Field(default="en")


CONFIG_SCHEMA = SBV2Config


class SBV2Engine:
    live = True

    def __init__(self, cfg: SBV2Config):
        self.cfg = cfg
        self._client = httpx.Client(timeout=300.0)
        self._sample_rate = 44_100

    def warmup(self) -> None:
        try:
            self._client.get(f"{self.cfg.url}/health", timeout=5.0)
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
                "sbv2 requires ref_wav_path + ref_text in engine params"
            )
        body = {
            "text": text,
            "ref_wav_path": self.cfg.ref_wav_path,
            "ref_text": self.cfg.ref_text,
            "language": self.cfg.language,
        }
        r = self._client.post(f"{self.cfg.url}/synth", json=body)
        r.raise_for_status()
        from voice.engines.tts.f5tts import _wav_to_pcm
        return _wav_to_pcm(r.content, self)


def make(cfg: SBV2Config) -> SBV2Engine:
    return SBV2Engine(cfg)
