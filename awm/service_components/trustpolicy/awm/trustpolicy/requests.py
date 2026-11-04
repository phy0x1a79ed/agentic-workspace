"""Ask Tony for a grant through the approver on alcor (fleet.md R97, R100, R90, R102, R103).

The calls go to this node's ``operations`` relay, which carries them to alcor and
records the calling session as the requester. Each request is a card in Needs you.
The approver answers with a signed grant, and for ``admin_ssh`` also with an ssh user
certificate. A request runs from 15 minutes to 24 hours and expires 24 hours after it
is made (R100 items 1-2).
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
from typing import Any, Callable

MIN_MINUTES = 15
MAX_MINUTES = 24 * 60
GRANT_KEY_FILE = "grant-key.pub"
WAIT_STEP_S = 55  # the approver holds one approval_wait at most this long

Call = Callable[[str, dict], Any]


class GrantDenied(PermissionError):
    """Tony denied the request, or it expired or was cancelled."""


def _approver_call(verb: str, args: dict) -> Any:
    from awm import gatewayclient
    return gatewayclient.call_sync("operations", verb, args, timeout=60.0)


def grant_key(awm_dir: Path | None = None) -> bytes:
    """alcor's raw Ed25519 grant key: the approver's ``grant_pub`` (base64url) in
    ``<AWM_DIR>/grant-key.pub``, written by the fleet's config."""
    if awm_dir is None:
        import awm.config as config
        awm_dir = config.AWM_DIR
    text = (awm_dir / GRANT_KEY_FILE).read_text().strip()
    raw = base64.urlsafe_b64decode(text.replace("+", "-").replace("/", "_") + "=" * (-len(text) % 4))
    if len(raw) != 32:
        raise ValueError(f"{awm_dir / GRANT_KEY_FILE}: expected a 32-byte Ed25519 key")
    return raw


def _minutes(minutes: int) -> int:
    if not MIN_MINUTES <= int(minutes) <= MAX_MINUTES:
        raise ValueError(f"a grant lasts {MIN_MINUTES} to {MAX_MINUTES} minutes, not {minutes}")
    return int(minutes)


def file_request(kind: str, *, node: str, reason: str, minutes: int = MIN_MINUTES,
                 payload: dict | None = None, call: Call = _approver_call) -> str:
    """File a request without waiting for the answer. Returns its id."""
    if not reason.strip():
        raise ValueError("a request needs a reason Tony can read")
    filed = call("approval_request", {
        "kind": kind, "node": node, "reason": reason, "minutes": _minutes(minutes),
        "payload": payload or {}})
    return filed["request_id"]


def request(kind: str, *, node: str, reason: str, minutes: int = MIN_MINUTES,
            payload: dict | None = None, wait_s: float = 24 * 3600, call: Call = _approver_call) -> dict:
    """File one request and wait for Tony. Returns the approver's final record."""
    if not reason.strip():
        raise ValueError("a request needs a reason Tony can read")
    filed = call("approval_request", {
        "kind": kind, "node": node, "reason": reason, "minutes": _minutes(minutes),
        "payload": payload or {}})
    rid = filed["request_id"]
    while True:
        step = min(wait_s, WAIT_STEP_S)
        rec = call("approval_wait", {"request_id": rid, "wait_s": step})
        state = rec.get("status") or rec.get("state")
        if state == "approved":
            return rec
        if state in ("denied", "expired", "cancelled"):
            raise GrantDenied(f"request {rid} for {kind} on {node} was {state}")
        wait_s -= step
        if wait_s <= 0:
            call("approval_cancel", {"request_id": rid})
            raise GrantDenied(f"request {rid} for {kind} on {node} had no answer in time")


def admin_cert(nodes: list[str], reason: str, *, minutes: int = MIN_MINUTES,
               key: Path = Path.home() / ".ssh" / "id_ed25519_admin",
               call: Call = _approver_call) -> Path:
    """One admin grant for every node of a run (R100 item 4): the ssh user
    certificate lands beside ``key`` as ``<key>-cert.pub``. ssh then logs in as
    each node's admin account with it, and the node checks alcor's CA."""
    if not nodes:
        raise ValueError("name at least one node")
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "awm-admin",
                        "-f", str(key)], check=True)
    pubkey = Path(f"{key}.pub").read_text().strip()
    rec = request("admin_ssh", node=",".join(sorted(nodes)), reason=reason, minutes=minutes,
                  payload={"pubkey": pubkey, "nodes": sorted(nodes)}, call=call)
    cert = (rec.get("grant") or {}).get("ssh_cert") or rec.get("ssh_cert")
    if not cert:
        raise GrantDenied("the approver approved but returned no certificate")
    path = Path(f"{key}-cert.pub")
    path.write_text(cert.strip() + "\n")
    path.chmod(0o600)
    return path


def trust_raise(session_id: str, current: str, to: str, *, node: str, reason: str,
                minutes: int = MIN_MINUTES, call: Call = _approver_call) -> dict:
    """A grant that lets reflection raise ``session_id`` from ``current`` to ``to`` (R103)."""
    rec = request("trust_raise", node=node, reason=reason, minutes=minutes,
                  payload={"session_id": session_id, "from": current, "to": to}, call=call)
    return rec["grant"]


def knowledge_invite(perm: str, reason: str, holder_pubkey: str, *, node: str,
                     minutes: int, call: Call = _approver_call) -> dict:
    """A timed invite to ``node``'s knowledge port, read only or read and write (R90)."""
    if perm not in ("ro", "rw"):
        raise ValueError("perm is ro or rw")
    rec = request("invite", node=node, reason=reason, minutes=minutes,
                  payload={"perm": perm, "reason": reason, "holder_pubkey": holder_pubkey}, call=call)
    return rec["grant"]


def browser_signin(seat: str, site: str, *, node: str, reason: str,
                   call: Call = _approver_call) -> dict:
    """Ask Tony to pass a captcha or sign in for ``seat`` on ``site`` (R102 item 7)."""
    rec = request("browser_signin", node=node, reason=reason,
                  payload={"seat": seat, "site": site}, call=call)
    return rec["grant"]
