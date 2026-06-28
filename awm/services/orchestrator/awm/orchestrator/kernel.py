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

The node lifecycle is a TRUE explicit state machine (see ``dao.STATES`` and the
``dao.REST_MODE`` / ``dao.OUT_STATE`` tables) — every distinct position is its
own named state. A *resting* state needs a placement; the matching *out* state
means one is live. ``agent_ref`` is pure data (which agent is placed) and is
never read to infer a position. The worker travels four placement legs:

    ready          -> planning        (a plan agent drafts the approach)
    plan_delivered -> verifying_plan   (a verifier checks the plan vs the goal)
    plan_approved  -> active           (a worker does the real work)
    decompose_pending -> decomposing   (a planner expands a too-big task)

There is **one** graph — the dependency DAG. A task that is too big does not
become a containment parent; it *decomposes* into an upstream sub-DAG and then
depends on that sub-DAG's terminal contracts (it rests ``blocked`` and re-runs
through the normal legs once they deliver). When a task gives up (``failed`` /
``abandoned``) its downstream **consumers** re-enter ``decompose_pending`` to
refine or replace it. Root has no consumer, so its give-up escalates to the
human.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from awm.orchestrator.dao import REST_MODE, STATES

if TYPE_CHECKING:  # avoid a hard import cost on the hot path
    from awm.orchestrator.dao import OrchestratorDAO

# A worker may fail transiently this many times (auto-retry to ``ready``)
# before the node rests in ``failed``.
TRANSIENT_RETRY_CAP = 3

# The states that count as "occupying" an attached scope: everything except the
# terminal ones. A scope held by a terminal task (completed / failed /
# abandoned) is free to re-attach elsewhere. Drives ``check_scope_free``.
_TERMINAL = {"completed", "failed", "abandoned"}
NON_TERMINAL = frozenset(s for s in STATES if s not in _TERMINAL)

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


def ready_frontier(dao: "OrchestratorDAO") -> list[dict]:
    """The nodes currently in ``ready`` — the global worker frontier."""
    return dao.list_tasks_by_state("ready")


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


def check_funnel(
    dao: "OrchestratorDAO", sink: str, nodes, *, conn=None
) -> bool:
    """True iff every task in ``nodes`` reaches ``sink`` along dependency edges.

    The decomposing task is the unique global sink of its own sub-DAG: every
    child must funnel into it. We walk the depends-on graph UPSTREAM from
    ``sink`` (``sink`` depends on its terminals, which depend on their
    producers, …) and require every node in ``nodes`` to be reached. Pass
    ``conn`` to walk the *uncommitted* edges of an open transaction. An empty
    ``nodes`` funnels vacuously (a pure re-spec with no children)."""
    targets = set(nodes)
    if not targets:
        return True
    seen: set[str] = set()
    stack = [sink]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(dao.depends_on(node, conn=conn))
    return targets <= seen


# ---------------------------------------------------------------------------
# Scope exclusivity — a scope is attached to ≤1 non-terminal task at a time
# ---------------------------------------------------------------------------


def check_scope_free(
    dao: "OrchestratorDAO", scope_ref: str, task_id: str, *, conn=None
) -> bool:
    """True iff ``scope_ref`` may be attached to ``task_id``.

    The hard rule: a git scope is attached to at most one **non-terminal** task
    at a time. So an attach is allowed iff no OTHER task that currently holds
    ``scope_ref`` is in a :data:`NON_TERMINAL` state (a scope held only by
    completed / failed / abandoned tasks is free to re-attach). Re-attaching to
    a task that already holds it is always fine (idempotent relink)."""
    for holder in dao.tasks_holding_scope(scope_ref, conn=conn):
        if holder["id"] != task_id and holder["state"] in NON_TERMINAL:
            return False
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


# Each agent-out state maps to (resting state, dispatch mode) for a transient
# auto-retry of the SAME leg — re-running that leg's agent, not re-planning.
_RETRY_REST: dict[str, tuple[str, str]] = {
    "planning": ("ready", "plan"),
    "verifying_plan": ("plan_delivered", "verify"),
    "active": ("plan_approved", "worker"),
}

# Placement-clearing patch shared by every routing transition. It does NOT touch
# ``workspace_slug`` (the unit persists for partial-work reuse) nor ``attached``
# (an orthogonal human flag); the slug is freed only on terminal completion or
# give-up.
_CLEAR_PLACEMENT = dict(mode=None, agent_ref=None, placement_token=None)


def abandon(dao: "OrchestratorDAO", task_id: str) -> list[DispatchIntent]:
    """Give up on a task: rest it ``abandoned`` (workspace freed, placement
    cleared) and route its downstream consumers to re-plan. Root has no
    consumers, so its give-up escalates (surfaced by ``orch_status``)."""
    dao.update_task(task_id, state="abandoned", workspace_slug=None,
                    **_CLEAR_PLACEMENT)
    return _route_consumers(dao, task_id)


