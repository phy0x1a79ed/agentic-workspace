"""Pydantic request/response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class StatusResponse(BaseModel):
    status: str = "ok"
    workspace_root: str
    active_locks: int = 0
    active_scopes: int = 0
    active_shared_edits: int = 0


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
    results_dir: str
    reports_dir: str
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


class ArtifactSearchResponse(BaseModel):
    artifacts: list[ArtifactInfo]
    total: int


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

class LockAcquireRequest(BaseModel):
    resource_path: str
    holder_id: str
    holder_pid: int | None = None
    lock_type: str = Field(default="exclusive", pattern="^(exclusive|shared)$")
    metadata: str | None = None


class LockReleaseRequest(BaseModel):
    resource_path: str
    holder_id: str


class LockInfo(BaseModel):
    id: int
    resource_path: str
    holder_id: str
    holder_pid: int | None
    lock_type: str
    acquired_at: str
    heartbeat_at: str
    metadata: str | None


class LockListResponse(BaseModel):
    locks: list[LockInfo]
    total: int


class LockActionResponse(BaseModel):
    message: str
    lock: LockInfo | None = None


# ---------------------------------------------------------------------------
# Shared Resources
# ---------------------------------------------------------------------------

class SharedEditRequest(BaseModel):
    name: str
    created_by: str = "unknown"


class SharedEditInfo(BaseModel):
    id: int
    name: str
    worktree_path: str
    branch: str
    created_by: str
    created_at: str
    status: str


class SharedEditListResponse(BaseModel):
    edits: list[SharedEditInfo]
    total: int


class SharedEditActionResponse(BaseModel):
    message: str
    edit: SharedEditInfo | None = None


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
    summary: str
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
    id: int
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


class RoomAgentInfo(BaseModel):
    scope: str
    kind: str  # 'scope' (local) | 'shadow_peer' (remote)
    identifier: str
    joined_at: str
    live: LiveAgentState | None = None  # None for shadow_peer


class RoomAgentsResponse(BaseModel):
    agents: list[RoomAgentInfo]


# ---------------------------------------------------------------------------
# Peers (control-center surface)
# ---------------------------------------------------------------------------

class PeerInfo(BaseModel):
    peer_id: str
    ssh_alias: str | None = None
    remote_port: int | None = None
    friendly_name: str | None = None
    last_seen: str | None = None
    added_at: str | None = None


class PeerListResponse(BaseModel):
    peers: list[PeerInfo]


class PeerPingResponse(BaseModel):
    peer_id: str
    ok: bool
    rtt_ms: float | None = None
    error: str | None = None


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
