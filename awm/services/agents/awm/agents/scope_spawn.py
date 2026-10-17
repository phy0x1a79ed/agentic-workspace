"""Scope agent: spawn an interactive agent *on an awm scope*.

A resolve-or-provision preamble around the generic **conversational spawn**
(``agent_instances.create_conversational_session``). Where a plain conversational
spawn lands an agent at a bare directory, the scope agent lands it in a real
**git-scope worktree** (``PROJECTS_DIR/<project>/<scope>``) with its
``.awm/context.md`` — what a human's ``claude`` in that worktree would get, but
as a *managed* session (event stream, transcript, human attach).

It is deliberately NOT the DAG ``create_session`` placement path: no orchestrator
task, no workspace unit, no supervision driver. The preamble:

1. Derive the worktree path from ``awm.config.PROJECTS_DIR`` (no scopes RPC
   returns it — a known gap; the path is deterministic).
2. If the worktree isn't on disk, provision it via the scopes service
   ``scope_create`` (which lays out the git worktree + scaffolds ``.awm/``).
   ``scope_create`` is NOT idempotent (raises if an active session exists), so
   we only call it when the directory is absent.
3. Hand off to ``create_conversational_session`` — the ONE managed spawn
   lifecycle, so the roster/attach/terminal all work unchanged.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from awm import config as _config

from . import agent_instances as ai


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
) -> dict[str, Any]:
    """Resolve-or-provision a scope worktree, then spawn a managed agent in it.

    Returns the managed session's identity + attach handle plus ``{project,
    scope}``. Raises ``ValueError`` on bad input, or propagates a scopes-service
    / spawn error so the overlay shows a clean message.
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

    permission_mode = ("bypassPermissions"
                       if (permission or "").strip() == "full" else "default")
    session = await ai.create_conversational_session(
        cwd=worktree,
        agent_cli=harness or "claude",
        model=model,
        effort=effort,
        permission_mode=permission_mode,
    )
    return {
        "session_id": session.id,
        "agent_id": session.agent_id,
        "tmux_session": session.tmux_session,
        "cwd": session.workdir,
        "harness": session.agent_cli,
        "model": session.model,
        "effort": session.effort,
        "project": project,
        "scope": scope,
    }


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
