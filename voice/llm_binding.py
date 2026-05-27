"""LLM binding — the seam between the voice call and whatever provides
the LLM turn.

Two discriminator kinds:

- `inline`: instantiate an engine from `voice.engines.llm` in-process.
  The orchestrator owns the conversation history. **Implemented today.**
- `awm_room`: the call attaches to an awm room; user transcripts post
  there (as `post_as`), agent posts in the room (from `subscribe_as`)
  get TTS'd back through the open WebSocket. **Reserved — not
  implemented in this plan.** When implemented, add
  `voice/engines/llm/awm_room.py` and flip the NotImplementedError
  below to instantiate it.

The shape of `AwmRoomBinding` is spelled out so future awm integration
doesn't have to redesign the surface — just implement the binding.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from voice.config import EngineRef
from voice import engines


class InlineBinding(BaseModel):
    """LLM runs in the voice service's process."""

    kind: Literal["inline"] = "inline"
    engine: str = Field(..., description="LLM engine id (e.g. 'openrouter', 'claude').")
    params: dict[str, Any] = Field(default_factory=dict)

    def connect(self):
        return engines.make_llm(EngineRef(engine=self.engine, params=self.params))


class AwmRoomBinding(BaseModel):
    """LLM = an agent in an awm room. Reserved future extension point."""

    kind: Literal["awm_room"] = "awm_room"
    room_id: str
    post_as: str = Field(..., description="Identity user transcripts post under.")
    subscribe_as: str = Field(..., description="Identity that receives the agent's posts.")
    voice_service_url: str | None = Field(
        default=None,
        description=(
            "URL the awm room poster can dial back to if this binding ever "
            "becomes bidirectional (e.g. agent-initiated voice). "
            "Optional; not used today."
        ),
    )

    def connect(self):
        raise NotImplementedError(
            "awm_room llm_binding is reserved for a future plan. "
            "Add voice/engines/llm/awm_room.py and update "
            "voice/llm_binding.py to instantiate it when ready."
        )


def parse_binding(payload: dict) -> InlineBinding | AwmRoomBinding:
    kind = payload.get("kind")
    if kind == "inline":
        return InlineBinding(**payload)
    if kind == "awm_room":
        return AwmRoomBinding(**payload)
    raise ValueError(f"unknown llm_binding kind {kind!r}; expected 'inline' or 'awm_room'")
