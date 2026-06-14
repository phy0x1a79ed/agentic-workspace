"""The dispatch seam — the one place the orchestrator hands work to the agents
service, kept injectable so T3 can swap the live call in without touching the
kernel.

Two load-bearing rules from the design:

* **Enqueue, never deep-call.** A privileged-op handler mutates the plan,
  recomputes the frontier, and *enqueues* the resulting dispatch intents — it
  returns its reply without awaiting ``place_on_task``. The actual placement
  runs later on a single background drain task, so a slow agents service never
  stalls a handler and the dispatch order is serialized (no double-dispatch).
* **Flip records the real placement.** A node only leaves its resting state
  (``ready`` → ``active`` for a worker, ``failed`` → ``analyzing`` for a
  planner) *after* ``place_on_task`` returns, writing back the ``agent_ref`` /
  ``scope`` / ``placement_token`` it reported. So ``active``/``analyzing`` always
  imply a real placement — crash-safe for boot ``reconcile``.

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
from awm.orchestrator.dao import OrchestratorDAO

log = logging.getLogger("awm.orchestrator.dispatch")

DispatchIntent = tuple[str, str]  # (task_id, mode)

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


def _mint_scope_slug(task: dict) -> str:
    """The flat scope slug for a task's placement.

    Reuses an existing slug when present — so a planner placed on a ``failed``
    leaf reuses the worker's freed slug and can read its partial work — and
    otherwise mints ``orch-<task-short-id>``.
    """
    return task["scope_slug"] or f"orch-{task['id'][:8]}"


def _build_payload(dao: OrchestratorDAO, task: dict, mode: str,
                   scope_slug: str) -> dict:
    """Assemble the Contract-A ``place_on_task`` payload for a task.

    ``contracts_out`` are the contracts this task produces; ``contracts_in`` are
    the dependency contracts it consumes (with their delivered payload refs). A
    planner brief also carries the latest failure reason so the re-planner knows
    why review was triggered.
    """
    contracts_out = [
        {"name": c["name"], "spec": c["spec"]}
        for c in dao.list_contracts_by_producer(task["id"])
    ]
    contracts_in = [
        {"name": e["name"], "payload_ref": e["payload_ref"]}
        for e in dao.list_incoming_edges(task["id"])
    ]
    brief: dict[str, Any] = {"goal": task["goal"], "mode": mode}
    parent_agent_ref = None
    if mode == "planner":
        mems = dao.list_attempt_memories(task["id"])
        if mems:
            last = mems[-1]
            brief["review_reason"] = {
                "reason_type": last["reason_type"],
                "reason_text": last["reason_text"],
                "partial_ref": last["payload_ref"],
            }
    if task["parent_id"]:
        parent = dao.get_task(task["parent_id"])
        parent_agent_ref = parent["agent_ref"] if parent else None
    return {
        "task_id": task["id"],
        "project": task["project"],
        "scope_slug": scope_slug,
        "brief": json.dumps(brief),
        "contracts_in": contracts_in,
        "contracts_out": contracts_out,
        "parent_agent_ref": parent_agent_ref,
        "mode": mode,
    }


# ---------------------------------------------------------------------------
# Prepare (guard + payload) and apply (the resting -> placement-out flip)
# ---------------------------------------------------------------------------


def _prepare(task_id: str, mode: str) -> dict | None:
    """Re-read the task and, if it is still in the expected resting state for
    ``mode``, build its placement payload. Returns ``None`` when the node has
    already moved on (a stale enqueue) so the drain simply skips it.
    """
    dao = OrchestratorDAO()
    task = dao.get_task(task_id)
    if task is None:
        return None
    resting = "ready" if mode == "worker" else "failed"
    if task["state"] != resting:
        log.debug("orchestrator: skip stale dispatch %s (mode=%s, state=%s)",
                  task_id, mode, task["state"])
        return None
    return _build_payload(dao, task, mode, _mint_scope_slug(task))


def _apply_placement(task_id: str, mode: str, result: dict) -> None:
    """Flip the resting node to its placement-out state, recording the agent
    placement the seam reported. ``ready`` → ``active`` (worker) or ``failed`` →
    ``analyzing`` (planner)."""
    new_state = "active" if mode == "worker" else "analyzing"
    dao = OrchestratorDAO()
    dao.update_task(
        task_id,
        state=new_state,
        mode=mode,
        agent_ref=(result or {}).get("agent_ref"),
        scope_slug=(result or {}).get("scope") or _mint_scope_slug(
            dao.get_task(task_id) or {"id": task_id, "scope_slug": None}),
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
