"""Task-bounded placement — the runtime side of the orchestrator.

A **placement** is the assignment of one worker agent onto one task in a fresh
scope. The placement record IS the task-bound ``agent_instances`` row (``mode``
!= ``'conversational'``); the ``placement_token`` names it; ``agent_ref`` is the
stable placement identity; ``task_ref`` binds it to the orchestrator's task.
This module owns:

- ``place_on_task`` (contract A) — provision a scope, spawn a supervised worker
  via the EXISTING conversational spawn path, record the placement, deliver the
  brief.
- the worker-tool relays (contract D → contract B) — ``relay_deliver`` /
  ``relay_fail`` / ``relay_decompose``: resolve the token, relay to the matching
  orchestrator op as the RESOLVED refs (never worker-supplied), reclaim the scope
  only after the orchestrator acks (deliver/fail; never decompose).
- the supervision driver — ``on_turn_boundary``: a hard turn budget that keeps a
  human-less worker moving and force-fails gracefully (saving progress) at 0.

Everything here reuses ``agent_instances`` (spawn / transcript / scope-delivery /
stdin injection) rather than forking it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import awm.gatewayclient as gatewayclient
from awm.agents import agent_instances as ai
from awm.agents import orch_client
from awm.agents.agent_instances import TASK_TURN_BUDGET, TASK_WARN_REMAINING

log = logging.getLogger("awm.agents.placement")


# ---------------------------------------------------------------------------
# Identity minting
# ---------------------------------------------------------------------------

def _mint_agent_ref() -> str:
    return "agt-" + uuid.uuid4().hex


def _mint_placement_token() -> str:
    return "plt-" + uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Brief / kickoff rendering
# ---------------------------------------------------------------------------

def _fmt_contracts(label: str, contracts: list) -> str:
    if not contracts:
        return f"{label}: (none)"
    lines = [f"{label}:"]
    for c in contracts:
        lines.append(f"  - {c}")
    return "\n".join(lines)


def render_brief(*, task_id: str, mode: str, brief: str,
                 contracts_in: list, contracts_out: list,
                 placement_token: str) -> str:
    """Render the worktree ``.awm/context.md`` for a placed worker.

    Written by ``scope_create`` (the ``context`` arg) so the worker reads its
    task, its contracts, and HOW to terminate the moment it boots. The
    placement_token is the worker's capability: it can act only on its own task."""
    return (
        f"# Task placement: {task_id}\n\n"
        f"You are a **{mode}** placed on orchestrator task `{task_id}`.\n\n"
        f"## Brief\n\n{brief or '(no brief provided)'}\n\n"
        f"## Contracts\n\n"
        f"{_fmt_contracts('Inputs (contracts_in)', contracts_in)}\n\n"
        f"{_fmt_contracts('Outputs you must produce (contracts_out)', contracts_out)}\n\n"
        f"## How to finish\n\n"
        f"You are running unattended. End your task by calling exactly one of "
        f"these tools with your placement_token `{placement_token}`:\n\n"
        f"- `task_deliver(placement_token, contract, payload_ref)` — you produced "
        f"the required output; payload_ref is an artifact reference.\n"
        f"- `task_fail(placement_token, reason_type, reason_text, partial_ref?)` — "
        f"you cannot complete it; pass partial_ref to preserve partial work.\n"
        f"- `task_decompose(placement_token, children, edges, contracts)` — the "
        f"task is too big; hand back a sub-DAG (planner mode).\n\n"
        f"Do NOT wait idle: every turn you don't act counts against a hard "
        f"{TASK_TURN_BUDGET}-turn budget. When the budget runs low you will be "
        f"warned to checkpoint and `task_fail(partial_ref=…)`.\n"
    )


def _kickoff_text(*, task_id: str, placement_token: str) -> str:
    """First stdin message — wakes the worker into its turn loop."""
    return (
        f"You have been placed on task `{task_id}`. Read `.awm/context.md` for "
        f"your brief, contracts, and how to finish. Your placement_token is "
        f"`{placement_token}`. Begin now."
    )


# ---------------------------------------------------------------------------
# place_on_task (contract A) + scope provisioning (contract C)
# ---------------------------------------------------------------------------

