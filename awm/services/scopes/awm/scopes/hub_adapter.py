"""Hub adapter for the scopes service.

Boots the scopes service as a gateway-registered process: stands up its own
DB (via ``dao.init``), then runs the shared :class:`awm.gatewayclient.ServiceAdapter`
loop (register → ready → serve → reconnect).

The four **identity RPCs** — ``resolveScope``, ``resolveRef``,
``ensureProject``, ``ensureScope`` — are exposed in ``functions[]`` of the
ready manifest. Surface ops (rooms / messaging / sessions / projects) will be
added to this manifest by the surfaces agent; leave ``API_MANIFEST`` extensible
(import it and ``.copy()`` + extend in the surfaces adapter).

Run via ``start.sh`` (which the hub spawns and respawns):
    python -m awm.scopes.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.scopes import dao
from awm.scopes.identity import (
    agent_id_for_scope,
    agent_record_for_scope,
    ensure_agent,
    ensure_project,
    project_scope_for_agent,
    resolve_ref,
    user_id_for_username,
    username_for_user_id,
    SYSTEM_REF,
)
from awm.scopes.operations.scopes import SCOPE_MANIFEST_FUNCTIONS, SCOPE_HANDLERS
from awm.scopes.operations.sessions import SESSION_MANIFEST_FUNCTIONS, SESSION_HANDLERS
from awm.scopes.operations.rooms import ROOM_MANIFEST_FUNCTIONS, ROOM_HANDLERS
from awm.scopes.operations.messaging import MESSAGING_MANIFEST_FUNCTIONS, MESSAGING_HANDLERS
from awm.scopes.operations.projects import PROJECT_MANIFEST_FUNCTIONS, PROJECT_HANDLERS

log = logging.getLogger("awm.scopes.hub_adapter")


# ---------------------------------------------------------------------------
# Identity RPC handlers
# ---------------------------------------------------------------------------
# Each handler receives ``args`` (the JSON body POSTed to the function
# endpoint) and returns a JSON-serialisable dict. Natural keys only — no uuids
# cross the RPC boundary. See IDENTITY_CONTRACT.md for the frozen shapes.
# ---------------------------------------------------------------------------


def _handle_resolve_scope(args: dict[str, Any]) -> dict[str, Any]:
    """resolveScope(project, scope) → {exists, project, scope, status} | not-found.

    Validate-only hot read: does this scope exist and is it live?
    Returns the agent's lifecycle status when found; the caller decides
    whether a ``retired`` scope counts. Never returns an agent_id.
    """
    project: str = args["project"]
    scope: str = args["scope"]
    # active_only=False so retired scopes resolve (status is returned, caller decides)
    rec = agent_record_for_scope(project, scope, active_only=False)
    if rec is None:
        return {"exists": False, "project": project, "scope": scope}
    return {
        "exists": True,
        "project": project,
        "scope": scope,
        "status": rec["status"],
    }


def _handle_resolve_ref(args: dict[str, Any]) -> dict[str, Any] | None:
    """resolveRef(literal) → {kind, project?, scope?, username?} | null.

    Pure lookup — no create_users side effect. Returns None (rendered as {} by
    the gateway) when the ref does not resolve.
    """
    literal: str = args["literal"]

    # Classify the literal form the same way identity.resolve_ref does, but
    # return natural keys instead of internal uuids.
    if not literal or literal == SYSTEM_REF:
        return {"kind": "system"}

    if literal.startswith("agent:") or literal.startswith("scope:"):
        prefix = "agent:" if literal.startswith("agent:") else "scope:"
        rest = literal[len(prefix):]
        if "/" not in rest:
            return None
        proj, sc = rest.split("/", 1)
        rec = agent_record_for_scope(proj, sc, active_only=False)
        if rec is None:
            return None
        return {"kind": "agent", "project": proj, "scope": sc}

    if literal.startswith("user:"):
        name = literal[len("user:"):]
        uid = user_id_for_username(name)
        if uid is None:
            return None
        return {"kind": "user", "username": name}

    if "/" in literal:
        proj, sc = literal.split("/", 1)
        rec = agent_record_for_scope(proj, sc, active_only=False)
        if rec is None:
            return None
        return {"kind": "agent", "project": proj, "scope": sc}

    # Bare opaque literal — treat as username.
    uid = user_id_for_username(literal)
    if uid is None:
        return None
    username = username_for_user_id(uid)
    return {"kind": "user", "username": username or literal}


def _handle_ensure_project(args: dict[str, Any]) -> dict[str, Any]:
    """ensureProject(project, repo_path?, url?) → {project}.

    Idempotent get-or-create of a project. Returns the natural key.
    """
    name: str = args["project"]
    repo_path: str = args.get("repo_path") or ""
    url: str | None = args.get("url")
    ensure_project(name, repo_path=repo_path, url=url)
    return {"project": name}


def _handle_ensure_scope(args: dict[str, Any]) -> dict[str, Any]:
    """ensureScope(project, scope, ...) → {project, scope}.

    Idempotent get-or-create of an agent/scope under an existing project.
    Raises KeyError (→ 500 → GatewayCallError at caller) if the project does
    not exist — callers must call ensureProject first.
    """
    project: str = args["project"]
    scope: str = args["scope"]
    # Optional fields mirror ensure_agent kwargs.
    ensure_agent(
        project,
        scope,
        branch=args.get("branch"),
        worktree=args.get("worktree") or "",
        agent_cli=args.get("agent_cli") or "claude",
        status=args.get("status") or "allocated",
        is_vagrant=bool(args.get("is_vagrant", False)),
        display_name=args.get("display_name"),
    )
    return {"project": project, "scope": scope}


# ---------------------------------------------------------------------------
# Manifest + handler table
# ---------------------------------------------------------------------------

_IDENTITY_FUNCTIONS = [
    {
        "name": "resolveScope",
        "description": (
            "Validate-only read: does this (project, scope) exist and "
            "what is its status? Returns {exists, project, scope, status} "
            "when found, {exists: false, project, scope} when not found."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
        ],
    },
    {
        "name": "resolveRef",
        "description": (
            "Resolve a literal author/recipient string to its natural-key "
            "identity. Returns {kind, project?, scope?, username?} or null."
        ),
        "params": [
            {"name": "literal", "type": "string", "required": True},
        ],
    },
    {
        "name": "ensureProject",
        "description": "Idempotent get-or-create of a project. Returns {project}.",
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "repo_path", "type": "string", "required": False},
            {"name": "url", "type": "string", "required": False},
        ],
    },
    {
        "name": "ensureScope",
        "description": (
            "Idempotent get-or-create of an agent/scope under an existing "
            "project. Returns {project, scope}."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "branch", "type": "string", "required": False},
            {"name": "worktree", "type": "string", "required": False},
            {"name": "agent_cli", "type": "string", "required": False},
            {"name": "status", "type": "string", "required": False},
            {"name": "is_vagrant", "type": "boolean", "required": False},
            {"name": "display_name", "type": "string", "required": False},
        ],
    },
]

API_MANIFEST: dict[str, Any] = {
    "functions": (
        _IDENTITY_FUNCTIONS
        + SCOPE_MANIFEST_FUNCTIONS
        + SESSION_MANIFEST_FUNCTIONS
        + ROOM_MANIFEST_FUNCTIONS
        + MESSAGING_MANIFEST_FUNCTIONS
        + PROJECT_MANIFEST_FUNCTIONS
    ),
    "emitters": [],
    "sessions": [],
}

_IDENTITY_HANDLERS: dict[str, Any] = {
    "resolveScope": _handle_resolve_scope,
    "resolveRef": _handle_resolve_ref,
    "ensureProject": _handle_ensure_project,
    "ensureScope": _handle_ensure_scope,
}

HANDLERS: dict[str, Any] = {
    **_IDENTITY_HANDLERS,
    **SCOPE_HANDLERS,
    **SESSION_HANDLERS,
    **ROOM_HANDLERS,
    **MESSAGING_HANDLERS,
    **PROJECT_HANDLERS,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "scopes", API_MANIFEST, HANDLERS, on_start=dao.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
