"""Pydantic request/response models owned by the scopes service.

Covers projects, scopes, and the scope channel (posts/subscribers) —
everything the scopes service exposes over its API surface. Live
agent-runtime state (pid/status/model/context) lives with the agents
service, even when surfaced through a scope-channel view.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
    branch_name: str | None = None
    context: str | None = None


class ScopeUpdateRequest(BaseModel):
    action: str = Field(default="complete", pattern="^complete$")
    merge: bool = False
    cleanup: bool = False
    # Teardown refuses when the scope's data clone holds content that exists
    # nowhere else; `force` accepts that loss deliberately.
    force: bool = False


class ScopeSyncRequest(BaseModel):
    strategy: str = Field(default="merge", pattern="^(merge|rebase)$")
    from_branch: str | None = None


class ScatterGatherResponse(BaseModel):
    """Result of a batch fan-in (gather) or fan-out (scatter) merge.

    ``results`` is one dict per peripheral, each
    ``{scope, branch, result, detail}`` where ``result`` ∈
    ``merged | up_to_date | conflict | skipped | error``. ``summary`` counts
    the results by outcome.
    """
    project: str
    hub: str
    hub_branch: str
    direction: str          # 'gather' (peripherals→hub) | 'scatter' (hub→peripherals)
    results: list[dict]
    # Populated only when data=True. One dict per peripheral, same shape as
    # ``results`` but with ``result`` also able to be ``blocked`` (incoming
    # revision references content nothing holds).
    data_results: list[dict] | None = None
    data_summary: dict | None = None
    summary: dict


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
# Scope channel (a scope IS the channel) — posts + subscribers.
#   Messages and journal (debrief) entries are posts differentiated by `kind`.
#   The wire shape is produced by channel.ScopePost.to_dict() /
#   channel.Subscriber.to_dict(); these models mirror it for HTTP/typing.
# ---------------------------------------------------------------------------

class ScopePostInfo(BaseModel):
    id: str
    project: str
    scope: str
    author: str            # 'agent:proj/scope', 'user:name', or 'system'
    kind: str              # 'message' | 'journal' | 'system' | …
    body: str
    meta: dict = Field(default_factory=dict)
    ts: str


class ScopeSubscriberInfo(BaseModel):
    project: str
    scope: str
    guest_kind: str        # 'agent' | 'user'
    guest_ref: str         # 'project/scope' or 'user:<name>'
    display_name: str
    joined_at: str


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
