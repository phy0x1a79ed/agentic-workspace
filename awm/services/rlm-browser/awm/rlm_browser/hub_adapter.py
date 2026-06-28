"""Hub adapter for the rlm-browser realm service.

Boots rlm-browser as a gateway-registered process: stands up its own DB, then
runs the shared :class:`awm.gatewayclient.ServiceAdapter` loop (register → ready
→ serve → reconnect). The realm-family functions are exposed over the control WS
and projected into the gateway catalog as ``rlm_browser_<verb>`` tools.

Skeleton: handlers are stubs. ``acquire`` mints a fake ``session_id`` backed by a
real DB row; ``observe`` returns a placeholder snapshot; the act verbs validate
the session and return acks. Real Chrome/CDP (launch a browser per session,
drive it over the DevTools protocol by loopback IP) and the VPN netns +
kill-switch are later passes — see this scope's ``.awm/context.md``. The
``browser`` emitter is declared now so a later real-Chrome pass can fire
``rlm.browser.<event>`` without a manifest change forcing a re-register.

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.rlm_browser.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.rlm_browser import dao

log = logging.getLogger("awm.rlm_browser.hub_adapter")

# Placeholder perception payload returned by ``observe`` until real CDP lands.
_PLACEHOLDER_SNAPSHOT = {
    "url": None,
    "title": None,
    "tree": [],  # a11y/DOM tree — empty in the skeleton
    "note": "placeholder snapshot; real CDP perception not wired yet",
}

API_MANIFEST: dict[str, Any] = {
    "functions": [
        # ---- lifecycle ----
        {
            "name": "acquire",
            "tool": "rlm_browser_acquire",
            "description": (
                "Acquire a browser realm session for a game. Returns "
                "{session_id}. Skeleton: mints a fake id backed by a DB row; no "
                "Chrome is launched yet."
            ),
            "params": [
                {"name": "game", "type": "string", "required": True},
                {"name": "opts", "type": "object", "required": False},
            ],
        },
        {
            "name": "release",
            "tool": "rlm_browser_release",
            "description": "Release a realm session and free its (future) browser.",
            "params": [
                {"name": "session_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "reset",
            "tool": "rlm_browser_reset",
            "description": "Reset a session in place (clear state, keep the slot).",
            "params": [
                {"name": "session_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "status",
            "tool": "rlm_browser_status",
            "description": (
                "Status of one session (pass session_id) or all sessions "
                "(omit it). Returns {sessions: [...]} ."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": False},
            ],
        },
        # ---- perceive ----
        {
            "name": "observe",
            "tool": "rlm_browser_observe",
            "description": (
                "Observe a session: returns {snapshot, screenshot?}. Skeleton: "
                "placeholder snapshot, null screenshot."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
            ],
        },
        # ---- act ----
        {
            "name": "navigate",
            "tool": "rlm_browser_navigate",
            "description": "Navigate the session's browser to a URL.",
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "url", "type": "string", "required": True},
            ],
        },
        {
            "name": "click",
            "tool": "rlm_browser_click",
            "description": "Click an element (by selector) in the session.",
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "selector", "type": "string", "required": True},
            ],
        },
        {
            "name": "type",
            "tool": "rlm_browser_type",
            "description": "Type text into the session (optionally into a selector).",
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "text", "type": "string", "required": True},
                {"name": "selector", "type": "string", "required": False},
            ],
        },
        {
            "name": "key",
            "tool": "rlm_browser_key",
            "description": "Press a key (e.g. 'Enter', 'Tab') in the session.",
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "key", "type": "string", "required": True},
            ],
        },
        {
            "name": "wait",
            "tool": "rlm_browser_wait",
            "description": (
                "Wait for a selector to appear, or a fixed timeout, in the session."
            ),
            "params": [
                {"name": "session_id", "type": "string", "required": True},
                {"name": "selector", "type": "string", "required": False},
                {"name": "timeout_ms", "type": "integer", "required": False},
            ],
        },
    ],
    "emitters": [
        {
            "topic": "browser",
            "description": (
                "Fires on a realm-side browser event. Payload "
                "{session_id, kind, data} where kind is e.g. 'captcha_detected' "
                "or 'error' — projected as rlm.browser.<kind>. Effectors "
                "subscribe filtered to their session_id. Not fired yet in the "
                "skeleton (no real browser)."
            ),
        },
    ],
    "sessions": [],
}


def _require_session(session_id: str) -> dict:
    """Look up a session or raise — used by act/perceive verbs."""
    row = dao.BrowserDAO().get_session(session_id)
    if row is None:
        raise ValueError(f"unknown session_id: {session_id!r}")
    return row


def _acquire(args: dict) -> dict:
    row = dao.BrowserDAO().create_session(args["game"])
    # opts is accepted and ignored in the skeleton (e.g. headful, proxy, viewport).
    return {"session_id": row["session_id"]}


def _release(args: dict) -> dict:
    removed = dao.BrowserDAO().delete_session(args["session_id"])
    return {"released": removed}


def _reset(args: dict) -> dict:
    _require_session(args["session_id"])
    row = dao.BrowserDAO().set_status(args["session_id"], "active")
    return {"session_id": row["session_id"], "status": row["status"]}


def _status(args: dict) -> dict:
    d = dao.BrowserDAO()
    sid = args.get("session_id")
    if sid:
        row = d.get_session(sid)
        return {"sessions": [row] if row else []}
    return {"sessions": d.list_sessions()}


def _observe(args: dict) -> dict:
    _require_session(args["session_id"])
    return {"snapshot": dict(_PLACEHOLDER_SNAPSHOT), "screenshot": None}


def _act_ack(verb: str, args: dict) -> dict:
    """Shared stub for act verbs: validate the session, echo the request."""
    _require_session(args["session_id"])
    echoed = {k: v for k, v in args.items() if k != "session_id"}
    return {"ok": True, "verb": verb, "session_id": args["session_id"],
            "args": echoed, "note": "stub ack; no browser action performed"}


HANDLERS = {
    "acquire": _acquire,
    "release": _release,
    "reset": _reset,
    "status": _status,
    "observe": _observe,
    "navigate": lambda args: _act_ack("navigate", args),
    "click": lambda args: _act_ack("click", args),
    "type": lambda args: _act_ack("type", args),
    "key": lambda args: _act_ack("key", args),
    "wait": lambda args: _act_ack("wait", args),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "rlm-browser", API_MANIFEST, HANDLERS, on_start=dao.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
