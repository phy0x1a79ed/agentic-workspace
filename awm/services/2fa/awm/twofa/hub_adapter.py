"""Hub adapter for the 2fa service.

Boots the local Duo 2FA capability as a gateway-registered process: builds the
:class:`~awm.twofa.service.TwoFAService` singleton, then runs the shared
:class:`awm.gatewayclient.ServiceAdapter` loop (register → ready → serve →
reconnect). The eight verbs are projected into the gateway catalog as ``2fa_*``
tools on MCP / HTTP / CLI.

The service folder is ``2fa`` (the gateway service name + verb domain); the
import package is ``awm.twofa`` because a Python module name can't start with a
digit. Run via ``run.sh`` (which the hub spawns and respawns):

    python -m awm.twofa.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.twofa.service import TwoFAService

log = logging.getLogger("awm.twofa.hub_adapter")

_svc = TwoFAService()

API_MANIFEST: dict[str, Any] = {
    "description": (
        "The fleet's single Duo approver. You almost certainly do not want to "
        "call it: `ssh(verb=connect)` and the vpn service arm it themselves, "
        "immediately before the one login they are about to make, and the fleet "
        "counts MFA attempts on that basis. Arming a window by hand spends "
        "budget nothing is accounting for, on an account that locks out after "
        "10 failed attempts in a row. What is left here is diagnosis: ask "
        "whether the approver can reach Duo and what it has seen."
    ),
    "functions": [
        {
            "name": "devices",
            "tool": "2fa_devices",
            "description": (
                "List discovered Duo devices and their enrollment — the valid "
                "device= names for the other verbs."
            ),
        },
        {
            "name": "ping",
            "tool": "2fa_ping",
            "description": (
                "Reachability + enrollment check. Makes a REAL read-only Duo "
                "API call (lists already-pending transactions; fires no push "
                "and spends no MFA budget), so ok=true means the approver was "
                "verified working just now — not merely that creds exist on "
                "disk. device=<name> for one device; omit for every device."
            ),
            "params": [
                {"name": "device", "type": "string", "required": False},
            ],
        },
        {
            "name": "reachability",
            "tool": "2fa_reachability",
            "description": (
                "The last VERIFIED Duo round-trip for one device, as a "
                "timestamp, without probing. A burst window refreshes it every "
                "poll, so it now reflects real traffic rather than only the "
                "last explicit ping — but it still only says WHEN, so prefer "
                "2fa_ping to ask whether the approver works right now."
            ),
            "params": [
                {"name": "device", "type": "string", "required": True},
            ],
        },
        {
            "name": "status",
            "tool": "2fa_status",
            "description": (
                "Full 2fa state: enrolled?, burst active?/expected approvals, "
                "held logins, approve-all window, approved count, and "
                "transactions_seen — Duo transactions observed for the device "
                "over this process's life. Only the difference between two "
                "readings of transactions_seen means anything: an unchanged "
                "count across an attempt proves Duo was never presented with a "
                "login. device=<name> for one device; omit to report every one."
            ),
            "params": [
                {"name": "device", "type": "string", "required": False},
            ],
        },
        {
            "name": "pending",
            "tool": "2fa_pending",
            "description": (
                "List currently-held (burst) Duo logins awaiting a decision. "
                "device=<name> for one device; omit to report every device."
            ),
            "params": [
                {"name": "device", "type": "string", "required": False},
            ],
        },
        {
            "name": "activate",
            "tool": "2fa_activate",
            # CLI/HTTP only. Enrolment is an operator act at a keyboard, with a
            # code out of an email. Nothing an agent does should reach it.
            "surfaces": ["cli", "http"],
            "description": (
                "Enroll a Duo device from a 'CODE-BASE64HOST' activation string "
                "(the QR / email value) under device=<name>; writes creds 0600. "
                "Operator verb — CLI only (`awm 2fa activate`)."
            ),
            "params": [
                {"name": "code", "type": "string", "required": True},
                {"name": "device", "type": "string", "required": True},
            ],
        },
        {
            "name": "burst",
            "tool": "2fa_burst",
            # CLI/HTTP only. A burst is authorization to approve a login without
            # a human, and it is only safe when the thing that arms it is also
            # the thing about to log in — which is why ssh and vpn arm their own,
            # one push at a time, over RPC (unaffected by this key). An agent
            # arming one is authorizing a login it does not control.
            "surfaces": ["cli", "http"],
            "description": (
                "Open (or extend) a counted poll window on device=<name> that "
                "auto-approves lone Duo logins. count= expected approvals to add "
                "(default 1); overlapping bursts add to the counter and extend "
                "the window. Returns started/extended, plus transactions_seen — "
                "Duo's observation count at arming, which a caller compares "
                "against 2fa_status afterwards to prove no login was presented. "
                "CLI only: ssh and vpn arm their own window per login, so an "
                "agent arming one is authorizing a login nothing is counting."
            ),
            "params": [
                {"name": "device", "type": "string", "required": True},
                {"name": "window", "type": "number", "required": False},
                {"name": "interval", "type": "number", "required": False},
                {"name": "count", "type": "number", "required": False},
            ],
        },
        {
            "name": "approve",
            "tool": "2fa_approve",
            # CLI/HTTP only: answering a live MFA challenge is the operator's
            # decision, and the whole security property of the second factor.
            "surfaces": ["cli", "http"],
            "description": (
                "Approve a held Duo login by urgid on device=<name>. "
                "Operator verb — CLI only (`awm 2fa approve`)."
            ),
            "params": [
                {"name": "urgid", "type": "string", "required": True},
                {"name": "device", "type": "string", "required": True},
            ],
        },
        {
            "name": "deny",
            "tool": "2fa_deny",
            "surfaces": ["cli", "http"],
            "description": (
                "Deny a held Duo login by urgid on device=<name>. "
                "Operator verb — CLI only (`awm 2fa deny`)."
            ),
            "params": [
                {"name": "urgid", "type": "string", "required": True},
                {"name": "device", "type": "string", "required": True},
            ],
        },
        {
            "name": "approve_all",
            "tool": "2fa_approve_all",
            # CLI/HTTP only, and the widest of them: an uncounted 5-minute
            # blanket yes to anything Duo presents.
            "surfaces": ["cli", "http"],
            "description": (
                "Open a 5-minute approve-all window on device=<name> and clear "
                "every held login. Operator verb — CLI only "
                "(`awm 2fa approve-all`)."
            ),
            "params": [
                {"name": "device", "type": "string", "required": True},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}


async def _h_burst(args: dict[str, Any]) -> Any:
    return await _svc.start_burst(
        args["device"], args.get("window"), args.get("interval"),
        args.get("count", 1))


HANDLERS = {
    "devices": lambda args: _svc.devices(),
    "ping": lambda args: _svc.ping(args.get("device")),
    "reachability": lambda args: _svc.reachability(args["device"]),
    "status": lambda args: _svc.status(args.get("device")),
    "pending": lambda args: _svc.pending(args.get("device")),
    "activate": lambda args: _svc.activate(args["code"], args["device"]),
    "burst": _h_burst,
    "approve": lambda args: _svc.approve(args["urgid"], args["device"]),
    "deny": lambda args: _svc.deny(args["urgid"], args["device"]),
    "approve_all": lambda args: _svc.approve_all(args["device"]),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "2fa", API_MANIFEST, HANDLERS, on_start=_svc.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
