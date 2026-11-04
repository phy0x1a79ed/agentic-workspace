"""The one ``operations`` tool: what the gateway publishes (queue.md R145)."""

from __future__ import annotations

from typing import Any

TOOL = "operations"
HIDDEN = ["cli", "http"]


def _p(name: str, type_: str = "string", required: bool = False, desc: str = "") -> dict:
    return {"name": name, "type": type_, "required": required, "description": desc}


SID = _p("session_id", desc="An R9 session id (cc + hex) or a Claude session UUID. Default: "
         "the calling session.")
TID = _p("task_id", required=True, desc="The 8-character task id.")
RID = _p("request_id", required=True)
#: The payload fields each request kind needs. The approver checks against this table.
PAYLOADS: dict[str, tuple[str, ...]] = {
    "admin_ssh": ("pubkey", "nodes"), "trust_raise": ("session_id", "from", "to"),
    "invite": ("perm", "reason", "holder_pubkey"), "browser_signin": ("seat", "site"),
    "vault_hold": (), "plugin_commits": ("change", "plugins")}
PAYLOAD_HELP = "; ".join(f"{k} {{{', '.join(v)}}}" for k, v in PAYLOADS.items()) + (
    ". plugin_commits.plugins is [{plugin, commit, source?, from?}]: an install, or a move "
    "from one commit to another (install.md R116 item 2). One request approves every plugin "
    "commit of the change.")
SHOWN = [_p("name", desc="The session's name, for the board."),
         _p("rc_url", desc="The session's Remote Control link, for the board.")]

VERBS: list[dict[str, Any]] = [
    {"name": "request_human", "effect": "write",
     "description": "Tell Tony you need him. kind blocked (with reason) or review (with summary) "
     "puts your card in Needs you. urgent:true on a block also sends him a Discord DM. kind "
     "approval posts an Arbutus admin bundle to Slack for Tony or Ryan. Tony answers in your "
     "session, and his prompt moves the card back to Working.",
     "params": [_p("kind", desc="blocked, review or approval."), _p("reason"), _p("summary"),
                _p("urgent", "boolean"), _p("bundle", desc="kind approval: the bundle's name."),
                _p("title", desc="A title, if this opens your card.")]},
    {"name": "task_open", "effect": "write", "surfaces": HIDDEN,
     "description": "Open a card for new work and make the session its main session. Works "
     "while alcor is down: the id is minted here and the write waits in the outbox.",
     "params": [_p("title", required=True), SID, _p("peer"), _p("tags", "array"), *SHOWN]},
    {"name": "task_attach", "effect": "write", "surfaces": HIDDEN,
     "description": "Put a session on an existing card, as cch start does for a task id.",
     "params": [TID, {**SID, "required": True}, _p("role", desc="main, worker or joined."),
                *SHOWN]},
    {"name": "task_join", "effect": "write",
     "description": "Join your session to a running task, when Tony started you while it ran. "
     "Your own card folds into it, and its main session is told.", "params": [TID, SID]},
    {"name": "task_report", "effect": "write",
     "description": "Move your card: working, planned, blocked, review or done. planned, "
     "blocked and review put it in Needs you.",
     "params": [_p("task_id", desc="Default: your session's task."), SID,
                _p("state", required=True), _p("reason"), _p("rc_url")]},
    {"name": "task_session_end", "effect": "write", "surfaces": HIDDEN,
     "description": "Mark a session of a task finished.", "params": [TID, {**SID, "required": True}]},
    {"name": "task_for_session", "effect": "read",
     "description": "A session's task id, or null.", "params": [SID]},
    {"name": "task_sessions", "effect": "read",
     "description": "Every session of a task, active or finished, with its role.", "params": [TID]},
    {"name": "task_get", "effect": "read", "description": "One card with its sessions.",
     "params": [TID]},
    {"name": "task_list", "effect": "read",
     "description": "Cards newest first, by section (proposed, needs_you, working, done), with a "
     "search. Pass next_before back as before for the next page.",
     "params": [_p("section"), _p("q"), _p("before", "number"), _p("limit", "integer")]},
    {"name": "hook_prompt", "effect": "write", "surfaces": HIDDEN,
     "description": "The prompt hook: a prompt to a session moves its card from Needs you to "
     "Working.", "params": [_p("prompt")]},
    {"name": "approval_request", "effect": "write",
     "description": "Ask Tony to approve an action on his passkey panel. Opens a Needs-you card "
     "and returns request_id and task_id. Expires after 24 h. Only your session can cancel it.",
     "params": [_p("kind", required=True, desc=", ".join(PAYLOADS) + "."),
                _p("node"), _p("reason", required=True),
                _p("minutes", "integer", desc="Grant length, 15 to 1440. Default 15."),
                _p("payload", "object", desc=PAYLOAD_HELP)]},
    {"name": "approval_get", "effect": "read",
     "description": "A request's status, with its grant and ssh certificate once approved.",
     "params": [RID]},
    {"name": "approval_wait", "effect": "read",
     "description": "Wait up to wait_s (at most 55) for a decision.",
     "params": [RID, _p("wait_s", "number")]},
    {"name": "approval_cancel", "effect": "write",
     "description": "Withdraw your own pending request.", "params": [RID]},
    {"name": "order_pull", "effect": "read",
     "description": "Signed orders waiting for a node. Long-polls up to wait_s (at most 55).",
     "params": [_p("node", desc="Default: this node."), _p("kinds", "array"),
                _p("wait_s", "number")]},
    {"name": "order_done", "effect": "write", "description": "Report how an order went.",
     "params": [_p("order_id", required=True), _p("ok", "boolean", True), _p("note")]},
    {"name": "keys", "effect": "read",
     "description": "alcor's public keys: the grant key, the ack key and the ssh user CA.",
     "params": []},
    {"name": "status", "effect": "read",
     "description": "This node's outbox and how long alcor has been unreachable.", "params": []},
    {"name": "ops_post", "effect": "write", "surfaces": HIDDEN,
     "description": "A service's notice in the Ops book, one note per kind and node, such as "
     "auth's login password. For this node's services, not for sessions.",
     "params": [_p("kind", required=True), _p("node", required=True), _p("title", required=True),
                _p("body", required=True)]},
    {"name": "devices", "effect": "read", "surfaces": HIDDEN,
     "description": "Enrolled devices. On alcor, for its operator only.", "params": []},
    {"name": "device_revoke", "effect": "write", "surfaces": HIDDEN,
     "description": "Revoke a device and its passkey. On alcor, for its operator only.",
     "params": [_p("device_id", required=True)]},
]

API_MANIFEST = {
    "description": "Your task's card on Tony's Swarm board, and Tony's approvals (queue.md, "
                   "fleet.md R97, R106). Call request_human when you need Tony. Tony answers "
                   "in your session, not on the card.",
    "functions": [{**v, "tool": TOOL} for v in VERBS],
    "emitters": [], "sessions": [],
}
