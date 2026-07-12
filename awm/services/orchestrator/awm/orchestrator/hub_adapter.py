"""Hub adapter for the orchestrator service — awm's coordination kernel.

Boots the orchestrator as a gateway-registered process: stands up its own DB and
starts the dispatch drain loop (``on_start``), then runs the shared
:class:`awm.gatewayclient.ServiceAdapter` loop (register → ready → serve →
reconnect).

**The honesty mechanism.** ``API_MANIFEST["functions"]`` lists ONLY the five
public ops, so the gateway catalog (``catalog.list_tools``, which iterates the
manifest) projects exactly five ``orch_*`` MCP tools. The privileged
plan-mutation ops (``claim`` / ``deliver`` / ``fail`` / ``decompose_commit`` /
``approve_plan`` / ``reject_plan`` / ``accept_work`` / ``reject_work`` /
``set_attached`` / ``set_steering``) and the
planner read ops
(``search_tasks`` / ``search_contracts``) live in ``HANDLERS`` but are
deliberately ABSENT from the manifest — so they are not MCP tools, yet remain
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
import os
from typing import Any

from awm import gatewayclient
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
                {"name": "goal", "type": "string", "required": True},
                {"name": "consumer", "type": "string", "required": False},
            ],
        },
        {
            "name": "orch_node_open",
            "tool": "orch_node_open",
            "description": "Seamless drop-in: create a node (prerequisite of a "
                           "consumer, root by default) and place an attended "
                           "worker on it directly — an interactive claude agent a "
                           "human attaches to. Optional repo links existing "
                           "scope(s) into the unit. Returns task_id + "
                           "workspace_slug (the attach handle) + agent_ref.",
            "params": [
                {"name": "goal", "type": "string", "required": True},
                {"name": "consumer", "type": "string", "required": False},
                {"name": "repo", "type": "object", "required": False,
                 "description": (
                     "Existing scope(s) to attach to the task and link into the "
                     "unit: a \"project/scope\" string, a {project, scope, name?} "
                     "object, or a list of either. A scope attaches to at most "
                     "one active task.")},
                {"name": "accept", "type": "object", "required": False,
                 "description": (
                     "Opt-in acceptance gate {objective, checks:[{name, cmd, "
                     "expect_exit?, expect_output?}]}: arms an independent "
                     "execution-verified check on the node's deliverable — the "
                     "worker's delivery becomes a CLAIM an accept verifier must "
                     "pass before it completes. Omit for the legacy path.")},
            ],
        },
        {
            "name": "orch_task_attach",
            "tool": "orch_task_attach",
            "description": "Attach a task to the global DAG as a prerequisite "
                           "(upstream) of a consumer (root by default), producing "
                           "and/or depending on named contracts. Optionally "
                           "attach scope(s) via repo.",
            "params": [
                {"name": "goal", "type": "string", "required": True},
                {"name": "consumer", "type": "string", "required": False},
                {"name": "produces", "type": "array", "required": False},
                {"name": "depends_on", "type": "array", "required": False},
                {"name": "repo", "type": "object", "required": False},
                {"name": "accept", "type": "object", "required": False,
                 "description": (
                     "Opt-in acceptance gate {objective, checks:[{name, cmd, "
                     "expect_exit?, expect_output?}]} armed on every contract "
                     "this task produces — the worker's delivery becomes a CLAIM "
                     "an independent accept verifier must pass before it "
                     "completes. Omit for the legacy immediate-completion path.")},
            ],
        },
        {
            "name": "orch_attach_scope",
            "tool": "orch_attach_scope",
            "description": "Attach git scope(s) to a task (a \"project/scope\" "
                           "string, a {project, scope, name?} object, or a list). "
                           "A scope attaches to at most one active task; a busy "
                           "scope is rejected.",
            "params": [
                {"name": "task_id", "type": "string", "required": True},
                {"name": "repo", "type": "object", "required": True},
            ],
        },
        {
            "name": "orch_detach_scope",
            "tool": "orch_detach_scope",
            "description": "Detach a git scope from a task by scope_ref "
                           "(\"project/scope\") or a repo handle, freeing it to "
                           "attach elsewhere.",
            "params": [
                {"name": "task_id", "type": "string", "required": True},
                {"name": "scope_ref", "type": "string", "required": False},
                {"name": "repo", "type": "object", "required": False},
            ],
        },
        {
            "name": "orch_status",
            "tool": "orch_status",
            "description": "Per-state task counts, task rows, completion, and "
                           "escalations for the global plan.",
            "params": [],
        },
        {
            "name": "orch_frontier",
            "tool": "orch_frontier",
            "description": "The ready nodes — the current global worker frontier.",
            "params": [],
        },
        {
            "name": "orch_dag",
            "tool": "orch_dag",
            "description": "The whole plan in one shot — tasks, contracts, and "
                           "denormalized dependency edges for a client to "
                           "visualize the global DAG.",
            "params": [],
        },
        {
            "name": "orch_set_title",
            "tool": "orch_set_title",
            "description": "Set a task's human title (the headline shown above its "
                           "goal). The user's direct edit.",
            "params": [
                {"name": "task_id", "type": "string", "required": True},
                {"name": "title", "type": "string", "required": True},
            ],
        },
        {
            "name": "orch_set_tags",
            "tool": "orch_set_tags",
            "description": "Set a task's free-text tags (a list of strings, "
                           "searchable in the UI).",
            "params": [
                {"name": "task_id", "type": "string", "required": True},
                {"name": "tags", "type": "array", "required": True},
            ],
        },
        {
            "name": "orch_set_paused",
            "tool": "orch_set_paused",
            "description": "Set a task's sticky paused flag (durable; freezes the "
                           "autonomous supervisor; survives chat disconnect).",
            "params": [
                {"name": "task_id", "type": "string", "required": True},
                {"name": "paused", "type": "boolean", "required": True},
            ],
        },
        {
            "name": "orch_task_detail",
            "tool": "orch_task_detail",
            "description": "On-selection read for one task: the objective record "
                           "text, the staged plan reference (if any), the attempt "
                           "memories, and the steering/consent bits. Heavier than "
                           "the DAG poll — fire it when a node is selected.",
            "params": [
                {"name": "task_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "orch_cancel",
            "tool": "orch_cancel",
            "description": "Cancel a task: route it through the give-up path so "
                           "downstream consumers re-plan (one give-up path), and "
                           "best-effort stop any live placement. Rejects the root "
                           "and terminal tasks. Cancelling an attached task is the "
                           "human's escape hatch and clears its attach/steering "
                           "state.",
            "params": [
                {"name": "task_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "orch_retry",
            "tool": "orch_retry",
            "description": "Retry a failed or abandoned task: reset it to ready "
                           "with a fresh replan budget, clear the sticky pause and "
                           "stale steering bits (keeping the objective record and "
                           "the retained workspace unit), and re-dispatch. Rejects "
                           "any non-failed/abandoned task. NOTE: retrying a node "
                           "whose consumers already re-planned can mint a competing "
                           "producer (OR/coalescing is deferred).",
            "params": [
                {"name": "task_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "orch_decompose",
            "tool": "orch_decompose",
            "description": "Decompose a resting non-terminal node: push it through "
                           "the needs-decomposition entry (a planner expands it "
                           "into a sub-DAG), charging the decompose budget like the "
                           "organic path. Rejects the root, out-states (a placement "
                           "is live), and terminals.",
            "params": [
                {"name": "task_id", "type": "string", "required": True},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}

# All handlers — the five public above plus the privileged ops that are
# intentionally NOT in the manifest (claim / deliver / fail / decompose_commit /
# approve_plan / reject_plan / set_attached) and the planner read ops
# (search_tasks / search_contracts). Their omission is what keeps them off the
# MCP tool surface; the catch-all /svc/<name>/fn/<fn> dispatch still resolves
# them here.
HANDLERS = {
    # public (manifest-visible)
    "orch_task_create": operations.orch_task_create,
    "orch_node_open": operations.orch_node_open,
    "orch_task_attach": operations.orch_task_attach,
    "orch_attach_scope": operations.orch_attach_scope,
    "orch_detach_scope": operations.orch_detach_scope,
    "orch_status": operations.orch_status,
    "orch_frontier": operations.orch_frontier,
    "orch_dag": operations.orch_dag,
    "orch_set_title": operations.orch_set_title,
    "orch_set_tags": operations.orch_set_tags,
    "orch_set_paused": operations.orch_set_paused,
    "orch_task_detail": operations.orch_task_detail,
    "orch_cancel": operations.orch_cancel,
    "orch_retry": operations.orch_retry,
    "orch_decompose": operations.orch_decompose,
    # privileged (manifest-OMITTED — agents harness only)
    "claim": operations.claim,
    "deliver": operations.deliver,
    "fail": operations.fail,
    "decompose_commit": operations.decompose_commit,
    "approve_plan": operations.approve_plan,
    "reject_plan": operations.reject_plan,
    "accept_work": operations.accept_work,
    "reject_work": operations.reject_work,
    "set_attached": operations.set_attached,
    "set_steering": operations.set_steering,
    "set_title": operations.set_title,
    "set_tags": operations.set_tags,
    # attached-only admin ops (DAG restructuring) — manifest-omitted; reached
    # only through the agents service's attach-gated admin relay.
    "relocate_task": operations.relocate_task,
    "node_add_dependency": operations.node_add_dependency,
    "link_repo": operations.link_repo,
    # planner reads — manifest-omitted (reached via the catch-all), read-only.
    "search_tasks": operations.search_tasks,
    "search_contracts": operations.search_contracts,
}


def _reclaim_workspace(unit_slug: str) -> None:
    """Free-but-retain a completed task's workspace unit (the reclaim seam).

    A synchronous gateway call to the workspace service's ``workspace_retain``;
    ``operations.deliver`` already wraps it best-effort, so a transient failure
    can't wedge completion."""
    gatewayclient.call_sync("workspace", "workspace_retain",
                            {"unit_slug": unit_slug})


