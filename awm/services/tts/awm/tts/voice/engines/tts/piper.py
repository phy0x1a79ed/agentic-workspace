"""Piper TTS engine (fast, fully-local, ideal RVC feedstock).

Piper is the production default: synthesis runs in-process via the installed
``piper`` package (``PiperVoice`` / ``SynthesisConfig``) against a local
``.onnx`` voice model, so the raw path needs no sidecar and has the lowest
latency. RVC (Retrieval-based Voice Conversion) is *not* a peer engine — it is
an optional voice-swap post-stage folded in here: ``rvc_voice == "off"`` (the
default) returns raw piper PCM; any other value routes piper's PCM through the
kokoro-rvc sidecar's RVC stage. The sidecar route degrades gracefully — if it
is unreachable the engine falls back to the raw piper PCM rather than failing.

Voice models live under ``<AWM_DATA_DIR>/tts-models/piper/*.onnx`` (reachable
from any scope via ``.awm/data/tts-models/piper/``), mirroring the sbv2 layout.
The ``voice`` dropdown enum is filled at ``listEngines`` time by scanning that
dir (see ``app._enrich_piper_enums``); the ``rvc_voice`` enum is the sidecar's
RVC voice list with ``off`` prepended.
"""

from __future__ import annotations

import logging
import os
import struct
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

ENGINE_ID = "piper"
log = logging.getLogger("voice.engines.tts.piper")


def piper_models_dir() -> Path:
    """Directory holding the local piper ``.onnx`` voice models.

    Shared with ``app._enrich_piper_enums`` so the dropdown enum and the engine
    resolve the same set of voices. Mirrors ``engines._data_engines_root``.
    """
    base = os.environ.get("AWM_DATA_DIR") or "data/awm"
    return Path(base) / "tts-models" / "piper"


def list_piper_voices() -> list[str]:
    """Installed piper voice filenames (``*.onnx``) under the models dir."""
    d = piper_models_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.onnx"))


class PiperConfig(BaseModel):
    """Voice + RVC-stage knobs for the piper engine.

    Infra (RVC sidecar URL / TLS) lives in env vars, not here, so the config
    form only shows what affects how the voice sounds.
    """

    voice: str | None = Field(
        default=None,
        description="Local piper voice (.onnx) under the piper models dir. "
        "Enum is filled at listEngines time by scanning that dir.",
    )
    rvc_voice: str = Field(
        default="off",
        description="RVC target voice. 'off' = raw local piper (no sidecar, "
        "lowest latency). Any other value routes piper's audio through the "
        "RVC voice-swap stage.",
        # Force `enum` (not a bare string) so DynamicConfigForm renders a
        # dropdown even before the sidecar list is folded in. The real options
        # are stamped at listEngines time.
        json_schema_extra={"enum": ["off"]},
    )
    pitch: int = Field(
        default=0, ge=-24, le=24,
        description="RVC pitch shift in semitones. Active only when rvc_voice != off.",
    )
    speed: float = Field(
        default=1.0, ge=0.5, le=2.0,
        description="Speech rate. <1 faster, >1 slower (piper length_scale = 1/speed).",
    )
    voice_path: str | None = Field(
        default=None,
        description="Absolute override for the voice .onnx path (bypasses the voice dropdown / PIPER_VOICE).",
    )


CONFIG_SCHEMA = PiperConfig


def _resolve_model_path(cfg: PiperConfig) -> Path:
    """Resolve the .onnx model path from config / env / models dir.

    Priority: explicit ``voice_path`` → ``voice`` filename under the models
    dir → ``PIPER_VOICE`` env → first ``.onnx`` in the models dir.
    """
    if cfg.voice_path:
        return Path(cfg.voice_path)
    models = piper_models_dir()
    if cfg.voice:
        # Accept either a bare filename or a stem.
        name = cfg.voice if cfg.voice.endswith(".onnx") else f"{cfg.voice}.onnx"
        return models / name
    env = os.environ.get("PIPER_VOICE")
    if env:
        return Path(env)
    voices = list_piper_voices()
    if voices:
        return models / voices[0]
    raise FileNotFoundError(
        f"no piper voice model found (set voice/voice_path/PIPER_VOICE or drop a "
        f".onnx into {models})"
    )


def _verify_from_env() -> bool:
    raw = os.environ.get("TTS_RVC_VERIFY_SSL", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


class PiperEngine:
    live = True

    def __init__(self, cfg: PiperConfig):
        # Import lazily so the registry's top-level _bootstrap import of this
        # module succeeds even if `piper` or a model is absent.
        from piper import PiperVoice

        self.cfg = cfg
        self._model_path = _resolve_model_path(cfg)
        self._voice = PiperVoice.load(str(self._model_path))
        self._sample_rate = int(getattr(self._voice.config, "sample_rate", 22_050))
        self._rvc_url = os.environ.get("TTS_RVC_URL", "https://127.0.0.1:12123")

    def warmup(self) -> None:
        # Model is loaded eagerly in __init__; nothing extra to warm.
        pass

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def _synth_raw(self, text: str) -> bytes:
        from piper import SynthesisConfig

        syn = SynthesisConfig(length_scale=1.0 / max(self.cfg.speed, 0.01))
        out = bytearray()
        for chunk in self._voice.synthesize(text, syn):
            out.extend(chunk.audio_int16_bytes)
        return bytes(out)

    def _apply_rvc(self, pcm: bytes) -> bytes:
        """Route raw piper PCM through the kokoro-rvc sidecar's RVC stage.

        Best-effort: any failure (sidecar down, route absent) logs and falls
        back to the raw piper PCM so the call never crashes. The PCM-in RVC
        route shape is the kokoro-rvc sidecar's `/rvc` endpoint (assumed); if
        the sidecar only does kokoro->RVC end-to-end today, this degrades to
        raw piper until the sidecar grows a PCM-in route.
        """
        if not pcm:
            return pcm
        try:
            with httpx.Client(timeout=300.0, verify=_verify_from_env()) as cli:
                # 12-byte little-endian header: [pcm_len][pitch][sample_rate],
                # then raw int16 PCM — mirrors the sidecar's framing on the way
                # out so it can read input without a multipart parse.
                header = struct.pack("<iii", len(pcm), int(self.cfg.pitch), self._sample_rate)
                r = cli.post(
                    f"{self._rvc_url}/rvc",
                    params={"rvc_label": self.cfg.rvc_voice, "pitch": self.cfg.pitch},
                    content=header + pcm,
                    headers={"Content-Type": "application/octet-stream"},
                )
                r.raise_for_status()
                sr = r.headers.get("X-Sample-Rate")
                if sr:
                    self._sample_rate = int(sr)
                return r.content
        except Exception as exc:
            log.warning("RVC stage unreachable (rvc_voice=%s), using raw piper: %s",
                        self.cfg.rvc_voice, exc)
            return pcm

    def synth(self, text: str) -> bytes:
        if not text.strip():
            return b""
        pcm = self._synth_raw(text)
        if self.cfg.rvc_voice and self.cfg.rvc_voice != "off":
            pcm = self._apply_rvc(pcm)
        return pcm


def make(cfg: PiperConfig) -> PiperEngine:
    return PiperEngine(cfg)
