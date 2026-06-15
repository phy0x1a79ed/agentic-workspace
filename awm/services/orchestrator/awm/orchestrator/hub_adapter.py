"""Hub adapter for the orchestrator service — awm's coordination kernel.

Boots the orchestrator as a gateway-registered process: stands up its own DB and
starts the dispatch drain loop (``on_start``), then runs the shared
:class:`awm.gatewayclient.ServiceAdapter` loop (register → ready → serve →
reconnect).

**The honesty mechanism.** ``API_MANIFEST["functions"]`` lists ONLY the four
public ops, so the gateway catalog (``catalog.list_tools``, which iterates the
manifest) projects exactly four ``orch_*`` MCP tools. The privileged
plan-mutation ops (``claim`` / ``deliver`` / ``fail`` / ``decompose_commit`` /
``approve_plan`` / ``reject_plan`` / ``set_attached``) live in ``HANDLERS`` but
are deliberately ABSENT from the manifest — so they are not MCP tools, yet remain
reachable by the agents harness through the gateway's catch-all
``POST /svc/orchestrator/fn/<op>`` dispatch (``proxy_service_http`` resolves
``ch.call`` against each control channel's handler set, not the manifest). No
gateway change is required.

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

# Only the PUBLIC ops appear here — this is what becomes MCP-visible.
API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "orch_task_create",
            "tool": "orch_task_create",
            "description": "Create a fresh, still-vague task and place it into an "
                           "attended initial specification (a planner specifies "
                           "it conversationally) before it becomes work.",
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "goal", "type": "string", "required": True},
                {"name": "consumer", "type": "string", "required": False},
            ],
        },
        {
            "name": "orch_task_attach",
            "tool": "orch_task_attach",
            "description": "Attach a task to the global DAG as a prerequisite "
                           "(upstream) of a consumer (root by default), producing "
                           "and/or depending on named contracts.",
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "goal", "type": "string", "required": True},
                {"name": "consumer", "type": "string", "required": False},
                {"name": "produces", "type": "array", "required": False},
                {"name": "depends_on", "type": "array", "required": False},
            ],
        },
        {
            "name": "orch_status",
            "tool": "orch_status",
            "description": "Per-state task counts, task rows, completion, and "
                           "escalations for the plan (optionally per-project).",
            "params": [
                {"name": "project", "type": "string", "required": False},
            ],
        },
        {
            "name": "orch_frontier",
            "tool": "orch_frontier",
            "description": "The ready nodes — the current worker frontier "
                           "(optionally per-project).",
            "params": [
                {"name": "project", "type": "string", "required": False},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}

# All handlers — the four public above plus the privileged ops that are
# intentionally NOT in the manifest (claim / deliver / fail / decompose_commit /
# approve_plan / reject_plan / set_attached). Their omission is what keeps them
# off the MCP tool surface; the catch-all /svc/<name>/fn/<fn> dispatch still
# resolves them here.
HANDLERS = {
    # public (manifest-visible)
    "orch_task_create": operations.orch_task_create,
    "orch_task_attach": operations.orch_task_attach,
    "orch_status": operations.orch_status,
    "orch_frontier": operations.orch_frontier,
    # privileged (manifest-OMITTED — agents harness only)
    "claim": operations.claim,
    "deliver": operations.deliver,
    "fail": operations.fail,
    "decompose_commit": operations.decompose_commit,
    "approve_plan": operations.approve_plan,
    "reject_plan": operations.reject_plan,
    "set_attached": operations.set_attached,
}


async def _on_start() -> None:
    """Stand up the DB, start the dispatch drain loop, then re-dispatch the
    resting frontier left by any prior run."""
    dao.init()
    dispatch.start_drain_loop()
    # Boot reconcile: re-dispatch ready + planner-less decomposing nodes; leave
    # placements out (active / decomposing-with-agent) alone.
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