def _link_repos(unit_slug: str, repos: list) -> None:
    """Live-link a task's attached scopes into its unit (the attach seam).

    A synchronous gateway call to the workspace service's
    ``workspace_link_repos``; the attach/detach ops wrap it best-effort."""
    gatewayclient.call_sync("workspace", "workspace_link_repos",
                            {"unit_slug": unit_slug, "repos": repos})


def _stop_placement(workspace_slug: str, task_id: str | None) -> None:
    """Best-effort stop of a live placement (the cancel stop seam).

    A synchronous gateway call to the agents service's manifest-omitted
    ``stop_placement`` (reached via the catch-all ``/svc/agents/fn/<op>``
    dispatch). The agents side closes the placement row BEFORE killing the
    session, so a dying agent's late fail can't re-route consumers again;
    ``operations.orch_cancel`` wraps this best-effort so a stop failure never
    fails the cancel."""
    gatewayclient.call_sync("agents", "stop_placement",
                            {"scope": workspace_slug, "task_id": task_id})


# Non-terminal agent-session statuses — a scope reporting any of these still has
# a live placement. (AgentSessionInfo.status ∈ starting|running|stopping|exited|
# killed|orphaned.)
_LIVE_STATUSES = frozenset({"starting", "running", "stopping"})


async def _live_scopes() -> "set[str] | None":
    """The set of workspace-unit slugs the agents service still has a live
    session for — the liveness signal :func:`kernel.recover_orphans` reconciles
    out-state placements against.

    The agents service may not be registered yet when the orchestrator boots
    (a full gateway restart brings both up concurrently), so the
    ``list_sessions`` call is retried a few times with a short backoff. On
    persistent failure we return ``None`` — the *unknown* sentinel that makes
    recovery a conservative no-op rather than blindly re-dispatching placements
    that might still be live.
    """
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            resp = await gatewayclient.call("agents", "list_sessions", {})
        except Exception as exc:  # noqa: BLE001 — agents may not be up yet
            last_err = exc
            await asyncio.sleep(1.0)
            continue
        sessions = (resp or {}).get("sessions", [])
        return {
            s["scope"] for s in sessions
            if s.get("scope") and s.get("status") in _LIVE_STATUSES
        }
    log.warning("orchestrator: agents service unreachable at boot (%s); "
                "skipping orphan recovery this cycle", last_err)
    return None


