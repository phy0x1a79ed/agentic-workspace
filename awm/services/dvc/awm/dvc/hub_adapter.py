"""Hub adapter for the dvc service — DVC cache sync against the chinook collection.

Boots dvc as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve → reconnect).
The gateway injects only ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — there is no token.

On the collapsed MCP surface this is a single ``dvc`` domain tool
(``dvc(verb="status")``, ``verb="pull"``, …); CLI and HTTP stay expanded as
``dvc_<verb>`` (``awm dvc pull``, ``POST /invoke {name:"dvc_pull"}``).

Three jobs, one config:
  - ``sync`` — the daily append-only push of the whole shared cache.
  - ``status`` / ``resolve`` / ``pull`` / ``push`` — the hash-selective inverse,
    moving only the objects one scope actually pins.
  - ``coverage`` — what the two remotes between them do *not* hold.

The byte-moving verbs address the same tree on chinook, so they read one
``prefix`` from ``$AWM_DIR/dvc.toml``. They must agree: ``pull`` reads back
exactly what ``sync`` wrote, and a mismatch means a restore silently finds
nothing.

Every verb that moves bytes **submits and returns a task id** rather than
blocking — a Globus task runs for minutes to hours. Poll with ``task``.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.dvc.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.dvc import coverage as coveragemod
from awm.dvc import sync as syncmod
from awm.dvc import transfer
from awm.dvc.config import load as load_config
from awm.dvc.globus import task_status, wait

log = logging.getLogger("awm.dvc.hub_adapter")

_SCOPE_PARAM = {
    "name": "scope",
    "type": "string",
    "required": True,
    "description": (
        "Scope worktree — absolute, or relative to the workspace root "
        "(e.g. 'projects/fabfos/dev')."
    ),
}

_DRY_RUN_PARAM = {
    "name": "dry_run",
    "type": "boolean",
    "required": False,
    "description": "Return the transfer items that would be submitted, and submit nothing.",
}

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "dvc_status",
            "description": (
                "Report a scope's DVC pin state against the local cache: pin count, "
                ".dir manifests, and how many objects are present vs missing. Local "
                "only — touches no network. unresolved_manifests>0 means some .dir "
                "manifests are absent, so their leaves are not yet counted."
            ),
            "params": [_SCOPE_PARAM],
            "timeout": 300.0,
        },
        {
            "name": "resolve",
            "tool": "dvc_resolve",
            "description": (
                "List the exact object hashes a scope depends on, split into "
                "present / missing / unresolved_manifests. Local only. Use to see "
                "precisely what a pull would move."
            ),
            "params": [_SCOPE_PARAM],
            "timeout": 300.0,
        },
        {
            "name": "pull",
            "tool": "dvc_pull",
            "description": (
                "Fetch from chinook only the cache objects this scope pins and does "
                "not have. Submits a Globus task and returns its task_id; poll with "
                "task. A cold restore is two-phase — .dir manifests must land before "
                "their leaves can be named — so a call whose phase is 'manifests' "
                "should be repeated once that task completes to fetch the leaves. "
                "Safe to repeat: each call re-resolves and does whatever remains. "
                "After the final task, run `dvc checkout` in the scope to materialize."
            ),
            "params": [_SCOPE_PARAM, _DRY_RUN_PARAM],
            "timeout": 600.0,
        },
        {
            "name": "push",
            "tool": "dvc_push",
            "description": (
                "Send the cache objects this scope pins up to chinook. Submits a "
                "Globus task and returns its task_id; poll with task. Never deletes "
                "on the remote. Fails if any .dir manifest is missing locally — you "
                "cannot push what you do not have."
            ),
            "params": [_SCOPE_PARAM, _DRY_RUN_PARAM],
            "timeout": 600.0,
        },
        {
            "name": "sync",
            "tool": "dvc_sync",
            "description": (
                "Submit the daily sync of the shared DVC cache (data/.dvc_cache) to "
                "chinook and return its task_id. APPEND-ONLY: never deletes on the "
                "remote, so chinook accumulates objects and a local `dvc gc` stays "
                "recoverable. Covers every project's data at once, because every "
                "project shares that one cache. Declines if a previous sync is still "
                "running unless force=true. Nothing else in the workspace is backed "
                "up by this — see coverage."
            ),
            "params": [
                {
                    "name": "dry_run",
                    "type": "boolean",
                    "required": False,
                    "description": "Build and return the transfer document; submit nothing.",
                },
                {
                    "name": "force",
                    "type": "boolean",
                    "required": False,
                    "description": "Submit even if a previous sync task is still running.",
                },
            ],
            "timeout": 300.0,
        },
        {
            "name": "coverage",
            "tool": "dvc_coverage",
            "description": (
                "Report what would be lost if this machine died: per scope worktree, "
                "uncommitted and untracked files, branches with no upstream or ahead "
                "of one, and pins whose objects are absent from the shared cache. "
                "Read-only, no network. Code is covered by GitHub and data by the "
                "cache sync; everything else is disposable by decision, and this is "
                "how that decision is checked."
            ),
            "params": [
                {
                    "name": "all",
                    "type": "boolean",
                    "required": False,
                    "description": "Include scopes with nothing at risk (default: only at-risk ones).",
                },
            ],
            # A `git status` per worktree across the whole workspace.
            "timeout": 900.0,
        },
        {
            "name": "task",
            "tool": "dvc_task",
            "description": (
                "Status of a Globus task submitted by this service: state, files "
                "transferred, bytes, throughput, faults. Pass wait=<seconds> to block "
                "until it finishes or that budget runs out (timed_out=true means "
                "still running, not failed). nice_status names the current fault, if "
                "any; an ACTIVE task with faults is still retrying."
            ),
            "params": [
                {"name": "task_id", "type": "string", "required": True},
                {
                    "name": "wait",
                    "type": "integer",
                    "required": False,
                    "description": "Seconds to block waiting for a terminal state (default: return immediately).",
                },
            ],
            "timeout": 3600.0,
        },
    ],
    "emitters": [],
    "sessions": [],
}


def _task(args: dict) -> dict:
    cfg = load_config()
    seconds = args.get("wait")
    if seconds:
        return wait(cfg, args["task_id"], timeout=int(seconds))
    return task_status(cfg, args["task_id"])


HANDLERS = {
    "status":  lambda a: transfer.status(a["scope"]),
    "resolve": lambda a: transfer.resolve(a["scope"]),
    "pull":    lambda a: transfer.pull(a["scope"], dry_run=bool(a.get("dry_run"))),
    "push":    lambda a: transfer.push(a["scope"], dry_run=bool(a.get("dry_run"))),
    "sync":    lambda a: syncmod.sync(
                   dry_run=bool(a.get("dry_run")), force=bool(a.get("force"))
               ),
    "coverage": lambda a: coveragemod.report(at_risk_only=not bool(a.get("all"))),
    "task":    _task,
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("dvc", API_MANIFEST, HANDLERS).run()


if __name__ == "__main__":
    asyncio.run(main())