async def place_on_task(args: dict) -> dict:
    """Drop a supervised worker onto a task in a fresh scope.

    Sequence: mint identity → ``scope_create`` (worktree + ``.awm/context.md``
    brief) BEFORE spawn → spawn live via the existing ``create_session`` path
    (``bypassPermissions`` so a human can attach) → record lineage on the row →
    kickoff via stdin. Returns ``{agent_ref, scope, placement_token}``.

    ``scope_create`` raises ``FileExistsError`` if a session is already active on
    the scope — that's one-agent-per-scope for free (planner-reuse is later)."""
    task_id = args["task_id"]
    project = args["project"]
    scope_slug = args["scope_slug"]
    brief = args.get("brief", "")
    contracts_in = args.get("contracts_in") or []
    contracts_out = args.get("contracts_out") or []
    mode = args.get("mode", "worker")

    agent_ref = _mint_agent_ref()
    placement_token = _mint_placement_token()
    context_md = render_brief(
        task_id=task_id, mode=mode, brief=brief,
        contracts_in=contracts_in, contracts_out=contracts_out,
        placement_token=placement_token,
    )

    # Contract C: provision the scope FIRST — the worktree must exist before the
    # spawn (create_session requires the workspace dir), and this is where the
    # brief+token land in .awm/context.md.
    await gatewayclient.call("scopes", "scope_create", {
        "project": project, "scope": scope_slug, "context": context_md,
    })

    # Spawn live via the EXISTING path so the human can attach via 'transcript'.
    session = await ai.create_session(
        project=project, scope=scope_slug,
        agent_cli="claude", permission_mode="bypassPermissions",
        mode=mode, task_ref=task_id, agent_ref=agent_ref,
        placement_token=placement_token,
    )

    # Kickoff via stdin (NOT scope_post — the scope-delivery loop seeds its
    # cursor at the newest post on connect, so a kickoff post could be swallowed).
    ai.enqueue_input(session, "orchestrator",
                     _kickoff_text(task_id=task_id, placement_token=placement_token))

    return {"agent_ref": agent_ref, "scope": scope_slug,
            "placement_token": placement_token}


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------

class PlacementError(Exception):
    """Raised when a worker-supplied placement_token is unknown or closed."""


def _resolve_open_placement(token: str | None) -> dict:
    """Resolve a placement_token to its OPEN placement row, else raise.

    Worker identity is the token only — never a worker-supplied task/agent ref.
    Rejects a missing token, an unknown token, and an already-closed placement
    (a terminal tool already fired or supervision force-failed it)."""
    if not token:
        raise PlacementError("missing placement_token")
    dao = ai._get_dao()
    row = dao.resolve_placement(token)
    if row is None:
        raise PlacementError("unknown placement_token")
    try:
        data = json.loads(row["data"] or "{}")
    except (TypeError, ValueError):
        data = {}
    if data.get("placement_outcome"):
        raise PlacementError("placement already closed")
    return row


# ---------------------------------------------------------------------------
# Scope reclaim + session teardown
# ---------------------------------------------------------------------------

async def _reclaim_scope(project: str, scope: str) -> None:
    """Retire the scope, RETAINING the worktree (audit + planner-reuse).

    ``scope_complete`` (not ``scope_delete``) so partial work + the transcript
    survive — progress is never destroyed by reclaim. Best-effort: a transient
    RPC failure is logged, not raised (the orchestrator already has the ack)."""
    try:
        await gatewayclient.call("scopes", "scope_complete", {
            "project": project, "scope": scope, "cleanup": False,
        })
    except Exception:  # noqa: BLE001
        log.warning("scope_complete failed for %s/%s", project, scope,
                    exc_info=True)


async def _retire_worker(instance_id: int) -> None:
    """Stop the worker subprocess (its task is done). Best-effort."""
    try:
        await ai.stop_session(instance_id)
    except Exception:  # noqa: BLE001
        log.warning("stop_session failed for instance %s", instance_id,
                    exc_info=True)


# ---------------------------------------------------------------------------
# Worker-tool relays (contract D → contract B)
# ---------------------------------------------------------------------------

async def relay_deliver(args: dict) -> dict:
    """Worker `task_deliver` → orchestrator `deliver` (then reclaim).

    No screening (orchestrator-only). Reclaim happens ONLY after the deliver ack
    returns — tearing the scope down first would lose the payload."""
    row = _resolve_open_placement(args.get("placement_token"))
    ack = await orch_client.deliver(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        contract=args.get("contract"), payload_ref=args.get("payload_ref"),
    )
    ai._get_dao().close_placement(row["id"], outcome="delivered")
    await _reclaim_scope(row["project"], row["scope"])
    await _retire_worker(row["id"])
    return {"ok": True, "outcome": "delivered", "ack": ack}


