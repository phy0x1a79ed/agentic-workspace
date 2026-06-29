"""The dispatch seam — the one place the orchestrator hands work to the agents
service, kept injectable so T3 can swap the live call in without touching the
kernel.

Two load-bearing rules from the design:

* **Enqueue, never deep-call.** A privileged-op handler mutates the plan,
  recomputes the frontier, and *enqueues* the resulting dispatch intents — it
  returns its reply without awaiting ``place_on_task``. The actual placement
  runs later on a single background drain task, so a slow agents service never
  stalls a handler and the dispatch order is serialized (no double-dispatch).
* **Flip records the real placement.** A node only records its placement
  (``ready`` → ``active`` for a worker, or the ``agent_ref`` stamped onto a
  ``decomposing`` node for a planner) *after* ``place_on_task`` returns, writing
  back the ``agent_ref`` / ``scope`` / ``placement_token`` it reported. So
  ``active`` (and ``decomposing`` with an ``agent_ref``) always imply a real
  placement — crash-safe for boot ``reconcile``.

The placement preparation and the post-placement DB flip are synchronous shared
helpers; only awaiting the seam differs between the live (async network) path
and the test (sync stub) path. Tests call :func:`configure` with ``sync=True``
and a synchronous stub, so the whole plan→dispatch→flip flow is exercised with
no event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from awm import gatewayclient
from awm.orchestrator.dao import OUT_STATE, REST_MODE, OrchestratorDAO

log = logging.getLogger("awm.orchestrator.dispatch")

DispatchIntent = tuple[str, str]  # (task_id, mode); mode in REST_MODE.values()

# Module state. Configured once at boot via ``configure``/``start_drain_loop``.
_place_fn: Callable[[dict], Any] = None  # type: ignore[assignment]
_sync: bool = False
_loop: asyncio.AbstractEventLoop | None = None
_queue: "asyncio.Queue[DispatchIntent] | None" = None
_drain_task: asyncio.Task | None = None
# Task ids currently queued-or-being-placed; dedups rapid re-enqueues so a
# single ``ready`` node is never placed twice. Touched only on the loop thread
# (live) or the calling thread (sync tests).
_inflight: set[str] = set()


# ---------------------------------------------------------------------------
# The seam — default = a real gateway RPC to the agents service (Contract A)
# ---------------------------------------------------------------------------


async def _default_place(payload: dict) -> dict:
    """Live placement: call ``agents.place_on_task`` over the gateway.

    Contract A — returns ``{agent_ref, scope, placement_token}``. Stubbed in
    every T1 test; wired for real at T3 integration.
    """
    result = await gatewayclient.call("agents", "place_on_task", payload)
    return result or {}


_place_fn = _default_place


def configure(
    *,
    place_fn: Callable[[dict], Any] | None = None,
    sync: bool | None = None,
) -> None:
    """Override the placement seam and/or dispatch mode (tests).

    ``place_fn`` replaces the agents-service call — a stub returning synthetic
    ``{agent_ref, scope, placement_token}``. ``sync=True`` makes :func:`enqueue`
    place inline (no event loop) so synchronous tests can drive the full flow.
    """
    global _place_fn, _sync
    if place_fn is not None:
        _place_fn = place_fn
    if sync is not None:
        _sync = sync


def reset() -> None:
    """Reset all dispatch state to live defaults (test teardown / fixtures)."""
    global _place_fn, _sync, _loop, _queue, _drain_task, _inflight
    _place_fn = _default_place
    _sync = False
    _loop = None
    _queue = None
    _drain_task = None
    _inflight = set()


# ---------------------------------------------------------------------------
# Scope-slug minting + placement payload
# ---------------------------------------------------------------------------


def _mint_workspace_slug(task: dict) -> str:
    """The workspace-service unit slug for a task's placement.

    Reuses an existing slug when present — so a planner placed on a task that
    just failed reuses the worker's retained unit and can read its partial work
    — and otherwise mints ``orch-<task-short-id>``.
    """
    return task["workspace_slug"] or f"orch-{task['id'][:8]}"


def _build_payload(dao: OrchestratorDAO, task: dict, mode: str,
                   unit_slug: str) -> dict:
    """Assemble the Contract-A ``place_on_task`` payload for a task.

    The agents runtime (``place_on_task``) reads ``unit_slug`` for the workspace
    unit and treats ``contracts_in`` / ``contracts_out`` as plain contract-NAME
    strings (it keys ``staged`` and ``orch.deliver(contract=...)`` off them).
    Delivered dependency payloads are materialized read-only via ``prereadings``
    (``[{name, path}]``). The brief is mode-specific:

    * ``plan`` — the goal; ``contracts_out`` is empty (the agents side defaults
      the reserved ``"plan"`` deliverable), and the real produced-contract names
      ride ``brief["produces"]`` so the plan agent knows its target WITHOUT
      delivering a real contract during ``planning`` (which would skip verify +
      work).
    * ``worker`` — the goal + the real ``contracts_out`` it must stage.
    * ``verify`` — the staged ``plan_ref`` and the objective (``contracts_out``)
      to check the plan against; the verifier needs no filesystem, so no
      ``contracts_in`` / ``prereadings`` are materialized for it.
    * ``planner`` — the latest failure reason (``review_reason``) so a re-planner
      knows why review was triggered, or ``reason="initial"`` when this is a
      fresh task's first specification (keyed off the ``created`` attempt memory).
    """
    produced = [c["name"] for c in dao.list_contracts_by_producer(task["id"])]
    incoming = dao.list_incoming_edges(task["id"])
    contracts_in = [e["name"] for e in incoming]
    # Delivered dependency payloads become the worker's read-only pre-readings.
    prereadings = [
        {"name": e["name"], "path": e["payload_ref"]}
        for e in incoming if e["payload_ref"]
    ]
    brief: dict[str, Any] = {"goal": task["goal"], "mode": mode}

    if mode == "plan":
        # The plan leg stages the reserved "plan" deliverable, NOT the real
        # contracts. Send no contracts_out (agents defaults to ["plan"]) and
        # carry the real produced names in the brief as the planning target.
        contracts_out: list[str] = []
        brief["produces"] = produced
    else:
        contracts_out = produced

    if mode == "verify":
        brief["plan_ref"] = task["plan_ref"]
        contracts_in = []  # the verifier is fs-less; objective rides contracts_out
        prereadings = []
    elif mode == "planner":
        mems = dao.list_attempt_memories(task["id"])
        last = mems[-1] if mems else None
        if last is not None and last["reason_type"] == "initial":
            brief["reason"] = "initial"
        elif last is not None:
            brief["review_reason"] = {
                "reason_type": last["reason_type"],
                "reason_text": last["reason_text"],
                "partial_ref": last["payload_ref"],
            }
    payload = {
        "task_id": task["id"],
        "unit_slug": unit_slug,
        "brief": json.dumps(brief),
        "contracts_in": contracts_in,
        "contracts_out": contracts_out,
        "prereadings": prereadings,
        "mode": mode,
    }
    # An attended node defaults to the claude harness (interactive, attachable
    # terminal + a capable model); unattended placements stay on the agents
    # service's own default (opencode / DSv4-free). Pure data — place_on_task
    # reads ``harness`` when present, else picks its default. The ``attached``
    # intent is ALSO threaded as data so the agents-side supervisor freezes
    # (no nag / no budget burn / no force-fail) on a born-attended node — a
    # human-driven drop-in idles politely until detached. Without this the flag
    # is hardcoded ``False`` at birth and the supervisor runs away (P2).
    if task["attached"]:
        payload["harness"] = "claude"
        payload["attached"] = True
    # The task's attached git scopes — linked into the unit under repos/<name>
    # on every (re)dispatch (the workspace symlink is idempotent). Read from the
    # first-class task_scopes relation as the workspace ``repos`` shape.
    repos = [{"name": r["name"], "project": r["project"], "scope": r["scope"]}
             for r in dao.list_task_scopes(task["id"])]
    if repos:
        payload["repos"] = repos
    return payload


# ---------------------------------------------------------------------------
# Prepare (guard + payload) and apply (the resting -> placement-out flip)
# ---------------------------------------------------------------------------


def _prepare(task_id: str, mode: str) -> dict | None:
    """Re-read the task and, if it is still in the resting state that wants
    ``mode``, build its placement payload. Returns ``None`` when the node has
    already moved on (a stale enqueue) so the drain simply skips it.

    Resting is read straight off the state via :data:`REST_MODE` — each resting
    state wants exactly one mode (``ready``→plan, ``plan_delivered``→verify,
    ``plan_approved``→worker, ``decompose_pending``→planner). Once a placement is
    live the node is in an out-state, so a stale re-enqueue is skipped.
    """
    dao = OrchestratorDAO()
    task = dao.get_task(task_id)
    if task is None:
        return None
    if REST_MODE.get(task["state"]) != mode:
        log.debug("orchestrator: skip stale dispatch %s (mode=%s, state=%s)",
                  task_id, mode, task["state"])
        return None
    return _build_payload(dao, task, mode, _mint_workspace_slug(task))


def _apply_placement(task_id: str, mode: str, result: dict) -> None:
    """Flip the resting node to its placement-out state (per :data:`OUT_STATE`),
    recording the agent placement the seam reported. ``agent_ref`` is pure data
    — it is set here, on the out-state, and read nowhere to infer position."""
    new_state = OUT_STATE[mode]
    dao = OrchestratorDAO()
    dao.update_task(
        task_id,
        state=new_state,
        mode=mode,
        agent_ref=(result or {}).get("agent_ref"),
        workspace_slug=(result or {}).get("unit_slug")
        or (result or {}).get("workspace")  # tolerate stub / legacy seam keys
        or (result or {}).get("scope")
        or _mint_workspace_slug(
            dao.get_task(task_id) or {"id": task_id, "workspace_slug": None}),
        placement_token=(result or {}).get("placement_token"),
    )


def _process_sync(intent: DispatchIntent) -> None:
    task_id, mode = intent
    payload = _prepare(task_id, mode)
    if payload is None:
        return
    result = _place_fn(payload)  # synchronous stub in test mode
    _apply_placement(task_id, mode, result)


async def _process_async(intent: DispatchIntent) -> None:
    task_id, mode = intent
    payload = _prepare(task_id, mode)
    if payload is None:
        return
    result = _place_fn(payload)
    if asyncio.iscoroutine(result) or asyncio.isfuture(result):
        result = await result
    _apply_placement(task_id, mode, result)


# ---------------------------------------------------------------------------
# Public enqueue + the background drain loop
# ---------------------------------------------------------------------------


def enqueue(intents: list[DispatchIntent]) -> None:
    """Queue dispatch intents for placement (the only seam the ops handlers use).

    Live: each intent is scheduled thread-safely onto the drain queue (handlers
    run in worker threads). Sync test mode: each intent is placed inline. In
    both, an in-flight set dedups a task that is already queued/being-placed.
    """
    for intent in intents:
        if _sync:
            tid = intent[0]
            if tid in _inflight:
                continue
            _inflight.add(tid)
            try:
                _process_sync(intent)
            finally:
                _inflight.discard(tid)
        else:
            if _loop is None:
                log.warning("orchestrator: dispatch loop not started; "
                            "dropping intent %s", intent)
                continue
            _loop.call_soon_threadsafe(_loop_enqueue, intent)


def _loop_enqueue(intent: DispatchIntent) -> None:
    """Add an intent to the drain queue — runs on the loop thread (so the
    in-flight set is only ever touched there)."""
    tid = intent[0]
    if tid in _inflight or _queue is None:
        return
    _inflight.add(tid)
    _queue.put_nowait(intent)


async def _drain() -> None:
    """Serially place every queued intent. Single task → no double-dispatch."""
    assert _queue is not None
    while True:
        intent = await _queue.get()
        try:
            await _process_async(intent)
        except Exception:  # noqa: BLE001 — never let one bad placement kill the loop
            log.exception("orchestrator: dispatch failed for %s", intent)
        finally:
            _inflight.discard(intent[0])
            _queue.task_done()


def start_drain_loop() -> None:
    """Start the background drain task. Called from ``on_start`` (in the event
    loop), so the running loop is captured for thread-safe enqueues."""
    global _loop, _queue, _drain_task
    _loop = asyncio.get_running_loop()
    _queue = asyncio.Queue()
    _drain_task = asyncio.create_task(_drain())
    log.info("orchestrator: dispatch drain loop started")
