"""The ``queue-store`` verbs: the manifest the gateway publishes and the handlers behind it."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from awm.taskqueue.store import Store

TOOL = "queue-store"


def _p(name: str, type_: str = "string", required: bool = False, desc: str = "") -> dict:
    return {"name": name, "type": type_, "required": required, "description": desc}


SID = _p("session_id", required=True, desc="The R9 session id, cc plus the Claude session UUID.")
TID = _p("task_id", required=True, desc="The 8-character task id.")
OP = _p("op_id", desc="The relay's replay key. A repeated op_id returns the first result.")
NODE = _p("node", desc="The node the session runs on.")

VERBS: list[dict[str, Any]] = [
    {"name": "task_open", "description": "Open a new card and make the session its main session.",
     "params": [_p("title", required=True), _p("session_id"), _p("task_id"), _p("kind"),
                _p("peer"), NODE, _p("section"), _p("status"), _p("tags", "array"),
                _p("meta", "object"), _p("name"), _p("rc_url"), OP]},
    {"name": "task_attach", "description": "Put a session on an existing card.",
     "params": [TID, SID, _p("role"), NODE, _p("name"), _p("rc_url"), OP]},
    {"name": "task_join", "description": "Join a running task. Folds the joiner's own card into it "
     "and returns main_session_id, which the caller notifies.",
     "params": [TID, SID, NODE, OP]},
    {"name": "task_for_session", "description": "The task id a session belongs to, or null.",
     "params": [SID]},
    {"name": "task_sessions", "description": "Every session of a task, active or ended.",
     "params": [TID]},
    {"name": "task_report", "description": "Move the session's card: working, planned, blocked, "
     "review or done.",
     "params": [TID, _p("session_id"), _p("state", required=True), _p("reason"), _p("rc_url"), OP]},
    {"name": "task_session_end", "description": "Mark a session of a task inactive.",
     "params": [TID, SID, OP]},
    {"name": "task_get", "description": "One card with its sessions table.", "params": [TID]},
    {"name": "task_list", "description": "Cards newest first, by section, with a search. Done "
     "pages back with before=<next_before>.",
     "params": [_p("section"), _p("q"), _p("before", "number"), _p("limit", "integer")]},
    {"name": "events_since", "description": "Store changes after an event id.",
     "params": [_p("after", "integer"), _p("limit", "integer")]},
    {"name": "card_action", "description": "Apply an action the approver verified. Refused "
     "without the approver's MAC.",
     "params": [_p("action", required=True), _p("task_id"), _p("args", "object", True),
                _p("ts", "number", True), _p("mac", required=True)]},
    {"name": "approval_card", "description": "Open an approval request's card. Approver only.",
     "params": [_p("request_id", required=True), _p("title", required=True), _p("node"),
                _p("meta", "object", True), _p("mac", required=True), _p("ts", "number", True)]},
    {"name": "approval_resolve", "description": "Close an approval card with its outcome. "
     "Approver only.",
     "params": [TID, _p("outcome", required=True), _p("mac", required=True),
                _p("ts", "number", True)]},
]

API_MANIFEST = {
    "description": "The fleet's one task store on alcor (fleet.md R106). Sessions reach it "
                   "through the queue tool on their own node, never directly.",
    "functions": [{**v, "tool": TOOL} for v in VERBS],
    "emitters": [], "sessions": [],
}


def open_store() -> Store:
    data = Path(os.environ.get("AWM_QUEUE_DB", Path.home() / ".awm" / "taskqueue" / "queue.db"))
    secret = os.environ.get("AWM_QUEUE_ACTION_SECRET", "").encode()
    return Store(data, action_secret=secret)


def handlers(store: Store) -> dict[str, Callable[[dict], Any]]:
    def call(method: str, positional: tuple[str, ...]):
        fn = getattr(store, method)

        def h(args: dict) -> Any:
            args = dict(args or {})
            pos = [args.pop(k) if k in args else None for k in positional]
            return fn(*pos, **args)
        return h

    return {
        "task_open": call("task_open", ("title",)),
        "task_attach": call("task_attach", ("task_id", "session_id")),
        "task_join": call("task_join", ("task_id", "session_id")),
        "task_for_session": call("task_for_session", ("session_id",)),
        "task_sessions": call("task_sessions", ("task_id",)),
        "task_report": call("task_report", ("task_id", "session_id", "state")),
        "task_session_end": call("task_session_end", ("task_id", "session_id")),
        "task_get": call("task_get", ("task_id",)),
        "task_list": call("task_list", ()),
        "events_since": call("events_since", ()),
        "card_action": call("card_action", ("action", "task_id", "args", "ts", "mac")),
        "approval_card": call("approval_card", ("request_id", "title")),
        "approval_resolve": call("approval_resolve", ("task_id", "outcome")),
    }
