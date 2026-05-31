"""Per-call configuration.

`CallConfig` is the wire shape POSTed to `/call/start`. Each
sub-config picks an engine by name (matches an engine module's
`ENGINE_ID`) plus a free-form `params` dict that is validated against
the engine's `CONFIG_SCHEMA` at instantiation time.

Resolution order (later wins):
1. defaults baked into each engine's CONFIG_SCHEMA
2. file profile (from_file)
3. env (from_env) — back-compat with STT_BACKEND/TTS_BACKEND/LLM_BACKEND
4. per-call overrides (merge)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class EngineRef(BaseModel):
    """One engine selection + its engine-specific params."""

    engine: str = Field(..., description="Engine ID, e.g. 'piper', 'whisper', 'openrouter'.")
    params: dict[str, Any] = Field(default_factory=dict)


class CallConfig(BaseModel):
    stt: EngineRef
    tts: EngineRef
    llm: EngineRef | None = None  # may come from llm_binding instead

    def merge(self, overrides: dict[str, Any]) -> "CallConfig":
        """Return a copy with `overrides` deep-merged on top."""
        base = self.model_dump()
        _deep_merge(base, overrides)
        return CallConfig(**base)


def _deep_merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def default_config() -> CallConfig:
    """Defaults wired for the dev page out-of-the-box."""
    return CallConfig(
        stt=EngineRef(engine="whisper", params={}),
        tts=EngineRef(engine="piper", params={}),
        llm=EngineRef(engine="openrouter", params={}),
    )


def from_env(base: CallConfig | None = None) -> CallConfig:
    """Apply the legacy env vars (STT_BACKEND / TTS_BACKEND / LLM_BACKEND
    + a few per-engine knobs) as overrides on top of `base` (or defaults)."""
    cfg = base or default_config()
    overrides: dict[str, Any] = {}
    if v := os.environ.get("STT_BACKEND"):
        overrides.setdefault("stt", {})["engine"] = v.lower()
    if v := os.environ.get("TTS_BACKEND"):
        overrides.setdefault("tts", {})["engine"] = v.lower()
    if v := os.environ.get("LLM_BACKEND"):
        overrides.setdefault("llm", {})["engine"] = v.lower()
    return cfg.merge(overrides) if overrides else cfg


def from_file(path: str | Path, base: CallConfig | None = None) -> CallConfig:
    """Load a JSON profile and merge on top of `base` (or defaults)."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    cfg = base or default_config()
    return cfg.merge(data)