# Periodic reconcile (T5) tunables — env-overridable for live drills.
_RECONCILE_SWEEP_S = 60.0
_ORPHAN_MIN_AGE_S = 120.0


def _sweep_interval_s() -> float:
    try:
        return float(os.environ.get("AWM_RECONCILE_SWEEP_S") or _RECONCILE_SWEEP_S)
    except (TypeError, ValueError):
        return _RECONCILE_SWEEP_S


def _orphan_min_age_s() -> float:
    try:
        return float(os.environ.get("AWM_ORPHAN_MIN_AGE_S") or _ORPHAN_MIN_AGE_S)
    except (TypeError, ValueError):
        return _ORPHAN_MIN_AGE_S


async def _reconcile_sweep_once(state: dict) -> None:
    """One periodic reconcile pass: recover orphaned placements (min-age +
    two-strike guarded, against agents liveness), then re-scan the frontier.

    ``state`` carries the two-strike memory (``prev_orphaned``) across sweeps.
    LIVENESS-UNKNOWN (agents unreachable → ``_live_scopes`` returns ``None``):
    the whole cycle is skipped — no orphan recovery AND no frontier re-scan —
    so a transport blip is never read as "everything is dead"."""
    db = OrchestratorDAO()
    live = await _live_scopes()
    recovered, current = kernel.periodic_recover(
        db, live, state.get("prev_orphaned", set()),
        min_age_s=_orphan_min_age_s())
    state["prev_orphaned"] = current
    if live is None:
        log.warning("orchestrator: agents liveness unknown; skipping reconcile "
                    "sweep cycle (orphan recovery + frontier re-scan)")
        return
    if recovered:
        log.info("orchestrator: periodic sweep recovered %d orphaned placement(s)",
                 len(recovered))
    # Frontier re-scan (every sweep, unguarded): re-enqueue every resting node —
    # including the just-recovered ones. Cheap + idempotent: dispatch dedups an
    # in-flight intent, and ``_prepare`` re-checks the rest-state, so a node whose
    # placement is already live is skipped.
    dispatch.enqueue(kernel.reconcile(db))


