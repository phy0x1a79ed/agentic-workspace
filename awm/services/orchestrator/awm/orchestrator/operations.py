"""The orchestrator's operation handlers — five public (MCP-visible) and the
privileged ones (manifest-omitted, reachable only by the agents harness).

Each handler is a thin, synchronous orchestration over the deterministic
:mod:`kernel` and the :class:`OrchestratorDAO`: it mutates the plan, recomputes
the frontier, and **enqueues** the resulting dispatch intents via
:mod:`dispatch` — it never deep-calls ``place_on_task`` inline.

The split between public and privileged is purely *manifest omission*: the
privileged ops (``claim`` / ``deliver`` / ``fail`` / ``decompose_commit`` /
``approve_plan`` / ``reject_plan`` / ``set_attached``) are absent from
``API_MANIFEST["functions"]`` so ``catalog.list_tools`` never projects them as
MCP tools, yet the gateway's catch-all ``/svc/<name>/fn/<fn>`` dispatch resolves
them straight out of the ``HANDLERS`` dict. That is the whole worker-honesty
mechanism — no gateway change required.

Two cross-service touch points are kept as **injectable seams**, defaulting to
read-only/no-op so the kernel never reaches a live gateway (and never prod
``:7819``): ``_project_exists`` (validate the project exists — never create it)
and ``_reclaim_workspace`` (free-but-retain a completed task's workspace unit;
wired to the workspace service's ``workspace_retain`` at integration).
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Callable

from awm import gatewayclient
from awm.orchestrator import dispatch, kernel
from awm.orchestrator.dao import PLAN_CONTRACT, OrchestratorDAO, init

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
# Free-but-retain a completed task's workspace unit (wired to the workspace
# service's ``workspace_retain`` at integration; no-op by default so the kernel
# stays offline).
_reclaim_workspace_fn: Callable[[str, str], None] | None = None


def configure(
    *,
    project_exists_fn: Callable[[str], bool] | None = None,
    reclaim_workspace_fn: Callable[[str, str], None] | None = None,
) -> None:
    """Override the cross-service seams (tests / integration)."""
    global _project_exists_fn, _reclaim_workspace_fn
    if project_exists_fn is not None:
        _project_exists_fn = project_exists_fn
    if reclaim_workspace_fn is not None:
        _reclaim_workspace_fn = reclaim_workspace_fn


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


def _has_consumers(dao: OrchestratorDAO, task_id: str) -> bool:
    """True if any task consumes a contract this task produces (it has somewhere
    downstream to route a failure)."""
    for c in dao.list_contracts_by_producer(task_id):
        if dao.list_consumers_of_contract(c["id"]):
            return True
    return False


def orch_task_attach(args: dict[str, Any]) -> dict[str, Any]:
    """Attach a task to the single global DAG as a prerequisite (upstream) of a
    consumer — root by default.

    ``produces`` = ``[{name, spec}]`` the task will deliver; ``depends_on`` =
    ``[contract_name]`` it consumes (a dependency edge per name, acyclicity
    checked). The task is wired UPSTREAM of ``consumer`` (an explicit task id, or
    the global root): the consumer gains a dependency edge onto each contract
    this task produces. A task with no produced contracts gets one synthetic
    ``deliverable`` so the consumer has something to wait on. A task with no unmet
    dependencies is born ``ready`` and dispatched.
    """
    init()
    project = str(args.get("project", "")).strip()
    goal = str(args.get("goal", "")).strip()
    if not project:
        raise ValueError("project is required")
    if not _project_exists_fn(project):
        raise ValueError(f"project {project!r} does not exist "
                         "(orchestrator never creates projects)")
    produces = args.get("produces") or []
    depends_on = args.get("depends_on") or []
    consumer_id = args.get("consumer")
    if any(c.get("name") == PLAN_CONTRACT for c in produces):
        raise ValueError(f"{PLAN_CONTRACT!r} is a reserved contract name")

    dao = OrchestratorDAO()
    with dao.transaction() as conn:
        root = dao.ensure_root(conn=conn)
        consumer = consumer_id or root["id"]
        if dao.get_task(consumer, conn=conn) is None:
            raise ValueError(f"consumer task {consumer!r} does not exist")
        task_id = dao.create_task(project, goal, state="blocked", conn=conn)

        produced_ids: list[str] = []
        for c in produces:
            produced_ids.append(
                dao.create_contract(project, c["name"], c.get("spec", ""),
                                    task_id, conn=conn))
        if not produced_ids:
            produced_ids.append(
                dao.create_contract(project, f"{project}:{task_id[:8]}:deliverable",
                                    goal, task_id, conn=conn))

        # This task's own prerequisites.
        for name in depends_on:
            contract = dao.get_contract_by_name(project, name, conn=conn)
            if contract is None:
                raise ValueError(f"unknown contract {name!r}")
            if not kernel.check_acyclic(dao, task_id, contract["producer_task"],
                                        conn=conn):
                raise ValueError(
                    f"dependency on {name!r} would create a cycle")
            dao.create_edge(task_id, contract["id"], conn=conn)

        # Wire upstream of the consumer: it depends on what this task produces.
        for cid in produced_ids:
            if not kernel.check_acyclic(dao, consumer, task_id, conn=conn):
                raise ValueError("attaching upstream of the consumer would "
                                 "create a cycle")
            dao.create_edge(consumer, cid, conn=conn)
    # Readiness + dispatch run after the structural commit. A newly-ready task
    # starts its worker journey at the PLAN leg (ready → planning).
    intents: list[tuple[str, str]] = []
    if kernel.recompute_readiness(dao, task_id):
        intents.append((task_id, "plan"))
    dispatch.enqueue(intents)
    task = dao.get_task(task_id)
    return {"task_id": task_id, "state": task["state"], "consumer": consumer}


def orch_task_create(args: dict[str, Any]) -> dict[str, Any]:
    """Create a fresh, still-vague task and place it into an attended initial
    specification.

    Unlike :func:`orch_task_attach` (which attaches work whose contracts are
    already known and sends it down the worker legs), this births the task into
    ``decompose_pending`` with the human ``attached`` and dispatches a planner to
    conversationally specify it — reusing the planner machinery, distinguished
    only by the orthogonal ``attached`` flag and an ``initial`` brief. The
    initial specification does **not** spend ``replan_budget`` (the budget is for
    genuine re-attempts). On commit the planner's sub-DAG (or empty re-spec) flows
    the task through the normal ``ready`` → … legs. The task hangs in the DAG via
    a synthetic ``deliverable`` upstream of the consumer (root by default).
    """
    init()
    project = str(args.get("project", "")).strip()
    goal = str(args.get("goal", "")).strip()
    if not project:
        raise ValueError("project is required")
    if not _project_exists_fn(project):
        raise ValueError(f"project {project!r} does not exist "
                         "(orchestrator never creates projects)")
    consumer_id = args.get("consumer")

    dao = OrchestratorDAO()
    with dao.transaction() as conn:
        root = dao.ensure_root(conn=conn)
        consumer = consumer_id or root["id"]
        if dao.get_task(consumer, conn=conn) is None:
            raise ValueError(f"consumer task {consumer!r} does not exist")
        task_id = dao.create_task(project, goal, state="decompose_pending",
                                  conn=conn)
        # A synthetic deliverable so the consumer has something to wait on; the
        # specifier defines the real contracts via decompose_commit.
        cid = dao.create_contract(
            project, f"{project}:{task_id[:8]}:deliverable", goal, task_id,
            conn=conn)
        if not kernel.check_acyclic(dao, consumer, task_id, conn=conn):
            raise ValueError("attaching upstream of the consumer would "
                             "create a cycle")
        dao.create_edge(consumer, cid, conn=conn)
        # Born attended; an 'initial' memory marks the SPECIFYING brief. No
        # budget is charged for the first specification.
        dao.update_task(task_id, attached=1, conn=conn)
        dao.add_attempt_memory(task_id, "created", reason_type="initial",
                               conn=conn)

    dispatch.enqueue([(task_id, "planner")])
    task = dao.get_task(task_id)
    return {"task_id": task_id, "state": task["state"], "consumer": consumer}


def orch_status(args: dict[str, Any]) -> dict[str, Any]:
    """Per-state counts + task rows, plus escalations.

    Global by default; pass ``project`` to filter. An escalation is a task
    resting in ``failed`` / ``abandoned`` that has nowhere left to route — the
    root sentinel, or a dead-end task with no downstream consumer. These are the
    only things handed back to the human (no auto-action).
    """
    init()
    project = str(args.get("project", "")).strip()
    dao = OrchestratorDAO()
    tasks = dao.list_tasks(project) if project else dao.query_all(
        "SELECT * FROM tasks ORDER BY created_at")
    counts = dict(Counter(t["state"] for t in tasks))
    escalations = [
        {"task_id": t["id"], "goal": t["goal"], "state": t["state"]}
        for t in tasks
        if t["state"] in ("failed", "abandoned")
        and (t["is_root"] or not _has_consumers(dao, t["id"]))
    ]
    root = dao.get_root()
    complete = root is not None and root["state"] == "completed"
    return {
        "project": project or None,
        "counts": counts,
        "complete": complete,
        "escalations": escalations,
        "tasks": [
            {"task_id": t["id"], "goal": t["goal"], "state": t["state"],
             "is_root": bool(t["is_root"]), "workspace_slug": t["workspace_slug"],
             "agent_ref": t["agent_ref"], "mode": t["mode"],
             "attached": bool(t["attached"])}
            for t in tasks
        ],
    }


def orch_frontier(args: dict[str, Any]) -> dict[str, Any]:
    """The ready nodes — the current worker frontier (optionally per-project)."""
    init()
    project = str(args.get("project", "")).strip()
    dao = OrchestratorDAO()
    if project:
        frontier = kernel.ready_frontier(dao, project)
    else:
        frontier = dao.query_all(
            "SELECT * FROM tasks WHERE state = 'ready' ORDER BY created_at")
    return {
        "project": project or None,
        "frontier": [
            {"task_id": t["id"], "goal": t["goal"],
             "workspace_slug": t["workspace_slug"]}
            for t in frontier
        ],
    }


def orch_dag(args: dict[str, Any]) -> dict[str, Any]:
    """The whole plan in one shot — tasks, contracts, and the dependency edges.

    Global by default; pass ``project`` to filter. A pure read projection of the
    three tables for a UI to build adjacency client-side. Edges are denormalized
    to carry both endpoints (``consumer_task`` + the contract's
    ``producer_task``) plus the contract ``name`` and a ``delivered`` flag, so a
    client never re-joins. The single global ``root_id`` is returned alongside so
    the consumer can special-case the sentinel.
    """
    init()
    project = str(args.get("project", "")).strip()
    dao = OrchestratorDAO()
    tasks = dao.list_tasks(project) if project else dao.query_all(
        "SELECT * FROM tasks ORDER BY created_at")
    contracts = dao.list_all_contracts(project or None)
    edges = dao.list_all_edges_joined(project or None)
    root = dao.get_root()
    return {
        "project": project or None,
        "root_id": root["id"] if root else None,
        "tasks": [
            {"task_id": t["id"], "goal": t["goal"], "state": t["state"],
             "is_root": bool(t["is_root"]), "mode": t["mode"],
             "workspace_slug": t["workspace_slug"], "agent_ref": t["agent_ref"],
             "attached": bool(t["attached"]),
             "created_at": t["created_at"], "updated_at": t["updated_at"]}
            for t in tasks
        ],
        "contracts": [
            {"contract_id": c["id"], "name": c["name"], "spec": c["spec"],
             "producer_task": c["producer_task"],
             "delivered": c["delivered_ts"] is not None,
             "payload_ref": c["payload_ref"], "delivered_ts": c["delivered_ts"]}
            for c in contracts
        ],
        "edges": [
            {"edge_id": e["edge_id"], "consumer_task": e["consumer_task"],
             "contract_id": e["contract_id"], "contract_name": e["contract_name"],
             "producer_task": e["producer_task"], "delivered": bool(e["delivered"])}
            for e in edges
        ],
    }


# ---------------------------------------------------------------------------
# Privileged ops (manifest-OMITTED — never MCP tools; agents harness only)
# ---------------------------------------------------------------------------


def claim(args: dict[str, Any]) -> dict[str, Any]:
    """Idempotent confirmation that a placed agent has attached.

    Not a lock acquisition — the placement (and the ``active`` flip, or the
    ``agent_ref`` stamped onto a ``decomposing`` node) already happened at
    dispatch. This just verifies the caller's
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
    """A placed agent delivers a payload (an artifact ref).

    Two cases, keyed by the contract name:

    * The reserved ``"plan"`` handoff — a ``plan`` agent delivers its staged plan
      while the task is ``planning``: record it in ``plan_ref``, flip the task to
      ``plan_delivered``, and dispatch a verifier. It is NOT a contracts-table
      delivery and never completes the task.
    * A normal contract delivery — a worker delivers one of its
      ``contracts_out``: screen it, mark it delivered, advance any newly-ready
      consumers (each starts at its PLAN leg), and once every produced contract
      is delivered, complete the task and free its workspace.
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

    # Reserved plan handoff: planning -> plan_delivered, then place a verifier.
    if contract_name == PLAN_CONTRACT:
        if task["state"] != "planning":
            return {"ok": False,
                    "error": f"plan delivered but task is {task['state']!r}, "
                             "not 'planning'"}
        dao.add_attempt_memory(task["id"], "delivered", reason_type="plan",
                               payload_ref=payload_ref)
        dao.update_task(task["id"], state="plan_delivered", plan_ref=payload_ref,
                        mode=None, agent_ref=None, placement_token=None)
        dispatch.enqueue([(task["id"], "verify")])
        fresh = dao.get_task(task["id"])
        return {"ok": True, "task_id": task["id"], "state": fresh["state"],
                "delivered": PLAN_CONTRACT}

    contract = dao.get_contract_by_name(task["project"], contract_name)
    if contract is None or contract["producer_task"] != task["id"]:
        return {"ok": False,
                "error": f"task does not produce contract {contract_name!r}"}

    intents: list[tuple[str, str]] = []
    dao.mark_contract_delivered(contract["id"], payload_ref)
    dao.add_attempt_memory(task["id"], "delivered", payload_ref=payload_ref)
    for consumer in dao.list_consumers_of_contract(contract["id"]):
        if kernel.recompute_readiness(dao, consumer):
            intents.append((consumer, "plan"))

    workspace_slug = task["workspace_slug"]
    if kernel.complete_task(dao, task["id"]):
        if _reclaim_workspace_fn is not None and workspace_slug:
            try:
                _reclaim_workspace_fn(task["project"], workspace_slug)
            except Exception as exc:  # noqa: BLE001 — reclaim is best-effort
                log.warning("orchestrator: workspace reclaim failed: %s", exc)

    dispatch.enqueue(intents)
    fresh = dao.get_task(task["id"])
    return {"ok": True, "task_id": task["id"], "state": fresh["state"],
            "delivered": contract_name}


def fail(args: dict[str, Any]) -> dict[str, Any]:
    """A worker or planner reports a failed attempt — a *signal*, not a terminus.

    Records an ``attempt_memory`` then routes by ``reason_type`` / current state
    (see :func:`kernel.route_failure`): ``transient-error`` auto-retries to
    ``ready``; ``needs-decomposition`` self-decomposes (``decomposing``);
    ``contract-unsatisfiable`` rests in ``failed`` and routes its downstream
    consumers to ``decomposing``; a planner give-up spends ``replan_budget`` and
    eventually rests in ``abandoned`` (also routing its consumers).
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

    intents = kernel.route_failure(dao, task["id"], reason_type)
    dispatch.enqueue(intents)
    fresh = dao.get_task(task["id"])
    return {"ok": True, "task_id": task["id"], "state": fresh["state"]}


