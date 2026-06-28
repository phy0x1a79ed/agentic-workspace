"""Hub adapter for the workspace service.

Boots the workspace service as a gateway-registered process: stands up its own
DB (workspace.db), then runs the shared :class:`awm.gatewayclient.ServiceAdapter`
loop (register → ready → serve → reconnect). Exposes the unit lifecycle ops the
agents service provisions placements through.

Run via run.sh (which the hub spawns and respawns):
    python -m awm.workspace.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.workspace import dao
from awm.workspace import units

log = logging.getLogger("awm.workspace.hub_adapter")

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "workspace_create",
            "tool": "workspace_create",
            "description": (
                "Provision a workspace unit (the DAG node's canonical execution "
                "sandbox): a directory with CLAUDE.md (the brief), a .awm/ (with a "
                "data symlink to shared project data), read-only inputs/<name> "
                "pre-readings, deliverable/<contract>/ staging, and repos/<name> "
                "symlinks to existing scopes. Idempotent on (project, unit_slug)."
            ),
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "unit_slug", "type": "string", "required": True},
                {"name": "context_md", "type": "string", "required": False},
                {"name": "prereadings", "type": "array", "required": False,
                 "description": (
                     "List of {name, content} (inline text) or {name, path} "
                     "(source file/dir to copy). Materialized read-only under "
                     "inputs/<name>.")},
                {"name": "repos", "type": "array", "required": False,
                 "description": (
                     "List of {name, project, scope} existing scopes to link "
                     "under repos/<name> (symlinked to projects/<project>/<scope>).")},
            ],
        },
        {
            "name": "workspace_link_repos",
            "tool": "workspace_link_repos",
            "description": (
                "Link (or refresh) existing scopes under a unit's repos/<name> "
                "without rewriting the brief — the admin link_repo primitive."
            ),
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "unit_slug", "type": "string", "required": True},
                {"name": "repos", "type": "array", "required": True,
                 "description": "[{name, project, scope}] scopes to link."},
            ],
        },
        {
            "name": "workspace_retain",
            "tool": "workspace_retain",
            "description": (
                "Free a unit but keep its contents (mark idle) — the "
                "free-but-retain primitive for audit and planner/lifecycle reuse."
            ),
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "unit_slug", "type": "string", "required": True},
            ],
        },
        {
            "name": "workspace_destroy",
            "tool": "workspace_destroy",
            "description": "Remove a unit directory and its row (terminal cleanup).",
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "unit_slug", "type": "string", "required": True},
            ],
        },
        {
            "name": "workspace_resolve",
            "tool": "workspace_resolve",
            "description": (
                "Return a unit's on-disk path + state (active|idle|unknown) "
                "without hardcoding the layout — used to recover a workdir on "
                "respawn."
            ),
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "unit_slug", "type": "string", "required": True},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}

HANDLERS = {
    "workspace_create": lambda args: units.create(
        project=args["project"], unit_slug=args["unit_slug"],
        context_md=args.get("context_md", ""),
        prereadings=args.get("prereadings") or [],
        repos=args.get("repos") or [],
    ),
    "workspace_link_repos": lambda args: units.link_repos(
        project=args["project"], unit_slug=args["unit_slug"],
        repos=args.get("repos") or []),
    "workspace_retain": lambda args: units.retain(
        project=args["project"], unit_slug=args["unit_slug"]),
    "workspace_destroy": lambda args: units.destroy(
        project=args["project"], unit_slug=args["unit_slug"]),
    "workspace_resolve": lambda args: units.resolve(
        project=args["project"], unit_slug=args["unit_slug"]),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "workspace", API_MANIFEST, HANDLERS, on_start=dao.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
