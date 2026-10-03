"""Scope agent: spawn an interactive agent *on an awm scope*.

The second agent implementation (alongside the DAG agent) — a SEPARATE control
plane that shares only leaf utilities with it, no class hierarchy. Where
``fleet_spawn`` launches a plain agent at a bare directory, the scope agent
lands the agent in a real **git-scope worktree** (``PROJECTS_DIR/<project>/<scope>``)
with its ``.awm/context.md`` and the workspace-root ``.mcp.json`` (awm MCP) —
exactly what a human's ``claude`` in that worktree would get.

It is deliberately NOT the DAG ``create_session`` path: no orchestrator task, no
workspace unit, no supervision driver, no one-live-per-scope guard. It is
``fleet_spawn.spawn_terminal`` with a resolve-or-provision preamble:

1. Derive the worktree path from ``awm.config.PROJECTS_DIR`` (no scopes RPC
   returns it — a known gap; the path is deterministic).
2. If the worktree isn't on disk, provision it via the scopes service
   ``scope_create`` (which lays out the git worktree + scaffolds ``.awm/``).
   ``scope_create`` is NOT idempotent (raises if an active session exists), so
   we only call it when the directory is absent.
3. Hand off to ``spawn_terminal`` — the same detached-tmux launch the adhoc
   path uses, so the roster/attach/terminal leaves all work unchanged.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from awm import config as _config

from . import fleet_spawn


def worktree_path(project: str, scope: str) -> str:
    """The deterministic worktree path for a scope: PROJECTS_DIR/<project>/<scope>.

    No scopes RPC returns this (identity RPCs are DB-only, create returns an
    action summary) — it is derived, matching the scopes service's own layout.
    """
    return str(_config.PROJECTS_DIR / project / scope)


async def spawn_scoped(
    *,
    project: str,
    scope: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    harness: str = "claude",
    permission: str = "default",
    context: Optional[str] = None,
    hub_url: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve-or-provision a scope worktree, then launch an idle agent in it.

    Returns ``spawn_terminal``'s dict plus ``{project, scope}``. Raises
    ``ValueError`` on bad input, or propagates a scopes-service / tmux error so
    the overlay shows a clean message.
    """
    project = (project or "").strip()
    scope = (scope or "").strip()
    if not project or not scope:
        raise ValueError("spawn_scoped requires both project and scope")

    worktree = worktree_path(project, scope)
    if not os.path.isdir(worktree):
        # Provision the scope: git worktree + .awm scaffolding. Guarded on disk
        # absence because scope_create is not idempotent (raises when an active
        # session already owns the scope).
        from awm.gatewayclient import call as gw_call
        create_args: dict[str, Any] = {"project": project, "scope": scope}
        if context:
            create_args["context"] = context
        await gw_call("scopes", "scope_create", create_args, timeout=120.0)
        if not os.path.isdir(worktree):
            raise FileNotFoundError(
                f"scope worktree still absent after scope_create: {worktree!r}")

    result = fleet_spawn.spawn_terminal(
        cwd=worktree,
        harness=harness or "claude",
        model=model,
        effort=effort,
        permission=permission or "default",
        hub_url=hub_url,
    )
    result["project"] = project
    result["scope"] = scope
    return result


async def list_scopes(
    *,
    project: Optional[str] = None,
    query: Optional[str] = None,
    status: str = "active",
    limit: int = 100,
) -> dict[str, Any]:
    """List scopes for the spawn picker: ``{scopes: [{project, scope, worktree,
    status, branch}]}``.

    Passthrough to the scopes service ``scope_search``; each row's worktree is
    taken from the service (or derived) so the picker can submit ``{project,
    scope}`` and show where the agent would land.
    """
    from awm.gatewayclient import call as gw_call
    search_args: dict[str, Any] = {"status": status, "limit": limit}
    if project:
        search_args["project"] = project
    if query:
        search_args["query"] = query
    resp = await gw_call("scopes", "scope_search", search_args, timeout=30.0)
    rows = (resp or {}).get("scopes") or []
    out = []
    for r in rows:
        p = r.get("project")
        s = r.get("scope")
        if not p or not s:
            continue
        out.append({
            "project": p,
            "scope": s,
            "worktree": r.get("worktree") or worktree_path(p, s),
            "status": r.get("status"),
            "branch": r.get("branch"),
        })
    return {"scopes": out}
