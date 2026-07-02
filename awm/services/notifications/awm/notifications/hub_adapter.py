"""Hub adapter for the notifications service.

Registers with the gateway via ``ServiceAdapter``, projects the verbs below
onto the HTTP + CLI surfaces (deliberately NOT the MCP surface — agents must
not self-notify or snoop the board), and publishes live deltas on the ``feed``
emitter that the board page subscribes to at ``/svc/notifications/emit/feed``.

Producers are harness-level, not services: the global Claude Code hook and the
global OpenCode plugin POST ``/svc/notifications/fn/report`` fire-and-forget.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from . import dao, service

log = logging.getLogger("awm.notifications")

_CLI_HTTP = ["cli", "http"]

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "report",
            "tool": "notifications_report",
            "surfaces": _CLI_HTTP,
            "timeout": 30,
            "description": (
                "Ingest one normalized harness lifecycle event (from the global "
                "Claude Code hook / OpenCode plugin): turn_end, notification, "
                "user_prompt, error, session_start, session_end. Classifies "
                "into question/idle/error attention items, auto-resolves on "
                "user_prompt, and pushes a delta on the feed emitter."
            ),
            "params": [
                {"name": "harness", "type": "string", "required": True,
                 "description": "claude | opencode"},
                {"name": "event", "type": "string", "required": True,
                 "description": "turn_end | notification | user_prompt | error"
                                " | session_start | session_end"},
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
            ],
        },
        {
            "name": "list",
            "tool": "notifications_list",
            "surfaces": _CLI_HTTP,
            "description": "Open attention items (+ the recently-resolved hour)"
                           " with their session rows. Runs the lazy stale-sweep.",
            "params": [
                {"name": "all", "type": "boolean",
                 "description": "Include resolved history (last 500)."},
            ],
        },
        {
            "name": "mark_seen",
            "tool": "notifications_seen",
            "surfaces": _CLI_HTTP,
            "description": "Stamp an item as seen (the page pushed/displayed it).",
            "params": [
                {"name": "id", "type": "string", "required": True},
            ],
        },
        {
            "name": "resolve",
            "tool": "notifications_resolve",
            "surfaces": _CLI_HTTP,
            "description": "Resolve one item by id, or every open item for a session.",
            "params": [
                {"name": "id", "type": "string"},
                {"name": "session_id", "type": "string"},
            ],
        },
        {
            "name": "clear",
            "tool": "notifications_clear",
            "surfaces": _CLI_HTTP,
            "description": "Resolve every open item (board reset).",
            "params": [],
        },
        {
            "name": "stats",
            "tool": "notifications_stats",
            "surfaces": _CLI_HTTP,
            "description": "Open-item counts by kind + session counts by state.",
            "params": [],
        },
    ],
    "emitters": [
        {
            "topic": "feed",
            "description": "Live board deltas: one JSON frame per raise/update/"
                           "resolve/session event. The page treats each frame "
                           "as a doorbell and re-fetches list.",
        },
    ],
    "sessions": [],
}

_adapter: ServiceAdapter | None = None


async def _emit_feed(payload: dict[str, Any]) -> None:
    if _adapter is not None and payload.get("type"):
        try:
            await _adapter.emit("feed", payload)
        except Exception:  # noqa: BLE001 — emit is best-effort signalling
            log.debug("feed emit failed", exc_info=True)


# ---------------------------------------------------------------------------
# Handlers — each opens a fresh connection to the service's own DB.
# ---------------------------------------------------------------------------


async def _handle_report(args: dict) -> dict:
    conn = dao.connect()
    try:
        delta = await service.handle_report(conn, args or {})
    finally:
        conn.close()
    await _emit_feed(delta)
    return delta


def _handle_list(args: dict) -> dict:
    conn = dao.connect()
    try:
        return service.list_items(conn, all=bool(args.get("all")))
    finally:
        conn.close()


def _handle_mark_seen(args: dict) -> dict:
    conn = dao.connect()
    try:
        return service.mark_seen(conn, args["id"])
    finally:
        conn.close()


async def _handle_resolve(args: dict) -> dict:
    conn = dao.connect()
    try:
        ids = service.resolve_items(
            conn, item_id=args.get("id"), session_id=args.get("session_id"),
            by="page")
    finally:
        conn.close()
    delta = {"ok": True, "type": "resolve" if ids else None, "ids": ids}
    await _emit_feed(delta)
    return delta


async def _handle_clear(args: dict) -> dict:
    conn = dao.connect()
    try:
        out = service.clear_all(conn)
    finally:
        conn.close()
    if out.get("resolved"):
        await _emit_feed({"type": "resolve", "ids": out["resolved"]})
    return out


def _handle_stats(args: dict) -> dict:
    conn = dao.connect()
    try:
        return service.stats(conn)
    finally:
        conn.close()


HANDLERS = {
    "report": _handle_report,
    "list": _handle_list,
    "mark_seen": _handle_mark_seen,
    "resolve": _handle_resolve,
    "clear": _handle_clear,
    "stats": _handle_stats,
}


async def main() -> None:
    global _adapter
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    _adapter = ServiceAdapter(
        "notifications", API_MANIFEST, HANDLERS, on_start=dao.init)
    await _adapter.run()


if __name__ == "__main__":
    asyncio.run(main())
