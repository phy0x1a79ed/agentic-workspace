"""The ``approver`` gateway verbs. Tony's own actions never come this way: they come signed through the panel."""

from __future__ import annotations

from typing import Any, Callable

from awm.approver.core import Approver

TOOL = "approver"


def _p(name: str, type_: str = "string", required: bool = False, desc: str = "") -> dict:
    return {"name": name, "type": type_, "required": required, "description": desc}


RID = _p("request_id", required=True)

VERBS: list[dict[str, Any]] = [
    {"name": "approval_request", "description": "Ask Tony to approve an action. Opens a Needs-you "
     "card. Returns request_id and task_id. Expires after 24 h.",
     "params": [_p("kind", required=True, desc="admin_ssh, trust_raise, invite, browser_signin or vault_hold."),
                _p("node"), _p("reason", required=True),
                _p("minutes", "integer", desc="Grant length, 15 to 1440. Default 15."),
                _p("payload", "object", desc="admin_ssh {pubkey, nodes}; trust_raise {session_id, "
                   "from, to}; invite {perm, reason, holder_pubkey}; browser_signin {seat, site}; vault_hold {}."),
                _p("requester_session")]},
    {"name": "approval_get", "description": "A request's status, with its grant and ssh certificate "
     "once approved.", "params": [RID]},
    {"name": "approval_wait", "description": "Wait up to wait_s (at most 55) for a decision.",
     "params": [RID, _p("wait_s", "number")]},
    {"name": "approval_cancel", "description": "Withdraw a pending request.", "params": [RID]},
    {"name": "devices", "description": "Enrolled devices and their state.", "params": []},
    {"name": "device_revoke", "description": "Revoke a device and its passkey.",
     "params": [_p("device_id", required=True)]},
    {"name": "order_pull", "description": "Signed orders waiting for a node. Long-polls up to "
     "wait_s (at most 55).",
     "params": [_p("node", required=True), _p("kinds", "array"), _p("wait_s", "number")]},
    {"name": "order_done", "description": "Report how an order went.",
     "params": [_p("order_id", required=True), _p("ok", "boolean", True), _p("note")]},
    {"name": "keys", "description": "alcor's public keys: the grant key, the ack key and the ssh "
     "user CA.", "params": []},
]

API_MANIFEST = {
    "description": "alcor's approver (fleet.md R97): approval requests, grants, ssh certificates "
                   "and signed orders. Tony decides on the passkey panel, never through this tool.",
    "functions": [{**v, "tool": TOOL} for v in VERBS],
    "emitters": [], "sessions": [],
}


def handlers(ap: Approver) -> dict[str, Callable[[dict], Any]]:
    async def request(a: dict) -> Any:
        return await ap.approval_request(a["kind"], a.get("node"), a.get("reason", ""),
                                         a.get("minutes", 15), a.get("payload"),
                                         a.get("requester_session"))

    async def wait(a: dict) -> Any:
        return await ap.approval_wait(a["request_id"], float(a.get("wait_s", 55)))

    async def cancel(a: dict) -> Any:
        return await ap.approval_cancel(a["request_id"])

    async def pull(a: dict) -> Any:
        return await ap.order_pull(a["node"], a.get("kinds"), float(a.get("wait_s", 0)))

    return {
        "approval_request": request,
        "approval_get": lambda a: ap.approval_get(a["request_id"]),
        "approval_wait": wait,
        "approval_cancel": cancel,
        "devices": lambda a: ap.devices(),
        "device_revoke": lambda a: ap.device_revoke(a["device_id"]),
        "order_pull": pull,
        "order_done": lambda a: ap.order_done(a["order_id"], bool(a["ok"]), a.get("note", "")),
        "keys": lambda a: {"grant_pub": ap.keys.grant_pub_b64u(),
                           "ack_pub": ap.keys.ack_pub_spki_b64u(),
                           "ssh_user_ca": ap.keys.ssh_ca_pub()},
    }
