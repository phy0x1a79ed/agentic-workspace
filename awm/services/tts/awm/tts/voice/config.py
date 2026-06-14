"""Engine selection shape.

``EngineRef`` is the (engine-id + free-form ``params``) pair the registry
consumes: ``params`` is validated against the engine's ``CONFIG_SCHEMA`` at
instantiation time.

This module used to also carry ``CallConfig`` (stt/tts/llm triple) and the
defaults/env/file resolution chain for the standalone STT conversation loop.
That loop (``orchestrator``/``service``/``llm_binding`` + the STT engines) was
pruned in the modular migration — ``ptt`` owns STT now — so only the TTS
``EngineRef`` selection shape remains here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EngineRef(BaseModel):
    """One engine selection + its engine-specific params."""

    engine: str = Field(..., description="Engine ID, e.g. 'piper', 'kokoro_rvc', 'sbv2'.")
    params: dict[str, Any] = Field(default_factory=dict)