def decompose_commit(args: dict[str, Any]) -> dict[str, Any]:
    """A planner commits a decomposition of a ``decomposing`` task.

    Atomically creates the upstream child tasks, the contracts they produce, and
    the dependency edges among them; then the **decomposing task itself gains a
    dependency edge onto each terminal contract of that sub-DAG** and rests
    ``blocked``. It re-runs through the normal legs once the sub-DAG delivers
    (there is no auto-complete). Newly-ready children (leaves with no unmet
    dependency) are dispatched.

    The kernel is authoritative on coherence: the sub-DAG must be acyclic and
    must **funnel** — every child must reach the decomposing task (its unique
    global sink) along dependency edges. An incoherent sub-DAG is rejected; the
    whole commit rolls back and (because budget is charged on *entry* to
    ``decompose_pending``, not here) costs no ``replan_budget``.

    Terminal contracts are those produced inside the sub-DAG and consumed by no
    child; ``depends_on`` may name them explicitly, else they are derived.

    Payload shape::

        children   = [{"ref": "<local>", "goal": "..."}]
        contracts  = [{"name": "...", "spec": "...", "producer": "<local ref>"}]
        edges      = [{"consumer": "<local ref>", "contract": "<name>"}]
        depends_on = ["<terminal contract name>", ...]   # optional
    """
    init()
    dao = OrchestratorDAO()
    task = dao.get_task(args["task_id"])
    if task is None:
        return {"ok": False, "error": "unknown task"}
    _verify_agent(task, args.get("agent_ref"))

    children = args.get("children") or []
    contracts = args.get("contracts") or []
    edges = args.get("edges") or []
    depends_on = args.get("depends_on") or []
    project = task["project"]

    local_to_id: dict[str, str] = {}
    created: list[tuple[str, str]] = []  # (name, contract_id) for sub-DAG contracts
    with dao.transaction() as conn:
        for ch in children:
            cid = dao.create_task(project, ch.get("goal", ""), state="blocked",
                                  conn=conn)
            local_to_id[str(ch["ref"])] = cid
        for c in contracts:
            producer = local_to_id.get(str(c["producer"]))
            if producer is None:
                raise ValueError(f"contract {c['name']!r} names unknown "
                                 f"producer ref {c['producer']!r}")
            cid = dao.create_contract(project, c["name"], c.get("spec", ""),
                                      producer, conn=conn)
            created.append((c["name"], cid))
        consumed_ids: set[str] = set()
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
            consumed_ids.add(contract["id"])

        # The decomposing task now depends on the sub-DAG's terminal contracts —
        # explicit ``depends_on`` if given, else every sub-DAG contract that no
        # child consumes (the sinks).
        if depends_on:
            terminal = []
            for name in depends_on:
                contract = dao.get_contract_by_name(project, name, conn=conn)
                if contract is None:
                    raise ValueError(f"unknown terminal contract {name!r}")
                terminal.append(contract["id"])
        else:
            terminal = [cid for (_, cid) in created if cid not in consumed_ids]
        for cid in terminal:
            contract = dao.get_contract(cid, conn=conn)
            if not kernel.check_acyclic(dao, task["id"],
                                        contract["producer_task"], conn=conn):
                raise ValueError("decomposing task depending on its sub-DAG "
                                 "would create a cycle")
            dao.create_edge(task["id"], cid, conn=conn)

        # Funnel rule (kernel-authoritative): every child must reach the
        # decomposing task along the dependency edges just inserted.
        if not kernel.check_funnel(
            dao, task["id"], local_to_id.values(), conn=conn
        ):
            raise ValueError(
                "incoherent sub-DAG: every child must funnel into the "
                "decomposing task (its unique sink)")

        # The decomposing task rests blocked, awaiting its own sub-DAG. Its
        # workspace is freed (children get fresh units; it gets a new one when it
        # re-runs). Budget was already charged on entry to decompose_pending.
        dao.update_task(task["id"], state="blocked", mode=None,
                        agent_ref=None, placement_token=None,
                        workspace_slug=None, conn=conn)

    intents: list[tuple[str, str]] = []
    for cid in local_to_id.values():
        if kernel.recompute_readiness(dao, cid):
            intents.append((cid, "plan"))
    # If the planner committed an empty (or terminal-less) sub-DAG the task has
    # no new dependency and is immediately ready again — restart at the PLAN leg.
    if kernel.recompute_readiness(dao, task["id"]):
        intents.append((task["id"], "plan"))
    dispatch.enqueue(intents)
    fresh = dao.get_task(task["id"])
    return {"ok": True, "task_id": task["id"], "state": fresh["state"],
            "children": list(local_to_id.values())}


