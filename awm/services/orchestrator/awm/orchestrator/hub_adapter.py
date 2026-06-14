"""Hub adapter for the orchestrator service — awm's coordination kernel.

Boots the orchestrator as a gateway-registered process: stands up its own DB and
starts the dispatch drain loop (``on_start``), then runs the shared
:class:`awm.gatewayclient.ServiceAdapter` loop (register → ready → serve →
reconnect).

**The honesty mechanism.** ``API_MANIFEST["functions"]`` lists ONLY the four
public ops, so the gateway catalog (``catalog.list_tools``, which iterates the
manifest) projects exactly four ``orch_*`` MCP tools. The four privileged
plan-mutation ops live in ``HANDLERS`` but are deliberately ABSENT from the
manifest — so they are not MCP tools, yet remain reachable by the agents harness
through the gateway's catch-all ``POST /svc/orchestrator/fn/<op>`` dispatch
(``proxy_service_http`` resolves ``ch.call`` against each control channel's
handler set, not the manifest). No gateway change is required.

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.orchestrator.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.orchestrator import dao, dispatch, kernel, operations
from awm.orchestrator.dao import OrchestratorDAO

log = logging.getLogger("awm.orchestrator.hub_adapter")

# Only the four PUBLIC ops appear here — this is what becomes MCP-visible.
API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "orch_plan_create",
            "tool": "orch_plan_create",
            "description": "Attach a root goal to an existing project as a plan "
                           "and start working it. Never creates a project.",
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "goal", "type": "string", "required": True},
                {"name": "contract", "type": "string", "required": False},
            ],
        },
        {
            "name": "orch_task_attach",
            "tool": "orch_task_attach",
            "description": "Attach a task to a plan — optionally under a parent, "
                           "producing and/or depending on named contracts.",
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "goal", "type": "string", "required": True},
                {"name": "parent_id", "type": "string", "required": False},
                {"name": "produces", "type": "array", "required": False},
                {"name": "depends_on", "type": "array", "required": False},
            ],
        },
        {
            "name": "orch_status",
            "tool": "orch_status",
            "description": "Per-state task counts, all task rows, completion, and "
                           "root escalations for a project's plan.",
            "params": [
                {"name": "project", "type": "string", "required": True},
            ],
        },
        {
            "name": "orch_frontier",
            "tool": "orch_frontier",
            "description": "The ready leaves of a project's plan — the current "
                           "worker frontier.",
            "params": [
                {"name": "project", "type": "string", "required": True},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}

# All EIGHT handlers — the four public above plus the four privileged ops that
# are intentionally NOT in the manifest (claim/deliver/fail/decompose_commit).
# Their omission is what keeps them off the MCP tool surface; the catch-all
# /svc/<name>/fn/<fn> dispatch still resolves them here.
HANDLERS = {
    # public (manifest-visible)
    "orch_plan_create": operations.orch_plan_create,
    "orch_task_attach": operations.orch_task_attach,
    "orch_status": operations.orch_status,
    "orch_frontier": operations.orch_frontier,
    # privileged (manifest-OMITTED — agents harness only)
    "claim": operations.claim,
    "deliver": operations.deliver,
    "fail": operations.fail,
    "decompose_commit": operations.decompose_commit,
}


async def _on_start() -> None:
    """Stand up the DB, start the dispatch drain loop, then re-dispatch the
    resting frontier left by any prior run."""
    dao.init()
    dispatch.start_drain_loop()
    # Boot reconcile: re-dispatch ready/failed nodes; leave active/analyzing.
    dispatch.enqueue(kernel.reconcile(OrchestratorDAO()))


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "orchestrator", API_MANIFEST, HANDLERS, on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
