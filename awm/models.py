"""Pydantic request/response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class StatusResponse(BaseModel):
    status: str = "ok"
    workspace_root: str
    active_scopes: int = 0


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

class ProjectCreateRequest(BaseModel):
    name: str
    clone_url: str | None = None
    fork_url: str | None = None


class ProjectCreateResponse(BaseModel):
    name: str
    bare_dir: str
    worktree_dir: str
    data_dir: str
    mode: str


# ---------------------------------------------------------------------------
# Scopes (formerly Tasks)
# ---------------------------------------------------------------------------

class ScopeCreateRequest(BaseModel):
    project: str
    scope: str
    from_branch: str | None = None
    context: str | None = None


class ScopeUpdateRequest(BaseModel):
    action: str = Field(default="complete", pattern="^complete$")
    merge: bool = False
    cleanup: bool = False


class ScopeSyncRequest(BaseModel):
    strategy: str = Field(default="merge", pattern="^(merge|rebase)$")
    from_branch: str | None = None


class ScopeInfo(BaseModel):
    project: str
    scope: str
    status: str
    branch: str
    worktree: str
    repo_path: str | None = None
    session: int = 1



class ScopeListResponse(BaseModel):
    scopes: list[ScopeInfo]
    total: int


class ScopeActionResponse(BaseModel):
    project: str
    scope: str
    status: str
    message: str
    session: int | None = None



# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

class ArtifactRegisterRequest(BaseModel):
    project: str
    scope: str
    name: str
    artifact_type: str = Field(
        ...,
        pattern="^(figure|dataset|report|model|script|other)$",
    )
    path: str
    description: str | None = None
    format: str | None = None
    tags: str | None = None


class ArtifactInfo(BaseModel):
    id: int
    project: str
    scope: str
    name: str
    artifact_type: str
    path: str
    description: str | None
    format: str | None
    tags: str | None
    status: str
    created_at: str
    # v34: peer that owns the on-disk file. Empty string = pre-federation
    # local. Callers consult this before reading ``path`` directly so they
    # know whether to GET /artifacts/{id}/content (which federates) instead
    # of opening the local file.
    origin_peer: str = ""


class ArtifactSearchResponse(BaseModel):
    artifacts: list[ArtifactInfo]
    total: int


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

class SkillInfo(BaseModel):
    name: str
    type: str
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    file_path: str


class SkillListResponse(BaseModel):
    skills: list[SkillInfo]
    total: int


class SkillContentResponse(BaseModel):
    skill: SkillInfo
    content: str


# ---------------------------------------------------------------------------
# Session Logs
# ---------------------------------------------------------------------------

class SessionLogCreateRequest(BaseModel):
    project: str
    scope: str
    summary: str
    title: str | None = None
    decisions: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    agent_id: str = "unknown"
    skill_path: str | None = None
    outcome: str | None = None
    deviations: str | None = None
    suggestions: str | None = None


class SessionLogEntry(BaseModel):
    id: int
    project: str
    scope: str
    file_path: str = ""
    git_commit: str | None = None
    logged_at: str
    title: str | None = None
    agent_id: str
    skill_path: str | None = None
    outcome: str | None = None
    deviations: str | None = None
    suggestions: str | None = None
    skill_version: str | None = None
    resolved_at: str | None = None
    resolution: str | None = None


class SessionLogPreview(BaseModel):
    """Lightweight session row. `summary` is at most ~240 chars; check
    `summary_truncated` to know if the underlying row had more."""
    id: int
    project: str
    scope: str
    logged_at: str
    summary: str
    title: str | None = None
    agent_id: str
    skill_path: str | None = None
    outcome: str | None = None
    resolved_at: str | None = None
    summary_truncated: bool = False


class SessionLogListResponse(BaseModel):
    entries: list[SessionLogEntry]
    total: int


class SessionSearchResponse(BaseModel):
    entries: list[SessionLogPreview]
    total: int


class SessionLogContentResponse(BaseModel):
    entry: SessionLogEntry
    content: str


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

class MessageSendRequest(BaseModel):
    scope: str
    sender: str
    msg_type: str = Field(
        ...,
        pattern="^(scope_assignment|reflection|status_update|notification|plan)$",
    )
    subject: str
    body: str
    metadata: str | None = None


class MessageInfo(BaseModel):
    id: int
    scope: str
    sender: str
    msg_type: str
    subject: str
    body: str
    metadata: str | None
    status: str
    created_at: str
    read_at: str | None


class MessagePreview(BaseModel):
    """Lightweight message row for browsing — omits body and metadata."""
    id: int
    scope: str
    sender: str
    msg_type: str
    subject: str
    status: str
    created_at: str
    read_at: str | None


class MessageSearchResponse(BaseModel):
    messages: list[MessagePreview]
    total: int


class MessageFetchResponse(BaseModel):
    messages: list[MessageInfo]
    total: int
    marked_read_count: int = 0


class MessageActionResponse(BaseModel):
    message: str
    msg: MessageInfo | None = None


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
# Rooms
# ---------------------------------------------------------------------------

class RoomInfo(BaseModel):
    id: str
    host_peer_id: str
    created_at: str
    closed_at: str | None = None
    topic: str | None = None
    status: str  # active|closed|archived
    close_on_exit: bool = False


class ParticipantInfo(BaseModel):
    room_id: str
    kind: str  # scope|subscriber|shadow_peer
    identifier: str
    joined_at: str
    left_at: str | None = None


class PostInfo(BaseModel):
    id: str
    room_id: str
    author: str
    body: str
    kind: str
    ts: str


class RoomCreateRequest(BaseModel):
    topic: str | None = None
    scopes: list[str] = Field(default_factory=list)
    prompts: dict[str, str] = Field(default_factory=dict)
    close_on_exit: bool = False


class RoomListResponse(BaseModel):
    rooms: list[RoomInfo]
    total: int


class RoomDetail(BaseModel):
    room: RoomInfo
    participants: list[ParticipantInfo]
    recent: list[PostInfo]


class RoomHistoryResponse(BaseModel):
    posts: list[PostInfo]
    total: int


class RoomPostRequest(BaseModel):
    body: str
    kind: str = "text"
    to: str | None = None  # optional ``scope:<scope>`` direct-address


class RoomInviteRequest(BaseModel):
    scope: str
    prompt: str | None = None


class RoomRemoveRequest(BaseModel):
    scope: str


class RoomCloseRequest(BaseModel):
    kill_agents: bool = False


class RoomActionResponse(BaseModel):
    message: str
    room: RoomInfo | None = None
    post: PostInfo | None = None
    participant: ParticipantInfo | None = None


class RoomArchiveBlockedResponse(BaseModel):
    """Body for the 409 returned when ``POST /rooms/{id}/archive`` is
    refused due to remaining active scope participants."""
    error: str = "room_archive_blocked"
    room_id: str
    blocking_scopes: list[str]


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


# ---------------------------------------------------------------------------
# Projects (control-center surface — list)
# ---------------------------------------------------------------------------

class ProjectScopeCounts(BaseModel):
    active: int = 0
    completed: int = 0
    deleted: int = 0


class ProjectListInfo(BaseModel):
    name: str
    scope_counts: ProjectScopeCounts


class ProjectListResponse(BaseModel):
    projects: list[ProjectListInfo]