async def _reconcile_sweep_loop() -> None:
    """Background task: re-run the boot recovery pair on a ~60s sweep so lost or
    dead work is re-driven without a restart. Started from :func:`_on_start`."""
    state: dict = {"prev_orphaned": set()}
    log.info("orchestrator: periodic reconcile started (sweep=%ss, min_age=%ss)",
             int(_sweep_interval_s()), int(_orphan_min_age_s()))
    while True:
        try:
            await asyncio.sleep(_sweep_interval_s())
            await _reconcile_sweep_once(state)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("orchestrator: reconcile sweep failed")


async def _on_start() -> None:
    """Stand up the DB, wire the reclaim seam, start the dispatch drain loop,
    recover orphaned placements, re-dispatch the resting frontier, then start the
    periodic reconcile sweep."""
    dao.init()
    operations.configure(reclaim_workspace_fn=_reclaim_workspace,
                         link_repos_fn=_link_repos,
                         stop_placement_fn=_stop_placement)
    dispatch.start_drain_loop()
    # Boot recovery: a full gateway restart bounces the agents service too, which
    # closes every open instance — so out-state placements may be dead with no
    # one left to report it. Reset orphaned out-state nodes (those whose unit has
    # no live session) back to their resting state...
    db = OrchestratorDAO()
    kernel.recover_orphans(db, await _live_scopes())
    # ...then the reconcile sweep re-dispatches the resting frontier — including
    # the just-recovered nodes — onto their retained workspace units.
    dispatch.enqueue(kernel.reconcile(db))
    # Long-session hardening (T5): the boot recovery pair also runs periodically,
    # so lost/dead work is re-driven within one ~60s sweep (guarded by min-age +
    # two-strike + liveness-unknown). Runs inside the event loop.
    asyncio.create_task(_reconcile_sweep_loop())


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
