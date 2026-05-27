"""OpenRouter LLM engine.

Wraps demo.openrouter_session.OpenRouterSession (which already exposes
the unified start/send/events/stop interface used by the orchestrator).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

ENGINE_ID = "openrouter"


class OpenRouterEngineConfig(BaseModel):
    model: str | None = Field(default=None, description="OpenRouter model id.")
    api_key: str | None = Field(default=None, description="Override OPENROUTER_API_KEY.")
    persona_path: str | None = Field(default=None)
    log_path: str | None = Field(default=None)


CONFIG_SCHEMA = OpenRouterEngineConfig


def make(cfg: OpenRouterEngineConfig):
    from openrouter_session import OpenRouterSession
    return OpenRouterSession(
        model=cfg.model,
        api_key=cfg.api_key,
        persona_path=Path(cfg.persona_path) if cfg.persona_path else None,
        log_path=Path(cfg.log_path) if cfg.log_path else None,
    )
