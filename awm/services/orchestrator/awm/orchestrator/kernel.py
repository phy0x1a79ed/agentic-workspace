"""The deterministic plan kernel — plain Python over the DAO, no LLM, no
background loop.

Every function here is reconstructable: given the durable plan in the DB it
computes the same answer with no agent running. The kernel never talks to the
agents service and never touches an event loop; it mutates plan state and
**returns the dispatch intents** it produced as a list of ``(task_id, mode)``
pairs (``mode`` ∈ ``"worker"`` | ``"planner"``). The caller (the operations
handlers) is responsible for enqueuing those onto the dispatch queue — so the
kernel stays a pure, synchronously-testable state machine and the asyncio lives
entirely in ``dispatch.py``.

The node lifecycle (see ``dao.STATES``) — a *resting* state needs a placement,
the matching *out* state means one is live:

    a worker is needed:   ready  -> active  -> completed   (done)
    a planner is needed:  decomposing(agent NULL) -> decomposing(agent set)

The worker pair is distinguished by state name (``ready`` vs ``active``); the
planner pair shares the ``decomposing`` state and is distinguished by whether an
``agent_ref`` is recorded (NULL = needs a planner; set = planner is out). That
keeps the lifecycle to the seven named states.

There is **one** graph — the dependency DAG. A task that is too big does not
become a containment parent; it *decomposes* into an upstream sub-DAG and then
depends on that sub-DAG's terminal contracts (it rests ``blocked`` and re-runs
as a normal worker once they deliver). When a task gives up (``failed`` /
``abandoned``) its downstream **consumers** re-enter ``decomposing`` to refine
or replace it. Root has no consumer, so its give-up escalates to the human.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a hard import cost on the hot path
    from awm.orchestrator.dao import OrchestratorDAO

# A worker may fail transiently this many times (auto-retry to ``ready``)
# before the node rests in ``failed``.
TRANSIENT_RETRY_CAP = 3

DispatchIntent = tuple[str, str]  # (task_id, mode)


# ---------------------------------------------------------------------------
# Readiness — blocked -> ready when every dependency contract is delivered
# ---------------------------------------------------------------------------


def recompute_readiness(dao: "OrchestratorDAO", task_id: str) -> bool:
    """Advance a ``blocked`` node whose incoming dependency contracts are all
    delivered. Returns True iff the node is now ``ready`` (i.e. wants a worker).

    A normal node with all dependencies delivered flips ``blocked -> ready``. The
    **root** sentinel never gets a worker: when it has at least one prerequisite
    and they are all delivered it flips ``blocked -> completed`` instead, and the
    function returns False so root is never dispatched. A node already past
    ``blocked`` is left untouched.
    """
    task = dao.get_task(task_id)
    if task is None or task["state"] != "blocked":
        return task is not None and task["state"] == "ready"
    incoming = dao.list_incoming_edges(task_id)
    if not all(e["delivered_ts"] is not None for e in incoming):
        return False
    if task["is_root"]:
        # Root is a sentinel — it completes when its prerequisites do, and is
        # never placed. An empty root (no prerequisites yet) stays ``blocked``.
        if incoming:
            dao.update_task(task_id, state="completed")
        return False
    dao.update_task(task_id, state="ready")
    return True


def ready_frontier(dao: "OrchestratorDAO", project: str) -> list[dict]:
    """The nodes currently in ``ready`` for a project — the worker frontier."""
    return dao.list_tasks_by_state(project, "ready")


# ---------------------------------------------------------------------------
# Acyclicity — checked ONLY on dependency-edge insert
# ---------------------------------------------------------------------------


def check_acyclic(
    dao: "OrchestratorDAO", consumer: str, producer: str, *, conn=None
) -> bool:
    """True if adding "consumer depends-on producer" keeps the dependency graph
    acyclic.

    A cycle would form iff ``producer`` already (transitively) depends on
    ``consumer``. DFS the depends-on graph from ``producer``; reject if it
    reaches ``consumer`` (or if producer == consumer, a self-loop). Pass ``conn``
    to DFS over the *uncommitted* edges of an open transaction (so a batch of
    edges inserted together — e.g. in ``decompose_commit`` — is checked against
    its own prior inserts).
    """
    if consumer == producer:
        return False
    seen: set[str] = set()
    stack = [producer]
    while stack:
        node = stack.pop()
        if node == consumer:
            return False
        if node in seen:
            continue
        seen.add(node)
        stack.extend(dao.depends_on(node, conn=conn))
    return True


# ---------------------------------------------------------------------------
# Cheap delivery screen — loose by design
# ---------------------------------------------------------------------------


def screen(payload_ref: str | None) -> bool:
    """Cheap structural screen on a delivery's artifact ref.

    Deliberately loose: a non-empty string passes. Deep validation (resolving
    the artifact, checking it matches the contract spec) is the consumer's job,
    not the kernel's gate. Returns True if the delivery may proceed.
    """
    return isinstance(payload_ref, str) and bool(payload_ref.strip())


# ---------------------------------------------------------------------------
# Failure routing — by reason_type and current state
# ---------------------------------------------------------------------------


def _route_consumers(dao: "OrchestratorDAO", task_id: str) -> list[DispatchIntent]:
    """Route every downstream consumer of ``task_id`` into ``decomposing``.

    For each contract the failed/abandoned task produces, each consuming task
    re-enters ``decomposing`` to refine or replace the broken prerequisite — and
    is enqueued for a planner. A consumer that is **root** cannot re-plan, so it
    is marked ``abandoned`` (surfaced as an escalation by ``orch_status``). A
    consumer already in ``decomposing`` (or a terminal state) is left alone.
    """
    out: list[DispatchIntent] = []
    cleared = dict(mode=None, agent_ref=None, placement_token=None)
    for c in dao.list_contracts_by_producer(task_id):
        for consumer in dao.list_consumers_of_contract(c["id"]):
            ct = dao.get_task(consumer)
            if ct is None or ct["state"] in ("decomposing", "abandoned"):
                continue
            if ct["is_root"]:
                dao.update_task(consumer, state="abandoned", **cleared)
                continue
            dao.update_task(consumer, state="decomposing", **cleared)
            out.append((consumer, "planner"))
    return out


def route_failure(
    dao: "OrchestratorDAO", task_id: str, reason_type: str
) -> list[DispatchIntent]:
    """Route a failed attempt to its next resting state; clear the placement.

    Returns the dispatch intents the routing produced.

    * A failure on a ``decomposing`` node is a *planner* give-up: spend one unit
      of ``replan_budget``. While budget remains the node stays ``decomposing``
      with its placement cleared (re-dispatch a planner); once exhausted it rests
      in ``abandoned`` and its downstream consumers re-enter ``decomposing``.
    * ``transient-error`` on a worker: auto-retry ``-> ready`` (bounded by
      ``retry_count`` ≤ :data:`TRANSIENT_RETRY_CAP`); once the cap is hit the node
      rests in ``failed`` and routes its consumers.
    * ``needs-decomposition``: the task is too big — it self-decomposes,
      ``-> decomposing`` (a planner is placed on it).
    * ``contract-unsatisfiable`` / impossible: the node rests in ``failed`` and
      routes its downstream consumers to re-plan it.
    """
    task = dao.get_task(task_id)
    if task is None:
        return []

    # Clear the agent placement but KEEP ``scope_slug``: the scope (worktree)
    # persists when an agent detaches, and a planner reuses the failed node's
    # slug so it can read the partial work left behind. The slug is only freed
    # on terminal completion (``complete_task``) or give-up (``abandoned``).
    cleared = dict(mode=None, agent_ref=None, placement_token=None)

    # Planner give-up (the failing node is mid-decomposition).
    if task["state"] == "decomposing":
        budget = int(task["replan_budget"]) - 1
        if budget <= 0:
            dao.update_task(task_id, state="abandoned", replan_budget=budget,
                            scope_slug=None, **cleared)
            return _route_consumers(dao, task_id)
        dao.update_task(task_id, state="decomposing", replan_budget=budget,
                        **cleared)
        return [(task_id, "planner")]

    # Worker transient error — auto-retry until the cap.
    if reason_type == "transient-error":
        n = int(task["retry_count"]) + 1
        if n <= TRANSIENT_RETRY_CAP:
            dao.update_task(task_id, state="ready", retry_count=n, **cleared)
            return [(task_id, "worker")]
        dao.update_task(task_id, state="failed", retry_count=n, **cleared)
        return _route_consumers(dao, task_id)

    # Worker signals the task is too big — self-decompose (a planner is placed).
    if reason_type == "needs-decomposition":
        dao.update_task(task_id, state="decomposing", **cleared)
        return [(task_id, "planner")]

    # Contract-unsatisfiable / impossible — rest in ``failed``; consumers replan.
    dao.update_task(task_id, state="failed", **cleared)
    return _route_consumers(dao, task_id)


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


def complete_task(dao: "OrchestratorDAO", task_id: str) -> bool:
    """Mark a task ``completed`` and clear its placement, **iff** every contract
    it produces is delivered. Returns True when the task was completed.

    A task that produces no contracts is treated as complete on the caller's
    say-so (the deliver handler invokes this only after recording the delivery).
    """
    contracts = dao.list_contracts_by_producer(task_id)
    if any(c["delivered_ts"] is None for c in contracts):
        return False
    dao.update_task(task_id, state="completed", mode=None, agent_ref=None,
                    scope_slug=None, placement_token=None)
    return True


# ---------------------------------------------------------------------------
# Boot reconcile — re-dispatch the resting frontier, leave placements alone
# ---------------------------------------------------------------------------


def reconcile(dao: "OrchestratorDAO") -> list[DispatchIntent]:
    """Recompute the dispatch frontier at boot.

    Re-dispatches every ``ready`` node (worker) and every ``decomposing`` node
    with no placement recorded (planner). Nodes with a placement out —
    ``active`` / ``decomposing`` with an ``agent_ref`` — are **left untouched**:
    their agent and scope persist across an orchestrator restart, and the agents
    service owns their liveness.
    """
    out: list[DispatchIntent] = []
    for row in dao.query_all("SELECT id FROM tasks WHERE state = 'ready'"):
        out.append((row["id"], "worker"))
    for row in dao.query_all(
        "SELECT id FROM tasks WHERE state = 'decomposing' "
        "AND agent_ref IS NULL"
    ):
        out.append((row["id"], "planner"))
    return out
