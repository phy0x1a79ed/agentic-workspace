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
    active_tasks: int = 0
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
# Tasks
# ---------------------------------------------------------------------------

class TaskCreateRequest(BaseModel):
    project: str
    task: str
    from_branch: str | None = None
    context: str | None = None


class TaskUpdateRequest(BaseModel):
    action: str = Field(default="complete", pattern="^complete$")
    merge: bool = False
    cleanup: bool = False


class TaskInfo(BaseModel):
    project: str
    task: str
    status: str
    branch: str
    worktree: str
    repo_path: str | None = None


class TaskListResponse(BaseModel):
    tasks: list[TaskInfo]
    total: int


class TaskActionResponse(BaseModel):
    project: str
    task: str
    status: str
    message: str


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
    task: str
    summary: str
    decisions: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    agent_id: str = "unknown"


class SessionLogEntry(BaseModel):
    id: int
    project: str
    task: str
    file_path: str
    git_commit: str | None
    logged_at: str
    summary: str
    agent_id: str


class SessionLogListResponse(BaseModel):
    entries: list[SessionLogEntry]
    total: int


class SessionLogContentResponse(BaseModel):
    entry: SessionLogEntry
    content: str
