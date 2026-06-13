"""Hub adapter for the agents service.

Boots the agents service as a gateway-registered process: stands up its own
DB (agents.db), then runs the shared ServiceAdapter loop
(register → ready → serve → reconnect).

Run via start.sh (which the hub spawns and respawns):
    python -m awm.agents.hub_adapter
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.agents import dao
from awm.agents import agent_instances as ai
from awm.agents.agent_slash import dispatch as slash_dispatch
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
            "description": "List agent sessions (optionally filtered by project/scope/status).",
            "params": [
                {"name": "project", "type": "string", "required": False},
                {"name": "scope", "type": "string", "required": False},
                {"name": "status", "type": "string", "required": False},
            ],
        },
        {
            "name": "create_session",
            "description": "Spawn a new agent subprocess for (project, scope).",
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "scope", "type": "string", "required": True},
                {"name": "agent_cli", "type": "string", "required": False},
                {"name": "permission_mode", "type": "string", "required": False},
                {"name": "model", "type": "string", "required": False},
                {"name": "effort", "type": "string", "required": False},
            ],
        },
        {
            "name": "stop_session",
            "description": "Send SIGTERM to a session by id.",
            "params": [
                {"name": "session_id", "type": "integer", "required": True},
            ],
        },
        {
            "name": "kill_session",
            "description": "Send SIGKILL to a session by id.",
            "params": [
                {"name": "session_id", "type": "integer", "required": True},
            ],
        },
        {
            "name": "tail_log",
            "description": "Return the last N lines from a session's stderr log.",
            "params": [
                {"name": "session_id", "type": "integer", "required": True},
                {"name": "lines", "type": "integer", "required": False},
            ],
        },
        {
            "name": "slash_command",
            "description": "Dispatch a slash command to a running agent scope.",
            "params": [
                {"name": "scope_key", "type": "string", "required": True},
                {"name": "cmd", "type": "string", "required": True},
            ],
        },
        {
            "name": "enqueue_post",
            "description": "Enqueue a room post into a running agent's stdin.",
            "params": [
                {"name": "project", "type": "string", "required": True},
                {"name": "scope", "type": "string", "required": True},
                {"name": "room_id", "type": "string", "required": True},
                {"name": "author", "type": "string", "required": True},
                {"name": "body", "type": "string", "required": True},
            ],
        },
        {
            "name": "get_slash_catalog",
            "description": "Return the server slash-command catalog.",
        },
        {
            "name": "reconcile",
            "description": "Run startup reconciliation (close stale instance rows, seed resume).",
        },
    ],
    "emitters": [],
    "sessions": [],
}


async def _h_list_sessions(args: dict) -> dict:
    sessions = ai.list_sessions(
        project=args.get("project"),
        scope=args.get("scope"),
        status=args.get("status"),
    )
    return {
        "sessions": [_serialize_session(s) for s in sessions],
        "total": len(sessions),
    }


async def _h_create_session(args: dict) -> dict:
    session = await ai.create_session(
        project=args["project"],
        scope=args["scope"],
        agent_cli=args.get("agent_cli", "claude"),
        permission_mode=args.get("permission_mode", "default"),
        model=args.get("model"),
        effort=args.get("effort"),
    )
    return {
        "session_id": session.id,
        "project": session.project,
        "scope": session.scope,
        "pid": session.proc.pid,
        "status": session.status,
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
    ok = ai.enqueue_input(
        session,
        args["room_id"],
        args["author"],
        args["body"],
    )
    return {"enqueued": ok}


def _h_get_slash_catalog(args: dict) -> dict:
    from awm.agents.agent_slash import server_catalog
    return {"commands": server_catalog()}


def _h_reconcile(args: dict) -> dict:
    ai.reconcile_on_startup()
    return {"ok": True}


HANDLERS = {
    "list_sessions": _h_list_sessions,
    "create_session": _h_create_session,
    "stop_session": _h_stop_session,
    "kill_session": _h_kill_session,
    "tail_log": _h_tail_log,
    "slash_command": _h_slash_command,
    "enqueue_post": _h_enqueue_post,
    "get_slash_catalog": _h_get_slash_catalog,
    "reconcile": _h_reconcile,
}


def _on_start() -> None:
    dao.init()
    ai.reconcile_on_startup()
    ai.start_resume_driver()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "agents", API_MANIFEST, HANDLERS, on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
