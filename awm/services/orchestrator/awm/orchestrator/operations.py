"""The orchestrator's operation handlers — four public (MCP-visible) and four
privileged (manifest-omitted, reachable only by the agents harness).

Each handler is a thin, synchronous orchestration over the deterministic
:mod:`kernel` and the :class:`OrchestratorDAO`: it mutates the plan, recomputes
the frontier, and **enqueues** the resulting dispatch intents via
:mod:`dispatch` — it never deep-calls ``place_on_task`` inline.

The split between public and privileged is purely *manifest omission*: the four
privileged ops (``claim`` / ``deliver`` / ``fail`` / ``decompose_commit``) are
absent from ``API_MANIFEST["functions"]`` so ``catalog.list_tools`` never
projects them as MCP tools, yet the gateway's catch-all ``/svc/<name>/fn/<fn>``
dispatch resolves them straight out of the ``HANDLERS`` dict. That is the whole
worker-honesty mechanism — no gateway change required.

Two cross-service touch points are kept as **injectable seams**, defaulting to
read-only/no-op so T1 never reaches a live gateway (and never prod ``:7819``):
``_project_exists`` (validate the project exists — never create it) and
``_reclaim_scope`` (free a completed task's scope; wired to
``scopes.complete_scope`` at T3).
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Callable

from awm import gatewayclient
from awm.orchestrator import dispatch, kernel
from awm.orchestrator.dao import OrchestratorDAO, init

log = logging.getLogger("awm.orchestrator.operations")


# ---------------------------------------------------------------------------
# Injectable cross-service seams (read-only / no-op by default — T1 is offline)
# ---------------------------------------------------------------------------


def _default_project_exists(project: str) -> bool:
    """Best-effort project-existence check via the scopes service (read-only).

    Uses ``project_search`` and matches the name exactly — it never creates a
    project. Defaults to permissive (True) if the gateway is unreachable, so a
    transient gateway blip can't wedge plan creation; T3 can tighten this.
    """
    try:
        res = gatewayclient.call_sync("scopes", "project_search",
                                      {"query": project})
    except Exception as exc:  # noqa: BLE001
        log.warning("orchestrator: project existence check failed (%s); "
                    "allowing", exc)
        return True
    rows = (res or {}).get("projects") or []
    return any(r.get("name") == project for r in rows)


_project_exists_fn: Callable[[str], bool] = _default_project_exists
_reclaim_scope_fn: Callable[[str, str], None] | None = None


def configure(
    *,
    project_exists_fn: Callable[[str], bool] | None = None,
    reclaim_scope_fn: Callable[[str, str], None] | None = None,
) -> None:
    """Override the cross-service seams (tests / T3 integration)."""
    global _project_exists_fn, _reclaim_scope_fn
    if project_exists_fn is not None:
        _project_exists_fn = project_exists_fn
    if reclaim_scope_fn is not None:
        _reclaim_scope_fn = reclaim_scope_fn


def _verify_agent(task: dict, agent_ref: str | None) -> None:
    """Reject a privileged mutation whose ``agent_ref`` is not the one placed on
    the task. A task with no placement recorded yet (None) is left permissive."""
    placed = task.get("agent_ref")
    if placed and agent_ref and placed != agent_ref:
        raise ValueError(
            f"agent_ref {agent_ref!r} does not match the placement "
            f"{placed!r} on task {task['id']}")


# ---------------------------------------------------------------------------
# Public ops (manifest-visible)
# ---------------------------------------------------------------------------


def orch_plan_create(args: dict[str, Any]) -> dict[str, Any]:
    """Attach a root task to an EXISTING project and dispatch it.

    Validates the project exists (never creates one), mints the root task
    (born ``ready``) plus its single outgoing "deliverable" contract, and
    enqueues the root for a worker placement.
    """
    init()
    project = str(args.get("project", "")).strip()
    goal = str(args.get("goal", "")).strip()
    if not project:
        raise ValueError("project is required")
    if not goal:
        raise ValueError("goal is required")
    if not _project_exists_fn(project):
        raise ValueError(f"project {project!r} does not exist "
                         "(orchestrator never creates projects)")

    dao = OrchestratorDAO()
    contract_name = str(args.get("contract") or f"{project}:deliverable")
    with dao.transaction() as conn:
        root = dao.create_task(project, goal, state="ready", conn=conn)
        dao.create_contract(project, contract_name, goal, root, conn=conn)
    dispatch.enqueue([(root, "worker")])
    return {"task_id": root, "contract": contract_name, "state": "ready"}


def orch_task_attach(args: dict[str, Any]) -> dict[str, Any]:
    """Attach a task to the plan: optionally under a parent, producing and/or
    consuming named contracts.

    ``produces`` = ``[{name, spec}]`` the task will deliver; ``depends_on`` =
    ``[contract_name]`` it consumes (a dependency edge per name, acyclicity
    checked). A task with no unmet dependencies is born ``ready`` and dispatched.
    """
    init()
    project = str(args.get("project", "")).strip()
    goal = str(args.get("goal", "")).strip()
    if not project:
        raise ValueError("project is required")
    parent_id = args.get("parent_id")
    produces = args.get("produces") or []
    depends_on = args.get("depends_on") or []

    dao = OrchestratorDAO()
    intents: list[tuple[str, str]] = []
    with dao.transaction() as conn:
        if parent_id is not None and dao.get_task(parent_id, conn=conn) is None:
            raise ValueError(f"parent task {parent_id!r} does not exist")
        task_id = dao.create_task(project, goal, state="pending",
                                  parent_id=parent_id, conn=conn)
        for c in produces:
            dao.create_contract(project, c["name"], c.get("spec", ""),
                                task_id, conn=conn)
        for name in depends_on:
            contract = dao.get_contract_by_name(project, name, conn=conn)
            if contract is None:
                raise ValueError(f"unknown contract {name!r}")
            if not kernel.check_acyclic(dao, task_id, contract["producer_task"],
                                        conn=conn):
                raise ValueError(
                    f"dependency on {name!r} would create a cycle")
            dao.create_edge(task_id, contract["id"], conn=conn)
    # Readiness + dispatch run after the structural commit.
    if kernel.recompute_readiness(dao, task_id):
        intents.append((task_id, "worker"))
    dispatch.enqueue(intents)
    task = dao.get_task(task_id)
    return {"task_id": task_id, "state": task["state"]}


def orch_status(args: dict[str, Any]) -> dict[str, Any]:
    """Per-state counts + task rows for a project, plus root escalations.

    A root task resting in ``failed`` or ``discarded`` is surfaced under
    ``escalations`` — the only thing v1 hands back to the human (no auto-action).
    """
    init()
    project = str(args.get("project", "")).strip()
    if not project:
        raise ValueError("project is required")
    dao = OrchestratorDAO()
    tasks = dao.list_tasks(project)
    counts = dict(Counter(t["state"] for t in tasks))
    escalations = [
        {"task_id": t["id"], "goal": t["goal"], "state": t["state"]}
        for t in tasks
        if t["parent_id"] is None and t["state"] in ("failed", "discarded")
    ]
    complete = bool(tasks) and all(t["state"] == "delivered" for t in tasks)
    return {
        "project": project,
        "counts": counts,
        "complete": complete,
        "escalations": escalations,
        "tasks": [
            {"task_id": t["id"], "goal": t["goal"], "state": t["state"],
             "parent_id": t["parent_id"], "scope_slug": t["scope_slug"],
             "agent_ref": t["agent_ref"], "mode": t["mode"]}
            for t in tasks
        ],
    }


def orch_frontier(args: dict[str, Any]) -> dict[str, Any]:
    """The ready leaves of a project — the current worker frontier."""
    init()
    project = str(args.get("project", "")).strip()
    if not project:
        raise ValueError("project is required")
    dao = OrchestratorDAO()
    frontier = kernel.ready_frontier(dao, project)
    return {
        "project": project,
        "frontier": [
            {"task_id": t["id"], "goal": t["goal"], "scope_slug": t["scope_slug"]}
            for t in frontier
        ],
    }


# ---------------------------------------------------------------------------
# Privileged ops (manifest-OMITTED — never MCP tools; agents harness only)
# ---------------------------------------------------------------------------


def claim(args: dict[str, Any]) -> dict[str, Any]:
    """Idempotent confirmation that a placed agent has attached.

    Not a lock acquisition — the placement (and the ``active``/``analyzing``
    flip) already happened at dispatch. This just verifies the caller's
    ``agent_ref`` matches the one recorded on the task. Liveness is the agents
    service's concern, reported back via :func:`fail` (``transient-error``).
    """
    init()
    dao = OrchestratorDAO()
    task = dao.get_task(args["task_id"])
    if task is None:
        return {"ok": False, "error": "unknown task"}
    agent_ref = args.get("agent_ref")
    ok = (task["agent_ref"] == agent_ref) if task["agent_ref"] else True
    return {"ok": ok, "task_id": task["id"], "state": task["state"]}


def deliver(args: dict[str, Any]) -> dict[str, Any]:
    """A worker delivers a contract's payload (an artifact ref).

    Screens the payload, marks the contract delivered, records an
    ``attempt_memory``, advances any newly-ready consumers, and — once every
    contract the task produces is delivered — completes the task, frees its
    scope, and aggregates the result up the containment tree. All resulting
    dispatch intents are enqueued.
    """
    init()
    dao = OrchestratorDAO()
    task = dao.get_task(args["task_id"])
    if task is None:
        return {"ok": False, "error": "unknown task"}
    _verify_agent(task, args.get("agent_ref"))

    payload_ref = args.get("payload_ref")  # take it before any reclaim
    if not kernel.screen(payload_ref):
        return {"ok": False, "error": "delivery failed screen (empty payload_ref)"}

    contract_name = args.get("contract")
    contract = dao.get_contract_by_name(task["project"], contract_name)
    if contract is None or contract["producer_task"] != task["id"]:
        return {"ok": False,
                "error": f"task does not produce contract {contract_name!r}"}

    intents: list[tuple[str, str]] = []
    dao.mark_contract_delivered(contract["id"], payload_ref)
    dao.add_attempt_memory(task["id"], "delivered", payload_ref=payload_ref)
    for consumer in dao.list_consumers_of_contract(contract["id"]):
        if kernel.recompute_readiness(dao, consumer):
            intents.append((consumer, "worker"))

    scope_slug = task["scope_slug"]
    if kernel.complete_task(dao, task["id"]):
        if _reclaim_scope_fn is not None and scope_slug:
            try:
                _reclaim_scope_fn(task["project"], scope_slug)
            except Exception as exc:  # noqa: BLE001 — reclaim is best-effort
                log.warning("orchestrator: scope reclaim failed: %s", exc)
        intents += kernel.aggregate(dao, task["parent_id"])

    dispatch.enqueue(intents)
    fresh = dao.get_task(task["id"])
    return {"ok": True, "task_id": task["id"], "state": fresh["state"],
            "delivered": contract_name}


def fail(args: dict[str, Any]) -> dict[str, Any]:
    """A worker or planner reports a failed attempt — a *signal*, not a terminus.

    Records an ``attempt_memory`` then routes by ``reason_type`` /current state
    (see :func:`kernel.route_failure`): ``transient-error`` auto-retries to
    ``ready``; ``needs-decomposition`` / ``contract-unsatisfiable`` rest in
    ``failed`` to await a planner; a planner give-up spends ``replan_budget`` and
    may go ``discarded`` (escalating to the parent).
    """
    init()
    dao = OrchestratorDAO()
    task = dao.get_task(args["task_id"])
    if task is None:
        return {"ok": False, "error": "unknown task"}
    _verify_agent(task, args.get("agent_ref"))

    reason_type = args.get("reason_type", "transient-error")
    dao.add_attempt_memory(
        task["id"], "failed", reason_type=reason_type,
        reason_text=str(args.get("reason_text", "")),
        payload_ref=args.get("partial_ref"))

    was_analyzing = task["state"] == "analyzing"
    intent = kernel.route_failure(dao, task["id"], reason_type)
    intents = [intent] if intent else []
    # A planner give-up (analyzing -> discarded) escalates to the parent.
    if was_analyzing and intent is None:
        intents += kernel.aggregate(dao, task["parent_id"])

    dispatch.enqueue(intents)
    fresh = dao.get_task(task["id"])
    return {"ok": True, "task_id": task["id"], "state": fresh["state"]}


def decompose_commit(args: dict[str, Any]) -> dict[str, Any]:
    """A planner commits a decomposition of a ``failed``/``analyzing`` task.

    Atomically creates the child tasks, the contracts they produce, and the
    dependency edges among them (acyclicity checked within the transaction);
    the parent becomes a ``pending`` composite awaiting its children, spending
    one unit of ``replan_budget``. Newly-ready children are dispatched.

    Payload shape::

        children  = [{"ref": "<local>", "goal": "..."}]
        contracts = [{"name": "...", "spec": "...", "producer": "<local ref>"}]
        edges     = [{"consumer": "<local ref>", "contract": "<name>"}]
    """
    init()
    dao = OrchestratorDAO()
    parent = dao.get_task(args["task_id"])
    if parent is None:
        return {"ok": False, "error": "unknown task"}
    _verify_agent(parent, args.get("agent_ref"))

    children = args.get("children") or []
    contracts = args.get("contracts") or []
    edges = args.get("edges") or []
    project = parent["project"]
    budget = int(parent["replan_budget"]) - 1

    local_to_id: dict[str, str] = {}
    with dao.transaction() as conn:
        for ch in children:
            cid = dao.create_task(project, ch.get("goal", ""), state="pending",
                                  parent_id=parent["id"], conn=conn)
            local_to_id[str(ch["ref"])] = cid
        for c in contracts:
            producer = local_to_id.get(str(c["producer"]))
            if producer is None:
                raise ValueError(f"contract {c['name']!r} names unknown "
                                 f"producer ref {c['producer']!r}")
            dao.create_contract(project, c["name"], c.get("spec", ""),
                                producer, conn=conn)
        for e in edges:
            consumer = local_to_id.get(str(e["consumer"]))
            if consumer is None:
                raise ValueError(f"edge names unknown consumer ref "
                                 f"{e['consumer']!r}")
            contract = dao.get_contract_by_name(project, e["contract"], conn=conn)
            if contract is None:
                raise ValueError(f"edge names unknown contract {e['contract']!r}")
            if not kernel.check_acyclic(dao, consumer, contract["producer_task"],
                                        conn=conn):
                raise ValueError(
                    f"edge {e['consumer']}->{e['contract']} would create a cycle")
            dao.create_edge(consumer, contract["id"], conn=conn)
        # Parent becomes a composite awaiting its children.
        dao.update_task(parent["id"], state="pending", mode=None,
                        agent_ref=None, placement_token=None,
                        replan_budget=budget, conn=conn)

    intents: list[tuple[str, str]] = []
    for cid in local_to_id.values():
        if kernel.recompute_readiness(dao, cid):
            intents.append((cid, "worker"))
    dispatch.enqueue(intents)
    return {"ok": True, "task_id": parent["id"], "state": "pending",
            "children": list(local_to_id.values())}
