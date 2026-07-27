"""Hub adapter for the reflection service.

Boots the service as a gateway-registered process and runs the shared
:class:`awm.gatewayclient.ServiceAdapter` loop (register → ready → serve →
reconnect). The service owns no database, so there is no ``on_start``.

Surface: both verbs project onto MCP + CLI + HTTP — the MCP surface is the whole
point (an agent calls ``reflection(verb="compact", args={pane})`` on itself).

- ``send``    — paste any text/slash command into a tmux pane and submit it.
- ``compact`` — sugar for ``send`` with ``text="/compact"``.

This process does not share the caller's environment, so the target pane can't
be read directly here — but the calling agent never needs to know or pass it
either. The per-session ``awm-mcp`` proxy sits inside the caller's own tmux pane
and forwards ``$TMUX_PANE`` as a header; the gateway fills it in as the default
``pane`` before the call reaches this service. ``pane`` remains an accepted
argument only as a manual override (a human at a shell, or deliberately
targeting another pane). See ``awm.reflection.tmux_inject`` for the mechanics.

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.reflection.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.reflection import tmux_inject

log = logging.getLogger("awm.reflection.hub_adapter")


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "send",
            "tool": "reflection_send",
            "description": (
                "Paste a command/text into a terminal agent's own tmux pane and "
                "submit it (no Escape, so it QUEUES behind the current turn). Use "
                "for self-directed slash commands like /compact or /model opus. A "
                "submitted slash command is auto-trailed by a follow-up prompt "
                "(override with `followup`) so the session has a next turn once "
                "the command finishes — a bare command would otherwise go idle. "
                "The follow-up is DEFERRED (injected after the command completes, "
                "never queued behind it) so it can't run ahead of the command. "
                "Modal commands (/mcp, /status, /model with no arg, …) are refused "
                "— they trap input and freeze the session. "
                "Call with no `pane` — your own tmux pane is detected automatically."
            ),
            "timeout": 60,
            "params": [
                {"name": "text", "type": "string", "required": True,
                 "description": "The line to inject, e.g. '/compact' or '/model opus'."},
                {"name": "pane", "type": "string",
                 "description": "Advanced override: a specific tmux pane id to "
                                "target instead of your own (e.g. to drive a "
                                "different session). Leave unset for the normal case."},
                {"name": "enter", "type": "boolean",
                 "description": "Press Enter to submit after pasting (default true)."},
                {"name": "followup", "type": "string",
                 "description": "Prompt injected after a slash command completes to "
                                "keep the session alive (default 'Continue with what "
                                "you were doing.'). Ignored for plain prompts."},
                {"name": "delay_ms", "type": "integer",
                 "description": "Wait this many ms before injecting (default 0)."},
                {"name": "confirm", "type": "boolean",
                 "description": "Required to send destructive commands (/clear, /quit, /exit)."},
                {"name": "socket", "type": "string",
                 "description": "tmux socket path (tmux -S); defaults to the standard per-uid socket."},
            ],
        },
        {
            "name": "compact",
            "tool": "reflection_compact",
            "description": (
                "Compact your own conversation: injects /compact into your tmux "
                "pane; it queues and runs the instant the current turn ends. A "
                "resume prompt is then injected AFTER compaction completes (a "
                "detached watcher waits for it — the resume is never queued behind "
                "/compact, so it always lands on the freshly-compacted context) so "
                "the session resumes instead of going idle. Returns immediately "
                "with followup_deferred=true. Call with no arguments — your own "
                "tmux pane is detected automatically."
            ),
            "timeout": 60,
            "params": [
                {"name": "pane", "type": "string",
                 "description": "Advanced override: a specific tmux pane id to "
                                "target instead of your own. Leave unset for the "
                                "normal case."},
                {"name": "followup", "type": "string",
                 "description": "Prompt queued after /compact to resume work "
                                "(default 'Continue with what you were doing.')."},
                {"name": "delay_ms", "type": "integer",
                 "description": "Wait this many ms before injecting (default 0)."},
                {"name": "socket", "type": "string",
                 "description": "tmux socket path (tmux -S); defaults to the standard per-uid socket."},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    return v in (True, "true", "True", "1", 1)


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _handle_send(args: dict) -> dict:
    try:
        return tmux_inject.send(
            args["text"],
            pane=args.get("pane"),
            enter=_bool(args.get("enter"), True),
            delay_ms=_int(args.get("delay_ms"), 0),
            confirm=_bool(args.get("confirm"), False),
            followup=args.get("followup"),
            socket=args.get("socket"),
        )
    except tmux_inject.TmuxError as exc:
        return {"ok": False, "error": str(exc)}


def _handle_compact(args: dict) -> dict:
    try:
        return tmux_inject.send(
            "/compact",
            pane=args.get("pane"),
            delay_ms=_int(args.get("delay_ms"), 0),
            followup=args.get("followup"),
            socket=args.get("socket"),
        )
    except tmux_inject.TmuxError as exc:
        return {"ok": False, "error": str(exc)}


HANDLERS = {
    "send": _handle_send,
    "compact": _handle_compact,
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("reflection", API_MANIFEST, HANDLERS).run()


if __name__ == "__main__":
    asyncio.run(main())
