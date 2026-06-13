"""Pydantic request/response models native to the gateway.

Feature models (scopes/rooms/messages/sessions, agents, artifacts, skills)
were split out to their owning services — co-located, not shared, since
services exchange JSON over RPC. Only gateway-native envelopes remain here.
"""

from __future__ import annotations

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class StatusResponse(BaseModel):
    status: str = "ok"
    workspace_root: str
    active_scopes: int = 0