def approve_plan(args: dict[str, Any]) -> dict[str, Any]:
    """A verifier approves a ``verifying_plan`` task's staged plan.

    Flips the task to ``plan_approved`` (the verifier's placement ends) and
    dispatches the worker that does the real work (``plan_approved`` → active).
    """
    init()
    dao = OrchestratorDAO()
    task = dao.get_task(args["task_id"])
    if task is None:
        return {"ok": False, "error": "unknown task"}
    _verify_agent(task, args.get("agent_ref"))
    if task["state"] != "verifying_plan":
        return {"ok": False,
                "error": f"approve_plan on a {task['state']!r} task "
                         "(expected 'verifying_plan')"}
    dao.update_task(task["id"], state="plan_approved", mode=None,
                    agent_ref=None, placement_token=None)
    dispatch.enqueue([(task["id"], "worker")])
    fresh = dao.get_task(task["id"])
    return {"ok": True, "task_id": task["id"], "state": fresh["state"]}


def reject_plan(args: dict[str, Any]) -> dict[str, Any]:
    """A verifier rejects a ``verifying_plan`` task's staged plan.

    Records the rejection, discards the plan, and re-plans: spend one
    ``replan_budget`` unit and return to ``ready`` (a fresh plan agent is
    dispatched). With no budget left the task gives up — ``abandoned``, routing
    its consumers (``kernel.abandon``).
    """
    init()
    dao = OrchestratorDAO()
    task = dao.get_task(args["task_id"])
    if task is None:
        return {"ok": False, "error": "unknown task"}
    _verify_agent(task, args.get("agent_ref"))
    if task["state"] != "verifying_plan":
        return {"ok": False,
                "error": f"reject_plan on a {task['state']!r} task "
                         "(expected 'verifying_plan')"}
    # The verifier relay sends the reason under ``reason``; accept either key.
    reason_text = args.get("reason_text") or args.get("reason") or ""
    dao.add_attempt_memory(task["id"], "failed", reason_type="plan-rejected",
                           reason_text=str(reason_text))

    budget = int(task["replan_budget"])
    if budget <= 0:
        intents = kernel.abandon(dao, task["id"])
    else:
        dao.update_task(task["id"], state="ready", replan_budget=budget - 1,
                        mode=None, agent_ref=None, placement_token=None,
                        plan_ref=None)
        intents = [(task["id"], "plan")]
    dispatch.enqueue(intents)
    fresh = dao.get_task(task["id"])
    return {"ok": True, "task_id": task["id"], "state": fresh["state"]}


