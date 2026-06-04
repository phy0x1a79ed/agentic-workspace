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
from typing import Literal

import httpx
from pydantic import BaseModel, Field

ENGINE_ID = "sbv2"

# Styles enumerated from the Portal_GLaDOS_v1 checkpoint's config.json
# (data.style2id). Hardcoded so the UI gets a dropdown without an
# extra round-trip to the sidecar. If the sidecar is reloaded with a
# different model whose style vectors disagree, update this list.
GLaDOSStyle = Literal["Neutral", "Standard", "Deep", "Light", "Standard_02"]

# The sidecar's /synth endpoint pins `language=Languages.EN` regardless
# of payload (see demo/tts_sbv2_service.py::synth), so "en" is the only
# honest option to expose today.
SBV2Language = Literal["en"]


class SBV2Config(BaseModel):
    """Prosody knobs surfaced by the sbv2 sidecar's /synth endpoint.

    Bounds are tuned for the GLaDOS checkpoint — the defaults reproduce
    the slow, deliberate, mechanical-deliberate delivery; widening the
    ranges lets the operator dial in other characters if the sidecar is
    later loaded with a different model.

    Infra knobs (sidecar URL) live in env vars, not here — they're not
    voice-shaping parameters and the user-facing config form should
    only show what affects how the voice sounds.
    """

    sdp_ratio:    float = Field(
        default=0.2, ge=0.0, le=1.0,
        description="Mix between stochastic and deterministic duration prediction. 0 = fully deterministic.",
    )
    length_scale: float = Field(
        default=1.1, ge=0.5, le=2.0,
        description="Overall speech rate. <1 faster, >1 slower.",
    )
    noise:        float = Field(
        default=0.4, ge=0.0, le=1.0,
        description="VITS noise_scale — pitch/intonation variability. Higher = more melodic variation per utterance.",
    )
    noise_w:      float = Field(
        default=0.8, ge=0.0, le=1.0,
        description="VITS noise_scale_w — duration/rhythm variability of the stochastic duration predictor.",
    )
    style:        GLaDOSStyle = Field(
        default="Standard",
        description="Style vector from the checkpoint. Standard = canonical GLaDOS; Deep/Light shift timbre.",
    )
    style_weight: float = Field(
        default=1.0, ge=0.0, le=2.0,
        description="How strongly the selected style is applied. 0 = ignore style, 1 = nominal.",
    )
    language:     SBV2Language = Field(
        default="en",
        description="Source language for g2p. Sidecar currently pins EN; multi-lingual checkpoints would expand this.",
        # Force `enum` (not `const`) in the JSON Schema so the UI's
        # DynamicConfigForm picks it up as a dropdown.
        json_schema_extra={"enum": ["en"]},
    )


CONFIG_SCHEMA = SBV2Config


class SBV2Engine:
    live = True

    def __init__(self, cfg: SBV2Config):
        self.cfg = cfg
        # Sidecar URL is infra, not a voice knob — read from env so it
        # stays out of the user-facing config schema.
        self._url = os.environ.get("SBV2_URL", "http://127.0.0.1:7843")
        self._client = httpx.Client(timeout=300.0)
        self._sample_rate = 44_100

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
        body = {
            "text": text,
            "sdp_ratio": self.cfg.sdp_ratio,
            "length_scale": self.cfg.length_scale,
            "noise": self.cfg.noise,
            "noise_w": self.cfg.noise_w,
            "style": self.cfg.style,
            "style_weight": self.cfg.style_weight,
        }
        r = self._client.post(f"{self._url}/synth", json=body)
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
