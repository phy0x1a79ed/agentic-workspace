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
from awm.agents.driver_config import DRIVER_CONTRACT
from awm.agents import dao
from awm.agents import agent_instances as ai
from awm.agents import agent_transcript
from awm.agents import agent_bus
from awm.agents._time import iso_to_ms
from awm.agents.agent_slash import dispatch as slash_dispatch
from awm.agents.terminal_session import terminal_session
from awm.agents import fleet_spawn
from awm.agents import scope_spawn
from awm.agents import roster
from awm.agents.fleet_config import FLEET_CONTRACT
from awm.agents.models import (
    AgentSessionInfo,
    AgentSessionListResponse,
)

log = logging.getLogger("awm.agents.hub_adapter")

# Set in main(); used by _emit_feed to push roster deltas on the ``feed`` emitter
# the fleet page subscribes to at /svc/agents/emit/feed.
_adapter: ServiceAdapter | None = None


async def _emit_feed(payload: dict[str, Any]) -> None:
    if _adapter is not None and payload.get("type"):
        try:
            await _adapter.emit("feed", payload)
        except Exception:  # noqa: BLE001 — emit is best-effort signalling
            log.debug("feed emit failed", exc_info=True)


def _serialize_session(s: AgentSessionInfo) -> dict:
    return s.model_dump()


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "list_sessions",
            "tool": "agent_list",
            "description": (
                "List agent sessions newest-first (optionally filtered by "
                "scope/status). Pass limit for the most recent / last N "
                "sessions."
            ),
            "params": [
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
            "name": "spawn",
            "tool": "agent_launch",
            "surfaces": ["cli", "http"],
            "description": (
                "Launch a plain, idle interactive agent (claude/opencode) in a "
                "fresh detached tmux session at a directory — NOT a DAG "
                "placement. Used by the fleet page's new-agent overlay. Returns "
                "the tmux session name (the terminal's attach handle)."
            ),
            "params": [
                {"name": "cwd", "type": "string", "required": True,
                 "description": "Directory to launch in (a scope worktree or "
                                "any path)."},
                {"name": "harness", "type": "string", "required": False,
                 "description": "claude (default) | opencode."},
                {"name": "model", "type": "string", "required": False,
                 "description": "Model id (required for claude; opencode picks "
                                "its own)."},
                {"name": "effort", "type": "string", "required": False,
                 "description": "claude reasoning effort: low/medium/high/"
                                "xhigh/max."},
                {"name": "permission", "type": "string", "required": False,
                 "description": "default (default) | full "
                                "(--dangerously-skip-permissions)."},
            ],
        },
        {
            "name": "kill_tmux",
            "tool": "agent_kill_tmux",
            "surfaces": ["cli", "http"],
            "description": (
                "Dispose an ad-hoc tmux session by name (fleet two-tap dispose "
                "for a session with no agents-registry row)."
            ),
            "params": [
                {"name": "tmux_session", "type": "string", "required": True},
            ],
        },
        # -- Scope agent (the second, scope-based implementation). Launch an
        # interactive agent in a real git-scope worktree (resolve-or-provision),
        # NOT a DAG placement. Shares the tmux-spawn + roster + terminal leaves.
        {
            "name": "spawn_scoped",
            "tool": "agent_launch_scoped",
            "surfaces": ["cli", "http"],
            "description": (
                "Launch an idle interactive agent (claude/opencode) in an awm "
                "scope's worktree (projects/<project>/<scope>/), provisioning the "
                "scope first if it doesn't exist. The agent lands with the scope's "
                ".awm/context.md + workspace MCP. Returns the tmux session name "
                "(the terminal's attach handle) plus {project, scope}."
            ),
            "params": [
                {"name": "project", "type": "string", "required": True,
                 "description": "awm project the scope lives under."},
                {"name": "scope", "type": "string", "required": True,
                 "description": "Scope (worktree) name; created if absent."},
                {"name": "model", "type": "string", "required": False,
                 "description": "Model id (required for claude)."},
                {"name": "effort", "type": "string", "required": False,
                 "description": "claude reasoning effort: low/medium/high/"
                                "xhigh/max."},
                {"name": "harness", "type": "string", "required": False,
                 "description": "claude (default) | opencode."},
                {"name": "permission", "type": "string", "required": False,
                 "description": "default (default) | full."},
                {"name": "context", "type": "string", "required": False,
                 "description": "Seed .awm/context.md when provisioning a new "
                                "scope (ignored if the scope already exists)."},
            ],
        },
        {
            "name": "list_scopes",
            "tool": "agent_list_scopes",
            "surfaces": ["cli", "http"],
            "description": (
                "List awm scopes for the spawn picker: {scopes:[{project, scope, "
                "worktree, status, branch}]}. Passthrough to the scopes service."
            ),
            "params": [
                {"name": "project", "type": "string", "required": False,
                 "description": "Filter to one project."},
                {"name": "query", "type": "string", "required": False,
                 "description": "Fuzzy filter on scope name/metadata."},
                {"name": "status", "type": "string", "required": False,
                 "description": "Scope status filter (default: active)."},
                {"name": "limit", "type": "integer", "required": False},
            ],
        },
        # -- Fleet observe plane (absorbed from the retired notifications
        # service). Deliberately cli+http, NOT MCP: a placed DAG agent must not
        # be able to forge roster events or snoop the attention board. The
        # global Claude Code hook POSTs `report` fire-and-forget.
        {
            "name": "report",
            "tool": "agent_report",
            "surfaces": ["cli", "http"],
            "timeout": 30,
            "description": (
                "Ingest one normalized harness lifecycle event (from the global "
                "Claude Code hook / OpenCode plugin): turn_end, notification, "
                "user_prompt, error, session_start, session_end, spawned. Upserts "
                "the fleet roster row, classifies into needs-you/idle/error "
                "attention items, auto-resolves on user_prompt, folds token/EOOT "
                "usage on turn_end, and pushes a delta on the feed emitter."
            ),
            "params": [
                {"name": "harness", "type": "string", "required": True,
                 "description": "claude | opencode"},
                {"name": "event", "type": "string", "required": True,
                 "description": "turn_end | notification | user_prompt | error"
                                " | session_start | session_end | spawned"},
                {"name": "session_id", "type": "string", "required": True,
                 "description": "Harness session id (stable per session)."},
                {"name": "cwd", "type": "string",
                 "description": "Session working directory."},
                {"name": "transcript_path", "type": "string",
                 "description": "Claude transcript JSONL path (turn_end; read "
                                "server-side with retry)."},
                {"name": "message", "type": "string",
                 "description": "Harness notification/error message."},
                {"name": "last_message", "type": "string",
                 "description": "Final assistant message inline (OpenCode)."},
                {"name": "title", "type": "string",
                 "description": "Optional session title."},
                {"name": "reason", "type": "string",
                 "description": "session_end reason."},
                {"name": "tmux_session", "type": "string",
                 "description": "tmux session name (best-effort, when $TMUX is "
                                "set) — the browser terminal's attach handle."},
            ],
        },
        {
            "name": "list_fleet",
            "tool": "agent_fleet",
            "surfaces": ["cli", "http"],
            "description": "The machine-wide agent roster: every recently-live "
                           "Claude/OpenCode session with state, attach handle, "
                           "cost (EOOT), context size, and open attention items. "
                           "Carries fleet column config + spawn defaults.",
            "params": [
                {"name": "window_s", "type": "number",
                 "description": "Liveness window override (seconds)."},
            ],
        },
        {
            "name": "list_attention",
            "tool": "agent_attention",
            "surfaces": ["cli", "http"],
            "description": "Open attention items (+ the recently-resolved hour)"
                           " with their session rows. Runs the lazy stale-sweep.",
            "params": [
                {"name": "all", "type": "boolean",
                 "description": "Include resolved history (last 500)."},
            ],
        },
        {
            "name": "mark_seen",
            "tool": "agent_mark_seen",
            "surfaces": ["cli", "http"],
            "description": "Stamp an attention item as seen (page pushed it).",
            "params": [
                {"name": "id", "type": "string", "required": True},
            ],
        },
        {
            "name": "resolve",
            "tool": "agent_resolve",
            "surfaces": ["cli", "http"],
            "description": "Resolve one attention item by id, or every open item "
                           "for a session.",
            "params": [
                {"name": "id", "type": "string"},
                {"name": "session_id", "type": "string"},
            ],
        },
        {
            "name": "clear",
            "tool": "agent_clear",
            "surfaces": ["cli", "http"],
            "description": "Resolve every open attention item (board reset).",
            "params": [],
        },
        {
            "name": "fleet_stats",
            "tool": "agent_fleet_stats",
            "surfaces": ["cli", "http"],
            "description": "Open-item counts by kind + session counts by state.",
            "params": [],
        },
        # Fleet config read/write. The agents `config_get`/`config_set` slot is
        # already owned by the driver contract, so the fleet contract is reached
        # by these dedicated verbs (values also ride inline in list_fleet).
        {
            "name": "get_fleet_config",
            "tool": "agent_get_fleet_config",
            "surfaces": ["cli", "http"],
            "description": "The fleet-view config contract (columns, spawn "
                           "defaults, EOOT rate table) + its live values.",
            "params": [],
        },
        {
            "name": "save_fleet_config",
            "tool": "agent_save_fleet_config",
            "surfaces": ["cli", "http"],
            "description": "Persist a partial patch of the fleet-view config "
                           "(merged + validated). Echoes the saved contract.",
            "params": [
                {"name": "values", "type": "object", "required": True,
                 "description": "Partial FleetSettings patch to merge."},
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
                {"name": "scope", "type": "string", "required": True},
                {"name": "author", "type": "string", "required": True},
                {"name": "body", "type": "string", "required": True},
                {"name": "client_id", "type": "string", "required": False},
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
                {"name": "scope", "type": "string", "required": True},
                {"name": "author", "type": "string", "required": True},
                {"name": "body", "type": "string", "required": True},
            ],
        },
        {
            "name": "set_paused",
            "tool": "agent_set_paused",
            "description": (
                "Set a task's sticky paused flag by its unit slug. Pauses freeze "
                "the autonomous supervisor (it stays out even after the human "
                "disconnects) and survive a redispatch. Updates the live placement "
                "at once and mirrors durably to the orchestrator."
            ),
            "params": [
                {"name": "scope", "type": "string", "required": True},
                {"name": "paused", "type": "boolean", "required": True},
                {"name": "task_id", "type": "string", "required": False},
            ],
        },
        {
            "name": "attach",
            "tool": "agent_attach",
            "description": (
                "Explicit-interrupt attach a task by its unit slug: set the "
                "durable attached flag (the single source of truth — opening the "
                "chat/terminal does NOT attach) and clear its wants-steering flag. "
                "Freezes the autonomous supervisor at the next boundary. Mirrors "
                "durably so it survives a redispatch."
            ),
            "params": [
                {"name": "scope", "type": "string", "required": True},
                {"name": "task_id", "type": "string", "required": False},
            ],
        },
        {
            "name": "set_user_done",
            "tool": "agent_set_user_done",
            "description": (
                "Set/clear the USER's detach-consent bit for an attached task by "
                "its unit slug. When the agent's readiness bit is also set the "
                "node auto-detaches (no confirmation). Rejected if the task is not "
                "attached. There is no force-detach — the user only offers consent."
            ),
            "params": [
                {"name": "scope", "type": "string", "required": True},
                {"name": "done", "type": "boolean", "required": True},
                {"name": "task_id", "type": "string", "required": False},
            ],
        },
        {
            "name": "agent_subscribe",
            "tool": "agent_subscribe",
            "description": (
                "Snapshot an agent's act stream (the normalized transcript from "
                "agents.db) for a scope. Subscribe to an AGENT for its acts. "
                "Returns acts after an optional cursor (after_ts ISO + after_id). "
                "For a LIVE stream (backfill + push, de-duped by act id) open the "
                "'transcript' WS session instead."
            ),
            "params": [
                {"name": "scope", "type": "string", "required": True},
                {"name": "after_ts", "type": "string", "required": False},
                {"name": "after_id", "type": "string", "required": False},
                {"name": "limit", "type": "integer", "required": False},
            ],
        },
        {
            "name": "get_slash_catalog",
            "tool": "agent_slash_catalog",
            "description": "Return the server slash-command catalog.",
        },
        # -- Game-bot runtime (feat-gamebot). Recurring bounded play sessions
        # spawned autonomously off the events service's schedule.tick /
        # agent.wake emitters (see gamebot.run_listeners) or explicitly here.
        {
            "name": "game_spawn",
            "tool": "agent_game_spawn",
            "description": (
                "Spawn one bounded play session for a registered game (a "
                "committed games/<game>.toml in the agents service). Dedupes: "
                "if the game's bot is already playing this is a no-op. The "
                "same path the schedule.tick / agent.wake consumers use."
            ),
            "params": [
                {"name": "game", "type": "string", "required": True},
            ],
        },
        {
            "name": "game_list",
            "tool": "agent_game_list",
            "description": (
                "List the registered games (committed games/*.toml) with "
                "their live play status."
            ),
        },
        {
            "name": "park",
            "tool": "agent_park",
            "description": (
                "Game bot: end your play session cleanly. Call after saving "
                "the world and updating journal.md. Resolves your own session "
                "from your identity — no arguments needed (an operator may "
                "pass an explicit scope)."
            ),
            "params": [
                {"name": "scope", "type": "string", "required": False},
            ],
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
            "tool": "agent_edit_deliverable",
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
            "tool": "agent_indicate_done",
            "description": (
                "Mark your placed task's deliverable complete. The harness "
                "validates it at your next stop and advances the task; editing a "
                "deliverable afterwards clears this flag. Takes no arguments."
            ),
            "params": [],
        },
        {
            "name": "task_fail",
            "tool": "agent_task_fail",
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
        # steering handshake (worker/plan/planner): consent to detach, rescind,
        # or flag mid-run that you want a human.
        {
            "name": "request_detach",
            "tool": "agent_request_detach",
            "description": (
                "Consent to detach from your steering session, refreshing the "
                "objective record in one act: the payload IS the record (markdown "
                "— intent, constraints, what done looks like, non-goals). When the "
                "user's done bit is also set the node detaches automatically and "
                "you proceed autonomously. Only valid while attached; a new user "
                "message afterwards clears your readiness (re-request)."
            ),
            "params": [
                {"name": "objective", "type": "string", "required": True},
            ],
        },
        {
            "name": "rescind_detach",
            "tool": "agent_rescind_detach",
            "description": (
                "Withdraw your detach-consent bit (you are not ready after all). "
                "Only valid while attached; the objective record is left as-is."
            ),
            "params": [],
        },
        {
            "name": "request_steering",
            "tool": "agent_request_steering",
            "description": (
                "Flag mid-run that this node wants human steering (it appears in "
                "the attention strip). Valid while detached — it does NOT freeze "
                "you; continue with your best judgment or park that thread until a "
                "human attaches."
            ),
            "params": [],
        },
        # verify: a verdict on the staged plan (terminal on call).
        {
            "name": "approve_plan",
            "tool": "agent_approve_plan",
            "description": (
                "Verifier: the plan satisfies the objective. Advances the task "
                "from VERIFYING_PLAN to ACTIVE. Takes no arguments."
            ),
            "params": [],
        },
        {
            "name": "reject_plan",
            "tool": "agent_reject_plan",
            "description": (
                "Verifier: the plan does not satisfy the objective. Send it back "
                "to re-plan with a concise reason."
            ),
            "params": [
                {"name": "reason", "type": "string", "required": False},
            ],
        },
        # accept: an independent verdict on the worker's CLAIMED delivery
        # (terminal on call). The accept verifier runs the machine checks, then
        # accepts (promote the claim → real delivery, complete) or rejects (loop
        # back to the worker under a bounded budget).
        {
            "name": "accept_work",
            "tool": "agent_accept_work",
            "description": (
                "Accept verifier: the worker's claimed delivery passes every "
                "acceptance check and meets the objective. Promotes the claim "
                "into a real delivery and completes the task. Pass evidence "
                "describing what you ran and observed."
            ),
            "params": [
                {"name": "evidence", "type": "string", "required": False},
            ],
        },
        {
            "name": "reject_work",
            "tool": "agent_reject_work",
            "description": (
                "Accept verifier: the worker's claimed delivery fails an "
                "acceptance check or does not meet the objective. Loops the task "
                "back to the worker with your reason (bounded budget). Optionally "
                "pass a partial_ref preserving partial work."
            ),
            "params": [
                {"name": "reason", "type": "string", "required": False},
                {"name": "partial_ref", "type": "string", "required": False},
            ],
        },
        # planner: buffer a sub-DAG, committed when you indicate done.
        {
            "name": "add_subtask",
            "tool": "agent_add_subtask",
            "description": (
                "Planner: add a subtask to the pending sub-DAG (objective + the "
                "outputs it must produce, and a short title for the task view)."
            ),
            "params": [
                {"name": "id", "type": "string", "required": False},
                {"name": "objective", "type": "string", "required": False},
                {"name": "contracts_out", "type": "array", "required": False},
                {"name": "title", "type": "string", "required": False},
            ],
        },
        {
            "name": "add_dependency",
            "tool": "agent_add_dependency",
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
            "tool": "agent_define_contract",
            "description": "Planner: define a contract used by the pending sub-DAG.",
            "params": [
                {"name": "name", "type": "string", "required": True},
            ],
        },
        {
            "name": "search_tasks",
            "tool": "agent_search_tasks",
            "description": "Planner read: search existing tasks (reuse nodes).",
            "params": [
                {"name": "query", "type": "string", "required": False},
            ],
        },
        {
            "name": "search_contracts",
            "tool": "agent_search_contracts",
            "description": "Planner read: search existing contracts.",
            "params": [
                {"name": "query", "type": "string", "required": False},
            ],
        },
    ],
    "emitters": [
        {
            "topic": "feed",
            "description": "Live fleet roster/board deltas: one JSON frame per "
                           "raise/update/resolve/session event. The fleet page "
                           "treats each frame as a doorbell and re-fetches "
                           "list_fleet.",
        },
    ],
    # Opt-in config contract (the default-driver settings). Its presence is the
    # marker the gateway `config` aggregator keys off; the title + schema let
    # discovery skip an extra RPC. config_get/config_set are in HANDLERS but NOT
    # in functions[] — RPC-reachable for the aggregator, never projected as
    # per-service MCP/HTTP/CLI tools (the settings page is the surface).
    "config": DRIVER_CONTRACT.manifest_fragment(),
    "sessions": [
        {
            "kind": "transcript",
            "transport": "direct",
            "description": (
                "Live agent act stream for a scope. Open with init "
                "{scope, after_ts?, after_id?}: the server replays the "
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
                "init {scope} (or {session_id}), optional {cols, rows}: "
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
        "tool": f"agent_{op['name']}",
        "description": op["description"],
        "params": op["params"],
    }
    for op in admin_ops.ADMIN_OPS
)


async def _h_list_sessions(args: dict) -> dict:
    _limit = args.get("limit")
    sessions = ai.list_sessions(
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


async def _h_spawn(args: dict) -> dict:
    result = fleet_spawn.spawn_terminal(
        cwd=args.get("cwd") or "",
        harness=args.get("harness") or "claude",
        model=args.get("model"),
        effort=args.get("effort"),
        permission=args.get("permission") or "default",
    )
    # Register an immediate 'starting' placeholder in the fleet roster so the new
    # agent shows up the instant it's launched (keyed by tmux session name until
    # its real hook fires and adopts the row). The roster is now in-process (this
    # same service), so report locally instead of an RPC to the retired
    # notifications service. Soft-fail: a roster hiccup must never fail the spawn
    # — the row still arrives via the agent's own hook.
    try:
        await _handle_report({
            "harness": result["harness"],
            "event": "spawned",
            "session_id": result["tmux_session"],
            "tmux_session": result["tmux_session"],
            "cwd": result["cwd"],
        })
    except Exception:  # noqa: BLE001 — placeholder is best-effort
        pass
    return result


def _h_kill_tmux(args: dict) -> dict:
    killed = fleet_spawn.kill_tmux_session(args.get("tmux_session") or "")
    return {"ok": killed, "tmux_session": args.get("tmux_session")}


async def _h_spawn_scoped(args: dict) -> dict:
    """Scope agent launch: resolve-or-provision the worktree, then spawn + roster.

    Mirrors _h_spawn's best-effort 'starting' placeholder — the row still
    arrives via the agent's own hook if the local report hiccups."""
    result = await scope_spawn.spawn_scoped(
        project=args.get("project") or "",
        scope=args.get("scope") or "",
        model=args.get("model"),
        effort=args.get("effort"),
        harness=args.get("harness") or "claude",
        permission=args.get("permission") or "default",
        context=args.get("context"),
    )
    try:
        await _handle_report({
            "harness": result["harness"],
            "event": "spawned",
            "session_id": result["tmux_session"],
            "tmux_session": result["tmux_session"],
            "cwd": result["cwd"],
            "title": f"{result['project']}/{result['scope']}",
        })
    except Exception:  # noqa: BLE001 — placeholder is best-effort
        pass
    return result


async def _h_list_scopes(args: dict) -> dict:
    return await scope_spawn.list_scopes(
        project=args.get("project"),
        query=args.get("query"),
        status=args.get("status") or "active",
        limit=int(args.get("limit") or 100),
    )


# ---------------------------------------------------------------------------
# Fleet observe-plane handlers (roster + attention board). Each opens a fresh
# connection to the agents DB (roster funcs are sync over a bare connection).
# ---------------------------------------------------------------------------


async def _handle_report(args: dict) -> dict:
    conn = dao.connect()
    try:
        delta = await roster.handle_report(conn, args or {})
    finally:
        conn.close()
    await _emit_feed(delta)
    return delta


def _h_list_fleet(args: dict) -> dict:
    conn = dao.connect()
    try:
        w = args.get("window_s")
        return roster.list_fleet(
            conn, window_s=float(w) if w is not None else None)
    finally:
        conn.close()


def _h_list_attention(args: dict) -> dict:
    conn = dao.connect()
    try:
        return roster.list_items(conn, all=bool(args.get("all")))
    finally:
        conn.close()


def _h_mark_seen(args: dict) -> dict:
    conn = dao.connect()
    try:
        return roster.mark_seen(conn, args["id"])
    finally:
        conn.close()


async def _h_resolve(args: dict) -> dict:
    conn = dao.connect()
    try:
        ids = roster.resolve_items(
            conn, item_id=args.get("id"), session_id=args.get("session_id"),
            by="page")
    finally:
        conn.close()
    delta = {"ok": True, "type": "resolve" if ids else None, "ids": ids}
    await _emit_feed(delta)
    return delta


async def _h_clear(args: dict) -> dict:
    conn = dao.connect()
    try:
        out = roster.clear_all(conn)
    finally:
        conn.close()
    if out.get("resolved"):
        await _emit_feed({"type": "resolve", "ids": out["resolved"]})
    return out


def _h_fleet_stats(args: dict) -> dict:
    conn = dao.connect()
    try:
        return roster.stats(conn)
    finally:
        conn.close()


def _h_get_fleet_config(args: dict) -> dict:
    return FLEET_CONTRACT.get(args)


def _h_save_fleet_config(args: dict) -> dict:
    return FLEET_CONTRACT.set(args)


def _h_tail_log(args: dict) -> dict:
    text = ai.tail_log(int(args["session_id"]), lines=int(args.get("lines") or 200))
    return {"log": text}


async def _h_slash_command(args: dict) -> dict:
    scope_key = args["scope_key"]
    cmd = args["cmd"]
    handled, result = await slash_dispatch(scope_key, cmd)
    return {"handled": handled, "result": result}


async def _h_enqueue_post(args: dict) -> dict:
    # Async on purpose: enqueue_input's consent-clear side effect
    # (placement.note_user_post) schedules its durable mirror on the running
    # loop — a sync handler runs in a worker thread where there is none.
    session = ai.get_session_by_scope(args["scope"])
    if session is None:
        return {"enqueued": False, "reason": "no active session"}
    ok = ai.enqueue_input(session, args["author"], args["body"],
                          client_id=args.get("client_id"))
    return {"enqueued": ok}


async def _h_set_paused(args: dict) -> dict:
    """UI pause toggle (by unit slug): set the live + durable sticky paused flag."""
    from awm.agents import placement
    await placement.set_paused(
        args["scope"], bool(args.get("paused")), args.get("task_id"))
    return {"ok": True, "scope": args["scope"], "paused": bool(args.get("paused"))}


async def _h_attach(args: dict) -> dict:
    """UI explicit-interrupt attach (by unit slug): durable attach + clear the
    wants-steering flag; freezes the supervisor."""
    from awm.agents import placement
    return await placement.attach(args["scope"], args.get("task_id"))


async def _h_set_user_done(args: dict) -> dict:
    """UI detach-consent (by unit slug): set/clear the user's done bit; runs the
    handshake (auto-detach when the agent is also ready)."""
    from awm.agents import placement
    return await placement.set_user_done(
        args["scope"], bool(args.get("done")), args.get("task_id"))


async def _h_notify_agent(args: dict) -> dict:
    session = ai.get_session_by_scope(args["scope"])
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
        args["scope"],
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
    scope = init.get("scope")
    if not scope:
        # Best-effort error frame, then return (the bridge closes).
        try:
            bridge = await ctx.open_bridge()
            await bridge.send(json.dumps(
                {"type": "error", "message": "init requires scope"}))
            await bridge.close()
        except Exception:  # noqa: BLE001
            pass
        return

    after_ts = iso_to_ms(init.get("after_ts"))
    after_id = init.get("after_id")

    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    await agent_bus.attach_live(scope, queue)
    # Connecting the transcript WS is PURE VIEWING — it does NOT attach. Durable
    # attach is the single source of truth (the ``agent_attach`` verb / an explicit
    # interrupt sets it), so opening or closing this stream never touches the
    # supervisor or the attach flag. A human's message reaches the agent via
    # agent_post → enqueue_input (and THAT is the explicit interrupt that attaches,
    # via the UI's attach call).
    bridge = await ctx.open_bridge()
    try:
        # 1) Backfill from the cursor. Track the last act so the live stream
        #    can be deduped by id on the client (overlap is fine).
        backfill = agent_transcript.read_acts_after(
            scope, after_ts=after_ts, after_id=after_id)
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
        await agent_bus.detach_live(scope, queue)
        # Disconnect is pure viewing too — never detach here (durable attach
        # persists across a closed WS; the node stays attached + frozen).
        try:
            await bridge.close()
        except Exception:  # noqa: BLE001
            pass


def _h_get_slash_catalog(args: dict) -> dict:
    from awm.agents.agent_slash import server_catalog
    return {"commands": server_catalog()}


# ---------------------------------------------------------------------------
# Game-bot verbs (feat-gamebot)
# ---------------------------------------------------------------------------

async def _h_game_spawn(args: dict) -> dict:
    from awm.agents import gamebot
    try:
        return await gamebot.spawn_for_game(args["game"], source="verb")
    except gamebot.GamebotError as exc:
        return {"spawned": False, "error": str(exc)}


def _h_game_list(args: dict) -> dict:
    from awm.agents import gamebot
    games = []
    for game in gamebot.list_games():
        slug = gamebot.unit_slug_for(game)
        games.append({
            "game": game,
            "scope": slug,
            "playing": ai.get_session_by_scope(slug) is not None,
        })
    return {"games": games}


async def _h_park(args: dict, as_: str | None = None) -> dict:
    from awm.agents import gamebot
    return await gamebot.park(args, as_)


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


async def _h_stop_placement(args: dict) -> dict:
    """Orchestrator-called stop seam (manifest-omitted, reached via catch-all).

    Best-effort stop of a live placement by unit slug and/or task id — closes
    the placement row before killing the session so a dying agent's late report
    can't re-route consumers again. Idempotent."""
    from awm.agents import placement
    return await placement.stop_placement(args)


def _relay(fn_name: str, verb: str):
    """Build an async handler that dispatches to ``placement.<fn_name>``.

    Declares the second positional ``as_`` so the adapter forwards the caller
    identity (``X-Awm-As``): the placement B-op relays resolve their placement
    from it, so a placed agent never supplies a token.

    The placement tools all collapse onto the single ``agent`` MCP domain now
    (``mcp__awm__agent`` + ``verb``), so the per-mode tool restriction can no
    longer ride claude's ``--allowedTools`` (one domain tool, all verbs) — and
    never could under the default opencode harness, which ignores it. So gate
    server-side: ``ensure_verb_allowed`` rejects a verb the caller's placement
    mode does not permit (``task_fail`` always allowed). Defense-in-depth, not a
    trust boundary — the ``as_`` identity is spoofable on the loopback bus."""
    async def _handler(args: dict, as_: str | None = None) -> dict:
        from awm.agents import placement
        placement.ensure_verb_allowed(as_, verb)
        return await getattr(placement, fn_name)(args, as_)
    return _handler


HANDLERS = {
    "list_sessions": _h_list_sessions,
    "stop_session": _h_stop_session,
    "kill_session": _h_kill_session,
    "spawn": _h_spawn,
    "kill_tmux": _h_kill_tmux,
    # Scope agent (second implementation; cli+http).
    "spawn_scoped": _h_spawn_scoped,
    "list_scopes": _h_list_scopes,
    # Fleet observe plane (absorbed from notifications; cli+http, not MCP).
    "report": _handle_report,
    "list_fleet": _h_list_fleet,
    "list_attention": _h_list_attention,
    "mark_seen": _h_mark_seen,
    "resolve": _h_resolve,
    "clear": _h_clear,
    "fleet_stats": _h_fleet_stats,
    "get_fleet_config": _h_get_fleet_config,
    "save_fleet_config": _h_save_fleet_config,
    "tail_log": _h_tail_log,
    "slash_command": _h_slash_command,
    "enqueue_post": _h_enqueue_post,
    "set_paused": _h_set_paused,
    "attach": _h_attach,
    "set_user_done": _h_set_user_done,
    "notify_agent": _h_notify_agent,
    "agent_subscribe": _h_agent_subscribe,
    "get_slash_catalog": _h_get_slash_catalog,
    "reconcile": _h_reconcile,
    # Game-bot runtime (feat-gamebot).
    "game_spawn": _h_game_spawn,
    "game_list": _h_game_list,
    "park": _h_park,
    # Task-bounded placement (manifest-omitted ops reached via catch-all).
    "place_on_task": _h_place_on_task,
    "stop_placement": _h_stop_placement,
    # Placement tools (also in the manifest, MCP-visible to placed agents under
    # the `agent` domain; verb == the internal fn name == the HANDLERS key).
    "edit_deliverable": _relay("relay_edit_deliverable", "edit_deliverable"),
    "indicate_done": _relay("relay_indicate_done", "indicate_done"),
    "task_fail": _relay("relay_task_fail", "task_fail"),
    "request_detach": _relay("relay_request_detach", "request_detach"),
    "rescind_detach": _relay("relay_rescind_detach", "rescind_detach"),
    "request_steering": _relay("relay_request_steering", "request_steering"),
    "approve_plan": _relay("relay_approve_plan", "approve_plan"),
    "reject_plan": _relay("relay_reject_plan", "reject_plan"),
    "accept_work": _relay("relay_accept_work", "accept_work"),
    "reject_work": _relay("relay_reject_work", "reject_work"),
    "add_subtask": _relay("relay_add_subtask", "add_subtask"),
    "add_dependency": _relay("relay_add_dependency", "add_dependency"),
    "define_contract": _relay("relay_define_contract", "define_contract"),
    "search_tasks": _relay("relay_search_tasks", "search_tasks"),
    "search_contracts": _relay("relay_search_contracts", "search_contracts"),
}


def _admin_relay(op_name: str):
    """Build the gated relay handler for one admin op (forwards the caller
    identity ``as_`` so ``placement.relay_admin`` resolves + attach-gates it).

    Two gates stack: ``ensure_verb_allowed`` (mode permits this admin verb) then
    ``relay_admin``'s own live attach-gate (a human is attached right now)."""
    async def _handler(args: dict, as_: str | None = None) -> dict:
        from awm.agents import placement
        placement.ensure_verb_allowed(as_, op_name)
        return await placement.relay_admin(op_name, args, as_)
    return _handler


# One gated handler per admin op, generated from the registry (single source).
for _op in admin_ops.ADMIN_OPS:
    HANDLERS[_op["name"]] = _admin_relay(_op["name"])

# Config contract handlers (config_get/config_set), reached by the gateway
# aggregator over RPC. Not in the manifest functions[] → not projected as tools.
HANDLERS.update(DRIVER_CONTRACT.handlers())


async def _on_start() -> None:
    dao.init()
    # Boot cleanup only — close stale instance rows. There is no agents-side
    # resume driver: the orchestrator owns re-dispatch of resting nodes, and a
    # dead placement is reported back via orch.fail (liveness).
    ai.reconcile_on_startup()
    # Long-session hardening (T5): start the stall watchdog — a ~60s sweep that
    # fails any live placement silent past AWM_PLACEMENT_STALL_S (typed transient,
    # so the orchestrator re-places it) and kills the hung session. Immune while
    # attached or paused. Runs inside the event loop (on_start is awaited there).
    from awm.agents import placement
    asyncio.create_task(placement.stall_watchdog_loop())


async def main() -> None:
    global _adapter
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from awm.agents import gamebot
    adapter = ServiceAdapter(
        "agents", API_MANIFEST, HANDLERS,
        session_handlers={
            "transcript": _transcript_session,
            "terminal": terminal_session,
        },
        on_start=_on_start,
    )
    _adapter = adapter
    # The gamebot wake fabric runs as a sibling task (the events-service
    # Scheduler pattern): never-die consumer loops over the events service's
    # schedule.tick + agent.wake emitters → spawn_for_game. Inert (backoff
    # retry) while no events service is on the gateway.
    await asyncio.gather(adapter.run(), gamebot.run_listeners())


if __name__ == "__main__":
    asyncio.run(main())