def set_attached(args: dict[str, Any]) -> dict[str, Any]:
    """Set a task's orthogonal human-attached flag (agents service authoritative).

    Attachment is independent of the lifecycle state — any live placement may be
    attached. While attached a task is exempt from auto-reclaim / force-progress
    (the agents side freezes its turn budget); the state machine is untouched.
    """
    init()
    dao = OrchestratorDAO()
    task = dao.get_task(args["task_id"])
    if task is None:
        return {"ok": False, "error": "unknown task"}
    _verify_agent(task, args.get("agent_ref"))
    attached = 1 if args.get("attached") else 0
    dao.update_task(task["id"], attached=attached)
    return {"ok": True, "task_id": task["id"], "attached": bool(attached)}


def search_tasks(args: dict[str, Any]) -> dict[str, Any]:
    """Planner read: existing tasks, for sub-DAG node reuse (manifest-omitted).

    Substring match (case-insensitive) on the goal; optionally project-scoped.
    Read-only — never mutates the plan. The root sentinel is omitted (it is not a
    reusable work node).
    """
    init()
    dao = OrchestratorDAO()
    project = str(args.get("project") or "").strip()
    query = str(args.get("query") or "").strip().lower()
    rows = (dao.list_tasks(project) if project
            else dao.query_all("SELECT * FROM tasks ORDER BY created_at"))
    tasks = [
        {"task_id": t["id"], "goal": t["goal"], "state": t["state"],
         "project": t["project"]}
        for t in rows
        if not t["is_root"] and (not query or query in (t["goal"] or "").lower())
    ]
    return {"tasks": tasks}


def search_contracts(args: dict[str, Any]) -> dict[str, Any]:
    """Planner read: existing contracts, for sub-DAG reuse (manifest-omitted).

    Substring match (case-insensitive) on the contract name; optionally
    project-scoped. Read-only.
    """
    init()
    dao = OrchestratorDAO()
    project = str(args.get("project") or "").strip()
    query = str(args.get("query") or "").strip().lower()
    rows = dao.query_all("SELECT * FROM contracts ORDER BY created_at")
    contracts = [
        {"contract_id": c["id"], "name": c["name"], "spec": c["spec"],
         "producer_task": c["producer_task"], "project": c["project"],
         "delivered": c["delivered_ts"] is not None}
        for c in rows
        if (not project or c["project"] == project)
        and (not query or query in (c["name"] or "").lower())
    ]
    return {"contracts": contracts}
