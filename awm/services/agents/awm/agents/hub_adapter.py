"""Hub adapter for the agents service.

Boots the agents service as a gateway-registered process: stands up its own
DB (agents.db), then runs the shared ServiceAdapter loop
(register → ready → serve → reconnect).

Run via start.sh (which the hub spawns and respawns):
    python -m awm.agents.hub_adapter
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.gatewayclient.adapter import SessionContext
from awm.agents import admin_ops
from awm.agents import dao
from awm.agents import agent_instances as ai
from awm.agents import agent_transcript
from awm.agents import agent_bus
from awm.agents._time import iso_to_ms
from awm.agents.agent_slash import dispatch as slash_dispatch
from awm.agents.terminal_session import terminal_session
from awm.agents.models import (
    AgentSessionInfo,
    AgentSessionListResponse,
)

log = logging.getLogger("awm.agents.hub_adapter")


def _serialize_session(s: AgentSessionInfo) -> dict:
    return s.model_dump()


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "list_sessions",
            "tool": "agent_list",
            "description": (
                "List agent sessions newest-first (optionally filtered by "
                "project/scope/status). Pass limit for the most recent / last N "
                "sessions."
            ),
            "params": [
                {"name": "project", "type": "string", "required": False},
                {"name": "scope", "type": "string", "required": False},
                {"name": "status", "type": "string", "required": False},
                {"name": "limit", "type": "integer", "required": False,
                 "description": "Return only the most recent N sessions (newest first)."},
            ],
        },
        {
            "name": "stop_session",
            "tool": "agent_stop",
            "description": "Send SIGTERM to a session by id.",
            "params": [
                {"name": "session_id", "type": "integer", "required": True},
            ],
        },
        {
            "name": "kill_session",
            "tool": "agent_kill",
            "description": "Send SIGKILL to a session by id.",
            "params": [
                {"name": "session_id", "type": "integer", "required": True},
            ],
        },
        {
            "name": "tail_log",
            "tool": "agent_log",
            "description": (
                "Tail an agent session's log: return the last N lines (most "
                "recent output) from its stderr log, by session id."
            ),
            "params": [
                {"name": "session_id", "type": "integer", "required": True},
                {"name": "lines", "type": "integer", "required": False},
            ],
        },
        {
            "name": "slash_command",
            "tool": "agent_slash",
            "description": "Dispatch a slash command to a running agent scope.",
            "params": [
                {"name": "scope_key", "type": "string", "required": True},
                {"name": "cmd", "type": "string", "required": True},
            ],
        },
        {
            "name": "enqueue_post",
            "tool": "agent_post",
            "description": (
                "Enqueue a scope-channel post into a running agent's stdin. "
                "PASSIVE channel: the agent reads it at the next turn boundary."
            ),
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "scope", "type": "string", "required": True},
                {"name": "author", "type": "string", "required": True},
                {"name": "body", "type": "string", "required": True},
            ],
        },
        {
            "name": "notify_agent",
            "tool": "agent_notify",
            "description": (
                "FORCED-INTERRUPT channel: preempt a running agent's current "
                "turn with a notification (tmux harness sends ESC then pastes "
                "the body; headless falls back to a plain send). Use for "
                "operator/inter-agent messages that must not wait for the turn "
                "to finish. Contrast agent_post, which is passive/turn-aligned."
            ),
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "scope", "type": "string", "required": True},
                {"name": "author", "type": "string", "required": True},
                {"name": "body", "type": "string", "required": True},
            ],
        },
        {
            "name": "agent_subscribe",
            "tool": "agent_subscribe",
            "description": (
                "Snapshot an agent's act stream (the normalized transcript from "
                "agents.db) for (project, scope). Subscribe to an AGENT for its "
                "acts; message a SCOPE for conversation. Returns acts after an "
                "optional cursor (after_ts ISO + after_id). For a LIVE stream "
                "(backfill + push, de-duped by act id) open the 'transcript' WS "
                "session instead."
            ),
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "scope", "type": "string", "required": True},
                {"name": "after_ts", "type": "string", "required": False},
                {"name": "after_id", "type": "string", "required": False},
                {"name": "limit", "type": "integer", "required": False},
            ],
        },
        {
            "name": "get_slash_catalog",
            "tool": "slash_catalog",
            "description": "Return the server slash-command catalog.",
        },
        {
            "name": "reconcile",
            "tool": "agent_reconcile",
            "description": "Run startup reconciliation (close stale instance rows, seed resume).",
        },
        # -- Placement tools (contract D). MCP-visible (`tool` set) so a placed
        # agent sees them in its spawn-mcp config; the per-mode allowlist scopes
        # which it may actually call. A placed agent acts ONLY on its own task:
        # the handlers resolve the placement from the CALLER IDENTITY (the
        # per-placement AWM_AS → X-Awm-As → as_), so no token is ever passed —
        # then relay to the orchestrator as the RESOLVED refs.
        #
        # worker / plan: stage outputs, then indicate done.
        {
            "name": "edit_deliverable",
            "tool": "edit_deliverable",
            "description": (
                "Stage (replace) the content for one output contract of your "
                "placed task. Call as many times as needed; each call replaces "
                "the staged content. Calling it clears any prior 'done'."
            ),
            "params": [
                {"name": "contract", "type": "string", "required": True},
                {"name": "content", "type": "string", "required": True},
            ],
        },
        {
            "name": "indicate_done",
            "tool": "indicate_done",
            "description": (
                "Mark your placed task's deliverable complete. The harness "
                "validates it at your next stop and advances the task; editing a "
                "deliverable afterwards clears this flag. Takes no arguments."
            ),
            "params": [],
        },
        {
            "name": "task_fail",
            "tool": "task_fail",
            "description": (
                "Give up on your placed task. Call with a reason_type and "
                "reason_text, and optionally a partial_ref to preserve partial "
                "work."
            ),
            "params": [
                {"name": "reason_type", "type": "string", "required": False},
                {"name": "reason_text", "type": "string", "required": False},
                {"name": "partial_ref", "type": "string", "required": False},
            ],
        },
        # verify: a verdict on the staged plan (terminal on call).
        {
            "name": "approve_plan",
            "tool": "approve_plan",
            "description": (
                "Verifier: the plan satisfies the objective. Advances the task "
                "from VERIFYING_PLAN to ACTIVE. Takes no arguments."
            ),
            "params": [],
        },
        {
            "name": "reject_plan",
            "tool": "reject_plan",
            "description": (
                "Verifier: the plan does not satisfy the objective. Send it back "
                "to re-plan with a concise reason."
            ),
            "params": [
                {"name": "reason", "type": "string", "required": False},
            ],
        },
        # planner: buffer a sub-DAG, committed when you indicate done.
        {
            "name": "add_subtask",
            "tool": "add_subtask",
            "description": (
                "Planner: add a subtask to the pending sub-DAG (objective + the "
                "outputs it must produce)."
            ),
            "params": [
                {"name": "id", "type": "string", "required": False},
                {"name": "objective", "type": "string", "required": False},
                {"name": "contracts_out", "type": "array", "required": False},
            ],
        },
        {
            "name": "add_dependency",
            "tool": "add_dependency",
            "description": (
                "Planner: add a dependency edge (from_id → to_id) to the pending "
                "sub-DAG — from_id is the upstream producer, to_id the downstream "
                "consumer. Every subtask must funnel into the task being "
                "decomposed. If from_id produces more than one contract, name the "
                "one to_id depends on via 'contract'."
            ),
            "params": [
                {"name": "from_id", "type": "string", "required": True},
                {"name": "to_id", "type": "string", "required": True},
                {"name": "contract", "type": "string", "required": False},
            ],
        },
        {
            "name": "define_contract",
            "tool": "define_contract",
            "description": "Planner: define a contract used by the pending sub-DAG.",
            "params": [
                {"name": "name", "type": "string", "required": True},
            ],
        },
        {
            "name": "search_tasks",
            "tool": "search_tasks",
            "description": "Planner read: search existing tasks (reuse nodes).",
            "params": [
                {"name": "query", "type": "string", "required": False},
            ],
        },
        {
            "name": "search_contracts",
            "tool": "search_contracts",
            "description": "Planner read: search existing contracts.",
            "params": [
                {"name": "query", "type": "string", "required": False},
            ],
        },
    ],
    "emitters": [],
    "sessions": [
        {
            "kind": "transcript",
            "transport": "direct",
            "description": (
                "Live agent act stream for (project, scope). Open with init "
                "{project, scope, after_ts?, after_id?}: the server replays the "
                "transcript from the cursor as 'backfill' acts, then streams new "
                "acts live as 'act' frames. Each act carries its agent_transcript "
                "id (uuid) so the client de-dupes the backfill/live overlap."
            ),
        },
        {
            "kind": "terminal",
            "transport": "direct",
            "description": (
                "Interactive tmux terminal for a claude agent. Open with "
                "init {project, scope} (or {session_id}), optional {cols, rows}: "
                "the server attaches a PTY-backed `tmux attach` to the agent's "
                "session and byte-relays it. Binary frames are raw terminal "
                "bytes (output downstream, keystrokes upstream); text frames are "
                "JSON control ({type:'resize',cols,rows}). Closing detaches the "
                "client — it never kills the agent. Errors for a non-tmux agent."
            ),
        },
    ],
}


# Attach-gated admin tools (DAG restructuring) are generated from the single
# admin_ops registry — MCP-visible (so an attended worker sees them), but each
# one's gate (placement.relay_admin) rejects it unless a human is attached.
# Editing admin_ops.ADMIN_OPS adds/removes/renames a command here automatically.
API_MANIFEST["functions"].extend(
    {
        "name": op["name"],
        "tool": op["name"],
        "description": op["description"],
        "params": op["params"],
    }
    for op in admin_ops.ADMIN_OPS
)


async def _h_list_sessions(args: dict) -> dict:
    _limit = args.get("limit")
    sessions = ai.list_sessions(
        project=args.get("project"),
        scope=args.get("scope"),
        status=args.get("status"),
        limit=int(_limit) if _limit is not None else None,
    )
    return {
        "sessions": [_serialize_session(s) for s in sessions],
        "total": len(sessions),
    }


async def _h_stop_session(args: dict) -> dict:
    info = await ai.stop_session(int(args["session_id"]))
    return _serialize_session(info)


async def _h_kill_session(args: dict) -> dict:
    info = await ai.kill_session(int(args["session_id"]))
    return _serialize_session(info)


def _h_tail_log(args: dict) -> dict:
    text = ai.tail_log(int(args["session_id"]), lines=int(args.get("lines") or 200))
    return {"log": text}


async def _h_slash_command(args: dict) -> dict:
    scope_key = args["scope_key"]
    cmd = args["cmd"]
    handled, result = await slash_dispatch(scope_key, cmd)
    return {"handled": handled, "result": result}


def _h_enqueue_post(args: dict) -> dict:
    project = args["project"]
    scope = args["scope"]
    session = ai.get_session_by_scope(project, scope)
    if session is None:
        return {"enqueued": False, "reason": "no active session"}
    ok = ai.enqueue_input(session, args["author"], args["body"])
    return {"enqueued": ok}


async def _h_notify_agent(args: dict) -> dict:
    project = args["project"]
    scope = args["scope"]
    session = ai.get_session_by_scope(project, scope)
    if session is None:
        return {"notified": False, "reason": "no active session"}
    ok = await ai.notify_agent(session, args["author"], args["body"])
    return {"notified": ok}


def _h_agent_subscribe(args: dict) -> dict:
    """One-shot cursored snapshot of an agent's acts (wire shape).

    Returns acts after the (after_ts, after_id) cursor in live wire shape
    (``{id, kind, body, meta, ts}``) ordered by (ts, id). The LIVE stream is the
    'transcript' WS session; this is the connect-time backfill / catch-up read.
    """
    after_ts = iso_to_ms(args.get("after_ts"))
    limit = args.get("limit")
    acts = agent_transcript.read_acts_after(
        args["project"], args["scope"],
        after_ts=after_ts,
        after_id=args.get("after_id"),
        limit=int(limit) if limit is not None else None,
    )
    return {"acts": acts, "total": len(acts)}


# ---------------------------------------------------------------------------
# Live transcript WS session (backfill from cursor → live push)
# ---------------------------------------------------------------------------

async def _transcript_session(ctx: SessionContext) -> None:
    """Drive a direct WS session streaming an agent's acts.

    On open: replay the transcript from the init cursor (``after_ts``/
    ``after_id``) as ``{"type":"backfill","acts":[...]}``, then stream new acts
    live as ``{"type":"act","act":{...}}`` from the in-process bus. Each act
    carries its ``agent_transcript`` id so the browser de-dupes the
    backfill/live overlap. On bus backpressure (high-volume partials) a
    ``{"type":"lagged"}`` sentinel is sent and the socket closes — the client
    reconnects with its last cursor and replays the gap.
    """
    init = ctx.init or {}
    project = init.get("project")
    scope = init.get("scope")
    if not project or not scope:
        # Best-effort error frame, then return (the bridge closes).
        try:
            bridge = await ctx.open_bridge()
            await bridge.send(json.dumps(
                {"type": "error", "message": "init requires project + scope"}))
            await bridge.close()
        except Exception:  # noqa: BLE001
            pass
        return

    after_ts = iso_to_ms(init.get("after_ts"))
    after_id = init.get("after_id")

    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    await agent_bus.attach_live(project, scope, queue)
    # A live transcript subscription IS the user-attached signal for a placement
    # (orthogonal to the task's state). Attach is PASSIVE (T2): it does not freeze
    # the autonomous supervisor — it only tells the orchestrator not to reclaim a
    # task a human is driving. A human's message reaches the agent via agent_post
    # → enqueue_input. No-op for a conversational scope. Best-effort — never block
    # the stream on it.
    try:
        from awm.agents import placement
        await placement.set_attached(project, scope, True)
    except Exception:  # noqa: BLE001
        pass
    bridge = await ctx.open_bridge()
    try:
        # 1) Backfill from the cursor. Track the last act so the live stream
        #    can be deduped by id on the client (overlap is fine).
        backfill = agent_transcript.read_acts_after(
            project, scope, after_ts=after_ts, after_id=after_id)
        await bridge.send(json.dumps({"type": "backfill", "acts": backfill}))

        # 2) Stream live acts published by the reader loop.
        while True:
            ev = await queue.get()
            if isinstance(ev, dict) and ev.get("type") == "lagged":
                try:
                    await bridge.send(json.dumps({"type": "lagged"}))
                except Exception:  # noqa: BLE001
                    pass
                break
            try:
                await bridge.send(json.dumps(ev))
            except Exception:  # noqa: BLE001
                break
    finally:
        await agent_bus.detach_live(project, scope, queue)
        try:
            from awm.agents import placement
            await placement.set_attached(project, scope, False)
        except Exception:  # noqa: BLE001
            pass
        try:
            await bridge.close()
        except Exception:  # noqa: BLE001
            pass


def _h_get_slash_catalog(args: dict) -> dict:
    from awm.agents.agent_slash import server_catalog
    return {"commands": server_catalog()}


def _h_reconcile(args: dict) -> dict:
    ai.reconcile_on_startup()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Orchestrator-facing + worker-tool handlers (task-bounded placement, T2)
# ---------------------------------------------------------------------------
# place_on_task (contract A) is orchestrator-called, NOT a worker tool, so it is
# omitted from API_MANIFEST and reached only via the gateway catch-all dispatch
# (keyed off HANDLERS). The task_* relays ARE in the manifest (MCP-visible to
# placed workers).

async def _h_place_on_task(args: dict) -> dict:
    from awm.agents import placement
    return await placement.place_on_task(args)


def _relay(fn_name: str):
    """Build an async handler that dispatches to ``placement.<fn_name>``.

    Declares the second positional ``as_`` so the adapter forwards the caller
    identity (``X-Awm-As``): the placement B-op relays resolve their placement
    from it, so a placed agent never supplies a token."""
    async def _handler(args: dict, as_: str | None = None) -> dict:
        from awm.agents import placement
        return await getattr(placement, fn_name)(args, as_)
    return _handler


HANDLERS = {
    "list_sessions": _h_list_sessions,
    "stop_session": _h_stop_session,
    "kill_session": _h_kill_session,
    "tail_log": _h_tail_log,
    "slash_command": _h_slash_command,
    "enqueue_post": _h_enqueue_post,
    "notify_agent": _h_notify_agent,
    "agent_subscribe": _h_agent_subscribe,
    "get_slash_catalog": _h_get_slash_catalog,
    "reconcile": _h_reconcile,
    # Task-bounded placement (manifest-omitted op reached via catch-all).
    "place_on_task": _h_place_on_task,
    # Placement tools (also in the manifest, MCP-visible to placed agents).
    "edit_deliverable": _relay("relay_edit_deliverable"),
    "indicate_done": _relay("relay_indicate_done"),
    "task_fail": _relay("relay_task_fail"),
    "approve_plan": _relay("relay_approve_plan"),
    "reject_plan": _relay("relay_reject_plan"),
    "add_subtask": _relay("relay_add_subtask"),
    "add_dependency": _relay("relay_add_dependency"),
    "define_contract": _relay("relay_define_contract"),
    "search_tasks": _relay("relay_search_tasks"),
    "search_contracts": _relay("relay_search_contracts"),
}


def _admin_relay(op_name: str):
    """Build the gated relay handler for one admin op (forwards the caller
    identity ``as_`` so ``placement.relay_admin`` resolves + attach-gates it)."""
    async def _handler(args: dict, as_: str | None = None) -> dict:
        from awm.agents import placement
        return await placement.relay_admin(op_name, args, as_)
    return _handler


# One gated handler per admin op, generated from the registry (single source).
for _op in admin_ops.ADMIN_OPS:
    HANDLERS[_op["name"]] = _admin_relay(_op["name"])


def _on_start() -> None:
    dao.init()
    # Boot cleanup only — close stale instance rows. There is no agents-side
    # resume driver: the orchestrator owns re-dispatch of resting nodes, and a
    # dead placement is reported back via orch.fail (liveness).
    ai.reconcile_on_startup()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "agents", API_MANIFEST, HANDLERS,
        session_handlers={
            "transcript": _transcript_session,
            "terminal": terminal_session,
        },
        on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
