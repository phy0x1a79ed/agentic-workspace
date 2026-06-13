"""Pydantic request/response models owned by the agents service.

Covers agent spawning, tracked agent sessions, live agent-runtime state
(pid/status/model/context), the room-scoped agent view, and the agent
slash-command surface.
"""

from __future__ import annotations

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Agent Spawning
# ---------------------------------------------------------------------------

class AgentSpawnRequest(BaseModel):
    project: str
    scope: str
    prompt: str | None = None
    agent_cli: str | None = None


class AgentSpawnResponse(BaseModel):
    project: str
    scope: str
    pid: int
    agent_cli: str
    message: str


# ---------------------------------------------------------------------------
# Agent Sessions (live, tracked, addressable)
# ---------------------------------------------------------------------------

class AgentSessionCreateRequest(BaseModel):
    project: str
    scope: str
    prompt: str | None = None
    agent_cli: str | None = None  # defaults to "claude"


class AgentSessionInfo(BaseModel):
    id: int
    project: str
    scope: str
    pid: int
    status: str  # starting|running|stopping|exited|killed|orphaned
    agent_cli: str
    started_at: str
    exited_at: str | None = None
    exit_code: int | None = None
    attached: bool = False


class AgentSessionListResponse(BaseModel):
    sessions: list[AgentSessionInfo]
    total: int


class AgentSessionActionResponse(BaseModel):
    id: int
    status: str
    message: str


# ---------------------------------------------------------------------------
# Live agent runtime state (room-scoped view of agents)
# ---------------------------------------------------------------------------

class LiveAgentState(BaseModel):
    pid: int | None = None
    status: str | None = None
    started_at: str | None = None
    exited_at: str | None = None
    exit_code: int | None = None
    agent_cli: str | None = None
    permission_mode: str | None = None
    model: str | None = None
    effort: str | None = None
    claude_session_id: str | None = None
    context_used: int | None = None
    context_max: int | None = None


class RoomAgentInfo(BaseModel):
    scope: str
    kind: str  # 'scope' (local) | 'shadow_peer' (remote)
    identifier: str
    joined_at: str
    live: LiveAgentState | None = None  # None for shadow_peer


class RoomAgentsResponse(BaseModel):
    agents: list[RoomAgentInfo]


# ---------------------------------------------------------------------------
# Agent slash-command surface
# ---------------------------------------------------------------------------

class SlashCommandInfo(BaseModel):
    name: str          # leading slash, e.g. "/restart"
    args: str          # display string, e.g. "[mode]"
    description: str


class AgentSlashCatalog(BaseModel):
    server: list[SlashCommandInfo]
    claude: list[str]  # bare claude command names (no leading slash)


class AgentSlashRequest(BaseModel):
    cmd: str           # full command line including leading slash


class AgentSlashResponse(BaseModel):
    handled: bool      # True = server command; False = forwarded to claude
    result: str        # result message (empty for forwarded)