async def relay_fail(args: dict) -> dict:
    """Worker `task_fail` → orchestrator `fail` (then reclaim, retaining work)."""
    row = _resolve_open_placement(args.get("placement_token"))
    ack = await orch_client.fail(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        reason_type=args.get("reason_type"), reason_text=args.get("reason_text"),
        partial_ref=args.get("partial_ref"),
    )
    ai._get_dao().close_placement(row["id"], outcome="failed")
    await _reclaim_scope(row["project"], row["scope"])
    await _retire_worker(row["id"])
    return {"ok": True, "outcome": "failed", "ack": ack}


async def relay_decompose(args: dict) -> dict:
    """Worker `task_decompose` → orchestrator `decompose_commit`.

    Relay only — does NOT reclaim the scope: a downstream planner reuses the
    freed leaf worktree to see the partial work. The worker is retired (its
    planning turn is done)."""
    row = _resolve_open_placement(args.get("placement_token"))
    ack = await orch_client.decompose_commit(
        task_id=row["task_ref"], agent_ref=row["agent_ref"],
        children=args.get("children") or [], edges=args.get("edges") or [],
        contracts=args.get("contracts") or [],
    )
    ai._get_dao().close_placement(row["id"], outcome="decomposed")
    await _retire_worker(row["id"])
    return {"ok": True, "outcome": "decomposed", "ack": ack}


# ---------------------------------------------------------------------------
# Supervision driver (hard turn budget, save-progress)
# ---------------------------------------------------------------------------

def _continuation_prompt(task_id: str, placement_token: str,
                         remaining: int) -> str:
    """The per-turn nudge. Escalates to a save-progress warning near the cap."""
    if remaining <= TASK_WARN_REMAINING:
        return (
            f"[supervisor] {remaining} turn(s) left before task `{task_id}` is "
            f"force-failed. COMMIT partial work to the worktree now, register a "
            f"partial artifact, then call `task_deliver(...)` if you are done, or "
            f"`task_fail(placement_token=\"{placement_token}\", "
            f"reason_type=\"transient-error\", reason_text=\"...\", "
            f"partial_ref=\"<artifact>\")` to preserve it. Do not start new work."
        )
    return (
        f"[supervisor] Continue task `{task_id}` ({remaining} turns left). When "
        f"done call `task_deliver(...)`; if you are stuck, `task_fail(...)` and "
        f"`task_decompose(...)` are valid exits. Your placement_token is "
        f"`{placement_token}`."
    )


async def _force_fail(session) -> None:
    """Turn budget exhausted: fail to the orchestrator, retire (keep worktree)."""
    await orch_client.fail(
        task_id=session.task_ref, agent_ref=session.agent_ref,
        reason_type="transient-error",
        reason_text=f"turn budget ({TASK_TURN_BUDGET}) exhausted",
        partial_ref=None,
    )
    ai._get_dao().close_placement(session.id, outcome="failed")
    await _reclaim_scope(session.project, session.scope)
    await _retire_worker(session.id)


async def on_turn_boundary(session) -> None:
    """Drive one task-bound worker at an outer-loop turn boundary.

    Called from ``_reader_loop`` on the ``result`` event (one per turn, because
    claude ``--print`` stream-json runs one turn per stdin message then stops).
    The inner loop (many tool calls within a turn) is only OBSERVED; we act here.

    If the placement already closed (a terminal tool fired), do nothing — drive
    completion off the tool call, not the event. Otherwise decrement the hard
    budget (no extension, no refill) and inject the next prompt; at 0, force-fail
    while preserving progress."""
    token = getattr(session, "placement_token", None)
    if not token:
        return
    try:
        row = _resolve_open_placement(token)
    except PlacementError:
        return  # closed/unknown — nothing to drive
    task_id = row["task_ref"]

    session.turn_budget -= 1
    remaining = session.turn_budget
    if remaining <= 0:
        await _force_fail(session)
        return
    ai.enqueue_input(session, "supervisor",
                     _continuation_prompt(task_id, token, remaining))
