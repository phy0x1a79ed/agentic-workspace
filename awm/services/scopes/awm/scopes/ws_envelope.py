"""Scope-channel WebSocket envelope — typed builders for the JSON protocol.

A scope IS the channel (there are no rooms). The wire format is one JSON object
per WS text frame.

→ server (sent by subscriber):
    {"type": "post",    "body": "...", "kind": "text", "to": "<scope>?"}
    {"type": "control", "action": "close" | "kill"}
    {"type": "ping"}

← server (sent by host):
    {"type": "history",              "posts": [<Post>, ...]}
    {"type": "post",                 "post":  <Post>}
    {"type": "participant_joined",   "participant": {...}}
    {"type": "participant_left",     "participant": {...}}
    {"type": "upstream_disconnected","peer": "<id>", "reason": "..."}
    {"type": "lagged"}    # consumer too slow; server closes 1011 after
    {"type": "error",   "message": "..."}
    {"type": "pong"}
"""

from __future__ import annotations

from typing import Any, Iterable


# Event type constants — used in envelope match arms.
HISTORY = "history"
POST = "post"
PARTICIPANT_JOINED = "participant_joined"
PARTICIPANT_LEFT = "participant_left"
UPSTREAM_DISCONNECTED = "upstream_disconnected"
LAGGED = "lagged"
ERROR = "error"
PONG = "pong"

# Inbound types
CONTROL = "control"
PING = "ping"


def history(posts: Iterable[dict]) -> dict:
    return {"type": HISTORY, "posts": list(posts)}


def post_event(post: dict) -> dict:
    return {"type": POST, "post": post}


def participant_joined(participant: dict) -> dict:
    return {"type": PARTICIPANT_JOINED, "participant": participant}


def participant_left(participant: dict) -> dict:
    return {"type": PARTICIPANT_LEFT, "participant": participant}


def upstream_disconnected(peer: str, reason: str) -> dict:
    return {"type": UPSTREAM_DISCONNECTED, "peer": peer, "reason": reason}


def lagged() -> dict:
    return {"type": LAGGED}


def error(message: str) -> dict:
    return {"type": ERROR, "message": message}


def pong() -> dict:
    return {"type": PONG}


def is_lagged(event: Any) -> bool:
    return isinstance(event, dict) and event.get("type") == LAGGED
