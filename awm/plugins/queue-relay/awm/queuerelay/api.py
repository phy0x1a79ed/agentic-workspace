"""The ``queue`` verbs every session calls on its own node."""

from __future__ import annotations

from typing import Any

from awm.queuerelay.relay import READS, WRITES, Relay

TOOL = "queue"


def _p(name, type_="string", required=False, desc=""):
    return {"name": name, "type": type_, "required": required, "description": desc}


SID, TID = _p("session_id", required=True), _p("task_id", required=True)
VERBS = [
    {"name": "task_open", "description": "Open a card for new work and become its main session. "
     "Works while alcor is down: the id is minted here and the write waits in the outbox.",
     "params": [_p("title", required=True), _p("session_id"), _p("peer"), _p("tags", "array"),
                _p("name"), _p("rc_url")]},
    {"name": "task_attach", "description": "Put a session on an existing card.",
     "params": [TID, SID, _p("role"), _p("name"), _p("rc_url")]},
    {"name": "task_join", "description": "Join a running task, folding your own card into it. "
     "Returns main_session_id: tell that session you joined.", "params": [TID, SID]},
    {"name": "task_report", "description": "Move your card: working, planned, blocked, review "
     "or done. blocked and review put it in Needs you.",
     "params": [TID, _p("session_id"), _p("state", required=True), _p("reason"), _p("rc_url")]},
    {"name": "task_session_end", "description": "Mark your session inactive on the card.",
     "params": [TID, SID]},
    {"name": "task_for_session", "description": "Your task id, or null.", "params": [SID]},
    {"name": "task_sessions", "description": "Every session of a task.", "params": [TID]},
    {"name": "task_get", "description": "One card with its sessions.", "params": [TID]},
    {"name": "task_list", "description": "Cards by section (proposed, needs_you, working, done), "
     "with a search.", "params": [_p("section"), _p("q"), _p("before", "number"),
                                  _p("limit", "integer")]},
    {"name": "status", "description": "This node's outbox and how long alcor has been away.",
     "params": []},
]
API_MANIFEST = {
    "description": "Your task's card on the Swarm board (queue.md, fleet.md R106). Open a card "
                   "for new work, report blocked or review when you need Tony, done when you "
                   "finish. Tony answers in your session, not on the card.",
    "functions": [{**v, "tool": TOOL} for v in VERBS], "emitters": [], "sessions": [],
}


def handlers(relay: Relay) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for verb in WRITES:
        out[verb] = (lambda v: (lambda args: relay.write(v, args)))(verb)
    for verb in READS:
        out[verb] = (lambda v: (lambda args: relay.read(v, args)))(verb)
    out["status"] = lambda args: relay.status()
    return out
