"""Claude CLI LLM engine.

Wraps demo.claude_session.ClaudeSession (persistent `claude --stream-json`
subprocess).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

ENGINE_ID = "claude"


class ClaudeEngineConfig(BaseModel):
    log_path: str | None = Field(default=None)


CONFIG_SCHEMA = ClaudeEngineConfig


def make(cfg: ClaudeEngineConfig):
    from claude_session import ClaudeSession
    s = ClaudeSession()
    if cfg.log_path:
        s.log_path = Path(cfg.log_path)
    return s