def _enter_decompose_pending(
    dao: "OrchestratorDAO", task_id: str
) -> list[DispatchIntent]:
    """The single path a task takes into a decompose attempt.

    Budget is charged HERE, on entry, regardless of how the attempt later turns
    out (commit or give-up): spend one ``replan_budget`` unit and rest the task
    in ``decompose_pending`` (a planner is then dispatched). A task with no
    budget **cannot decompose** — it gives up via :func:`abandon`.
    """
    task = dao.get_task(task_id)
    if task is None:
        return []
    budget = int(task["replan_budget"])
    if budget <= 0:
        return abandon(dao, task_id)
    dao.update_task(task_id, state="decompose_pending",
                    replan_budget=budget - 1, **_CLEAR_PLACEMENT)
    return [(task_id, "planner")]


def _route_consumers(dao: "OrchestratorDAO", task_id: str) -> list[DispatchIntent]:
    """Route every downstream consumer of ``task_id`` into a decompose attempt.

    For each contract the failed/abandoned task produces, each consuming task
    re-enters ``decompose_pending`` (via :func:`_enter_decompose_pending`, which
    charges its budget and may itself abandon-and-cascade if the consumer is out
    of budget) to refine or replace the broken prerequisite. A consumer that is
    **root** cannot re-plan, so it is marked ``abandoned`` (surfaced as an
    escalation by ``orch_status``). A consumer already mid-decompose (or in a
    terminal state) is left alone.
    """
    out: list[DispatchIntent] = []
    for c in dao.list_contracts_by_producer(task_id):
        for consumer in dao.list_consumers_of_contract(c["id"]):
            ct = dao.get_task(consumer)
            if ct is None or ct["state"] in (
                "decompose_pending", "decomposing", "abandoned"
            ):
                continue
            if ct["is_root"]:
                dao.update_task(consumer, state="abandoned", **_CLEAR_PLACEMENT)
                continue
            out.extend(_enter_decompose_pending(dao, consumer))
    return out


def route_failure(
    dao: "OrchestratorDAO", task_id: str, reason_type: str
) -> list[DispatchIntent]:
    """Route a failed attempt to its next resting state; clear the placement.

    Returns the dispatch intents the routing produced.

    * A failure on a ``decomposing`` node is a *planner* give-up: a retry is a
      fresh decompose attempt, so it re-enters via
      :func:`_enter_decompose_pending` (charge + budget gate; abandons when
      exhausted and routes its consumers).
    * ``needs-decomposition`` (from any leg): the task is too big — it
      self-decomposes via :func:`_enter_decompose_pending`.
    * ``transient-error`` on a plan/verify/work leg: auto-retry the SAME leg
      (``planning``→``ready``, ``verifying_plan``→``plan_delivered``,
      ``active``→``plan_approved``), bounded by ``retry_count`` ≤
      :data:`TRANSIENT_RETRY_CAP`; once the cap is hit the node rests in
      ``failed`` and routes its consumers.
    * ``contract-unsatisfiable`` / impossible: the node rests in ``failed`` and
      routes its downstream consumers to re-plan it.
    """
    task = dao.get_task(task_id)
    if task is None:
        return []
    state = task["state"]

    # Planner give-up, or any leg signalling the task is too big → decompose.
    if state == "decomposing" or reason_type == "needs-decomposition":
        return _enter_decompose_pending(dao, task_id)

    retry_rest = _RETRY_REST.get(state)

    # Transient error — auto-retry the same leg until the cap.
    if reason_type == "transient-error" and retry_rest is not None:
        rest_state, mode = retry_rest
        n = int(task["retry_count"]) + 1
        if n <= TRANSIENT_RETRY_CAP:
            dao.update_task(task_id, state=rest_state, retry_count=n,
                            **_CLEAR_PLACEMENT)
            return [(task_id, mode)]
        dao.update_task(task_id, state="failed", retry_count=n,
                        **_CLEAR_PLACEMENT)
        return _route_consumers(dao, task_id)

    # Contract-unsatisfiable / impossible — rest in ``failed``; consumers replan.
    dao.update_task(task_id, state="failed", **_CLEAR_PLACEMENT)
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
                    workspace_slug=None, placement_token=None)
    return True


# ---------------------------------------------------------------------------
# Boot reconcile — re-dispatch the resting frontier, leave placements alone
# ---------------------------------------------------------------------------


def reconcile(dao: "OrchestratorDAO") -> list[DispatchIntent]:
    """Recompute the dispatch frontier at boot.

    Re-dispatches every node resting in a placement-needed state, mapping the
    state to its mode via :data:`dao.REST_MODE` (``ready``→plan,
    ``plan_delivered``→verify, ``plan_approved``→worker,
    ``decompose_pending``→planner). Nodes with a placement out (any out-state)
    are **left untouched**: their agent and workspace persist across an
    orchestrator restart, and the agents service owns their liveness.
    """
    out: list[DispatchIntent] = []
    rest_states = list(REST_MODE)
    placeholders = ", ".join("?" for _ in rest_states)
    for row in dao.query_all(
        f"SELECT id, state FROM tasks WHERE state IN ({placeholders}) "
        "ORDER BY created_at",
        tuple(rest_states),
    ):
        out.append((row["id"], REST_MODE[row["state"]]))
    return out
