"""Style-Bert-VITS2 engine.

Thin HTTP client of `tts_sbv2_service.py` (default :7843). The sidecar
loads the WarriorMama777/GLaDOS_TTS Style-Bert-VITS2 checkpoint and
synthesises authentic GLaDOS prosody from text alone — no reference
WAV or transcript needed, unlike f5tts / gptsovits. The knobs below
match the sidecar's `/synth` payload (see
`demo/tts_sbv2_service.py::synth`).
"""

from __future__ import annotations

import os

import httpx
from pydantic import BaseModel, Field

ENGINE_ID = "sbv2"


class SBV2Config(BaseModel):
    """Prosody knobs surfaced by the sbv2 sidecar's /synth endpoint.

    Bounds are tuned for the GLaDOS checkpoint — the defaults reproduce
    the slow, deliberate, mechanical-deliberate delivery; widening the
    ranges lets the operator dial in other characters if the sidecar is
    later loaded with a different model.
    """

    url: str = Field(default_factory=lambda: os.environ.get("SBV2_URL", "http://127.0.0.1:7843"))
    sdp_ratio:    float = Field(default=0.2, ge=0.0, le=1.0)
    length_scale: float = Field(default=1.1, ge=0.5, le=2.0)
    noise:        float = Field(default=0.4, ge=0.0, le=1.0)
    noise_w:      float = Field(default=0.8, ge=0.0, le=1.0)
    style:        str   = Field(default="Standard")
    style_weight: float = Field(default=1.0, ge=0.0, le=2.0)
    language:     str   = Field(default="en")


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
        body = {
            "text": text,
            "sdp_ratio": self.cfg.sdp_ratio,
            "length_scale": self.cfg.length_scale,
            "noise": self.cfg.noise,
            "noise_w": self.cfg.noise_w,
            "style": self.cfg.style,
            "style_weight": self.cfg.style_weight,
        }
        r = self._client.post(f"{self.cfg.url}/synth", json=body)
        r.raise_for_status()
        # Pick the sample rate up from the response header so the AudioContext
        # decodes correctly even if the checkpoint isn't 44.1 kHz.
        sr = r.headers.get("X-Sample-Rate")
        if sr:
            try:
                self._sample_rate = int(sr)
            except ValueError:
                pass
        from voice.engines.tts.f5tts import _wav_to_pcm
        return _wav_to_pcm(r.content, self)


def make(cfg: SBV2Config) -> SBV2Engine:
    return SBV2Engine(cfg)
