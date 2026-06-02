"""Kokoro + RVC TTS engine.

Thin HTTP client of the `tts_rvc_service.py` sidecar (default :12103,
or :12123 on the prod deploy). Calls /stream and concatenates the
length-prefixed PCM frames into a single buffer per `synth(text)` call.

For the live PTT loop we collect the stream; the comparison page at
/voices/ still does its own progressive playback against the sidecar.
"""

from __future__ import annotations

import logging
import os
import struct

import httpx
from pydantic import BaseModel, Field

ENGINE_ID = "kokoro_rvc"
log = logging.getLogger("voice.engines.tts.kokoro_rvc")


class KokoroRvcConfig(BaseModel):
    """Voice-shaping knobs for the kokoro+rvc sidecar.

    Infra (sidecar URL, TLS verification) lives in env vars rather than
    here so the user-facing config form only shows what affects how the
    voice sounds.
    """

    tts_voice: str = Field(default="bf_emma")
    rvc_label: str | None = Field(default="chelly_egoist")
    pitch: int = Field(default=0, ge=-24, le=24)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


CONFIG_SCHEMA = KokoroRvcConfig


def _verify_from_env() -> bool:
    raw = os.environ.get("TTS_RVC_VERIFY_SSL", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


class KokoroRvcEngine:
    live = True

    def __init__(self, cfg: KokoroRvcConfig):
        self.cfg = cfg
        # Sidecar URL + TLS verify are infra, not voice knobs.
        self._url = os.environ.get("TTS_RVC_URL", "https://127.0.0.1:12123")
        self._sample_rate = 24_000  # default, refreshed from response header
        self._client = httpx.Client(timeout=300.0, verify=_verify_from_env())

    def warmup(self) -> None:
        try:
            r = self._client.get(f"{self._url}/health", timeout=5.0)
            r.raise_for_status()
        except Exception as exc:
            log.warning("tts_rvc sidecar health check failed: %s", exc)

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def synth(self, text: str) -> bytes:
        if not text.strip():
            return b""
        body = {
            "text": text,
            "tts_voice": self.cfg.tts_voice,
            "rvc_label": self.cfg.rvc_label,
            "pitch": self.cfg.pitch,
            "speed": self.cfg.speed,
        }
        out = bytearray()
        with self._client.stream("POST", f"{self._url}/stream", json=body) as r:
            r.raise_for_status()
            sr = r.headers.get("X-Sample-Rate")
            if sr:
                self._sample_rate = int(sr)
            buf = bytearray()
            for piece in r.iter_bytes():
                buf.extend(piece)
                # tts_rvc_service emits 12-byte headers: [len][head_overlap][tail_overlap]
                while len(buf) >= 12:
                    n = struct.unpack("<I", buf[:4])[0]
                    if len(buf) < 12 + n:
                        break
                    pcm_bytes = bytes(buf[12:12 + n])
                    del buf[:12 + n]
                    out.extend(pcm_bytes)
        return bytes(out)


def make(cfg: KokoroRvcConfig) -> KokoroRvcEngine:
    return KokoroRvcEngine(cfg)
