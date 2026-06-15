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

The node lifecycle (see ``dao.STATES``) is symmetric — a *resting* state means
"this node needs a placement", the *active* state means "a placement is out":

    a worker is needed:   ready  -> active    -> delivered    (done)
    a planner is needed:  failed -> analyzing  -> discarded     (gave up)

``pending`` covers both "leaf waiting on its dependency contracts" and
"composite parent waiting on its children to deliver".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a hard import cost on the hot path
    from awm.orchestrator.dao import OrchestratorDAO

# A worker may fail transiently this many times (auto-retry to ``ready``)
# before the node rests in ``failed`` to await review.
TRANSIENT_RETRY_CAP = 3

DispatchIntent = tuple[str, str]  # (task_id, mode)


# ---------------------------------------------------------------------------
# Readiness — pending -> ready when every dependency contract is delivered
# ---------------------------------------------------------------------------


def recompute_readiness(dao: "OrchestratorDAO", task_id: str) -> bool:
    """Flip a ``pending`` leaf to ``ready`` when all its incoming dependency
    contracts are delivered. Returns True iff the node is now ``ready``.

    A node with no incoming dependency edges is ready immediately. Only
    ``pending`` nodes are advanced — a node that already left the resting state
    (``active``/``analyzing``/terminal) is never pulled back here.
    """
    task = dao.get_task(task_id)
    if task is None or task["state"] != "pending":
        return task is not None and task["state"] == "ready"
    incoming = dao.list_incoming_edges(task_id)
    if all(e["delivered_ts"] is not None for e in incoming):
        dao.update_task(task_id, state="ready")
        return True
    return False


def ready_frontier(dao: "OrchestratorDAO", project: str) -> list[dict]:
    """The leaves currently in ``ready`` for a project — the worker frontier."""
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


def route_failure(
    dao: "OrchestratorDAO", task_id: str, reason_type: str
) -> DispatchIntent | None:
    """Route a failed attempt to its next resting state; clear the placement.

    Returns the dispatch intent the routing produced (``(task_id, mode)``), or
    ``None`` when the node comes to rest with nothing to dispatch (terminal
    ``discarded``).

    * A failure on an ``analyzing`` node is a *planner* give-up: spend one unit
      of ``replan_budget``; ``→ failed`` (try review again) while budget remains,
      else ``→ discarded`` (terminal).
    * ``transient-error`` on a worker: auto-retry ``→ ready`` (bounded by
      ``retry_count`` ≤ :data:`TRANSIENT_RETRY_CAP`), never resting in
      ``failed``; once the cap is hit, rest in ``failed`` to await review.
    * ``needs-decomposition`` / ``contract-unsatisfiable``: rest in ``failed``
      to await a planner (review). ``fail`` is a *signal*, not a terminus.
    """
    task = dao.get_task(task_id)
    if task is None:
        return None

    # Clear the agent placement but KEEP ``scope_slug``: the scope (worktree)
    # persists when an agent detaches, and a planner reuses the failed leaf's
    # slug so it can read the partial work left behind. The slug is only freed
    # on terminal completion (``complete_task``) or give-up (``discarded``).
    cleared = dict(mode=None, agent_ref=None, placement_token=None)

    # Planner give-up (the failing node is mid-review).
    if task["state"] == "analyzing":
        budget = int(task["replan_budget"]) - 1
        if budget <= 0:
            dao.update_task(task_id, state="discarded", replan_budget=budget,
                            scope_slug=None, **cleared)
            return None
        dao.update_task(task_id, state="failed", replan_budget=budget, **cleared)
        return (task_id, "planner")

    # Worker transient error — auto-retry until the cap.
    if reason_type == "transient-error":
        n = int(task["retry_count"]) + 1
        if n <= TRANSIENT_RETRY_CAP:
            dao.update_task(task_id, state="ready", retry_count=n, **cleared)
            return (task_id, "worker")
        dao.update_task(task_id, state="failed", retry_count=n, **cleared)
        return (task_id, "planner")

    # Review-worthy reasons rest in ``failed`` awaiting a planner.
    dao.update_task(task_id, state="failed", **cleared)
    return (task_id, "planner")


# ---------------------------------------------------------------------------
# Completion + upward aggregation
# ---------------------------------------------------------------------------


def _deliver_producer_contracts(
    dao: "OrchestratorDAO", task_id: str, payload_ref: str
) -> list[DispatchIntent]:
    """Mark every not-yet-delivered contract this task produces as delivered,
    then advance each newly-ready consumer. Returns the worker dispatch intents.
    """
    out: list[DispatchIntent] = []
    for c in dao.list_contracts_by_producer(task_id):
        if c["delivered_ts"] is None:
            dao.mark_contract_delivered(c["id"], payload_ref)
            for consumer in dao.list_consumers_of_contract(c["id"]):
                if recompute_readiness(dao, consumer):
                    out.append((consumer, "worker"))
    return out


def complete_task(
    dao: "OrchestratorDAO", task_id: str
) -> bool:
    """Mark a task ``delivered`` and clear its placement, **iff** every contract
    it produces is delivered. Returns True when the task was completed.

    A task that produces no contracts is treated as complete on the caller's
    say-so (the deliver handler invokes this only after recording the delivery).
    """
    contracts = dao.list_contracts_by_producer(task_id)
    if any(c["delivered_ts"] is None for c in contracts):
        return False
    dao.update_task(task_id, state="delivered", mode=None, agent_ref=None,
                    scope_slug=None, placement_token=None)
    return True


def aggregate(
    dao: "OrchestratorDAO", parent_id: str | None
) -> list[DispatchIntent]:
    """Walk up the containment tree from ``parent_id``, completing composites.

    When every child of a composite is ``delivered`` the composite becomes
    ``delivered`` too (delivering its own outgoing contracts, which may unblock
    downstream consumers), and aggregation recurses to *its* parent. When the
    children settle but at least one is ``discarded`` (so the composite can
    never complete), the composite is pushed to ``failed`` to escalate for
    review. A composite with children still in progress halts the walk.
    """
    out: list[DispatchIntent] = []
    pid = parent_id
    while pid:
        parent = dao.get_task(pid)
        if parent is None:
            break
        children = dao.list_children(pid)
        if not children:
            break
        states = {c["state"] for c in children}
        if states <= {"delivered"}:
            dao.update_task(pid, state="delivered", mode=None)
            out += _deliver_producer_contracts(dao, pid, f"composite:{pid}")
            pid = parent["parent_id"]
            continue
        if states <= {"delivered", "discarded"} and "discarded" in states:
            # The composite can never complete — escalate for review.
            dao.update_task(pid, state="failed", mode=None)
            out.append((pid, "planner"))
            break
        break  # children still in progress
    return out


# ---------------------------------------------------------------------------
# Boot reconcile — re-dispatch the resting frontier, leave placements alone
# ---------------------------------------------------------------------------


def reconcile(dao: "OrchestratorDAO") -> list[DispatchIntent]:
    """Recompute the dispatch frontier at boot.

    Re-dispatches every ``ready`` leaf (worker) and every ``failed`` node
    awaiting review (planner). Nodes with a placement out — ``active`` /
    ``analyzing`` — are **left untouched**: their agent and scope persist across
    an orchestrator restart, and the agents service owns their liveness.
    """
    out: list[DispatchIntent] = []
    for row in dao.query_all("SELECT id FROM tasks WHERE state = 'ready'"):
        out.append((row["id"], "worker"))
    for row in dao.query_all("SELECT id FROM tasks WHERE state = 'failed'"):
        out.append((row["id"], "planner"))
    return out
