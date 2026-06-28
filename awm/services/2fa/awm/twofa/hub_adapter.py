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
    "functions": [
        {
            "name": "ping",
            "tool": "2fa_ping",
            "description": "Liveness + enrollment check for the 2fa device.",
        },
        {
            "name": "status",
            "tool": "2fa_status",
            "description": (
                "Full 2fa state: enrolled?, burst active?, held logins, "
                "approve-all window remaining, approved count."
            ),
        },
        {
            "name": "pending",
            "tool": "2fa_pending",
            "description": "List currently-held (burst) Duo logins awaiting a decision.",
        },
        {
            "name": "activate",
            "tool": "2fa_activate",
            "description": (
                "Enroll a fresh Duo device from a 'CODE-BASE64HOST' activation "
                "string (the QR / email value); writes device creds 0600."
            ),
            "params": [
                {"name": "code", "type": "string", "required": True},
            ],
        },
        {
            "name": "burst",
            "tool": "2fa_burst",
            "description": (
                "Open a bounded poll window that auto-approves a lone Duo login "
                "and holds a concurrent burst. Returns started/already-running."
            ),
            "params": [
                {"name": "window", "type": "number", "required": False},
                {"name": "interval", "type": "number", "required": False},
                {"name": "exit_on_approve", "type": "boolean", "required": False},
            ],
        },
        {
            "name": "approve",
            "tool": "2fa_approve",
            "description": "Approve a held Duo login by urgid.",
            "params": [
                {"name": "urgid", "type": "string", "required": True},
            ],
        },
        {
            "name": "deny",
            "tool": "2fa_deny",
            "description": "Deny a held Duo login by urgid.",
            "params": [
                {"name": "urgid", "type": "string", "required": True},
            ],
        },
        {
            "name": "approve_all",
            "tool": "2fa_approve_all",
            "description": (
                "Open a 5-minute approve-all window and clear every held login."
            ),
        },
    ],
    "emitters": [],
    "sessions": [],
}


async def _h_burst(args: dict[str, Any]) -> Any:
    return await _svc.start_burst(
        args.get("window"), args.get("interval"), args.get("exit_on_approve"))


HANDLERS = {
    "ping": lambda args: _svc.ping(),
    "status": lambda args: _svc.status(),
    "pending": lambda args: _svc.pending(),
    "activate": lambda args: _svc.activate(args["code"]),
    "burst": _h_burst,
    "approve": lambda args: _svc.approve(args["urgid"]),
    "deny": lambda args: _svc.deny(args["urgid"]),
    "approve_all": lambda args: _svc.approve_all(),
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
