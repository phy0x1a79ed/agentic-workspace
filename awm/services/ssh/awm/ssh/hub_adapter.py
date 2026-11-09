from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.ssh.config import KNOWN_HOSTS
from awm.ssh.service import SSHService

log = logging.getLogger("awm.ssh.hub_adapter")

svc = SSHService()

API_MANIFEST: dict[str, Any] = {
    "description": (
        "SSH to the managed HPC hosts, and the only sanctioned way to reach "
        "them. `connect` arms the Duo approver itself, so do NOT call the 2fa "
        "domain around it — it is already armed, "
        "once, for the single login it is about to make. After connecting, plain "
        "`ssh <host> <cmd>` multiplexes over the ControlMaster with no new "
        "authentication. A host reported 'unavailable' is held after an earlier "
        "failure and only an operator's `/approve <device>` in Discord releases "
        "it: retrying cannot, and each retry that reaches a login spends an MFA "
        "attempt against a 10-strike account lockout."
    ),
    "functions": [
        {
            "name": "connect",
            "tool": "ssh_connect",
            "description": (
                "Open a ControlMaster SSH connection to a managed host. "
                "Idempotent — if already connected it does an auth-free liveness "
                "check and returns without a new login. Arms the 2FA burst "
                "automatically for hosts that require it, deduplicating "
                "concurrent requests into a single attempt — you do not need to "
                "call the 2fa domain, and doing so spends MFA budget this "
                "service is accounting for. Sockeye hosts jump through the UBC "
                "VPN tunnel that altair's vpn service keeps up; never call vpn "
                "for them. Blocks until the ControlMaster socket is live. A "
                "failure names what failed (the VPN tunnel, the Duo approver, "
                "the host). Returns status 'unavailable' (no login attempted) if "
                "the host is held by the circuit breaker after a prior failure — "
                "recovery is an operator /approve in Discord, never a retry. An "
                "error saying no MFA attempt was spent is safe to retry. "
                "Known hosts: "
                + ", ".join(sorted(KNOWN_HOSTS))
            ),
            "params": [
                {"name": "host", "type": "string", "required": True},
            ],
            # The ssh login (service._CONNECT_TIMEOUT), plus slack for a
            # lease queued behind another login.
            "timeout": 300,
        },
        {
            "name": "disconnect",
            "tool": "ssh_disconnect",
            "description": (
                "Close the ControlMaster SSH connection to a host. Does NOT abort "
                "an in-progress connect: if the host is mid-authentication the "
                "disconnect is deferred until that attempt resolves, then the "
                "connection is torn down. Waits for the control socket to be "
                "removed (reports a warning if the master may still be running)."
            ),
            "params": [
                {"name": "host", "type": "string", "required": True},
            ],
        },
        {
            "name": "status",
            "tool": "ssh_status",
            "description": (
                "List all managed hosts and their connection state on this node. "
                "Reports an in-memory snapshot of each host's lifecycle state and "
                "does NOT probe the network. Per-host status is one of connected / "
                "connecting / disconnecting / disconnected. A held host reads "
                "disconnected here, and `connect` reports it as 'unavailable'. A "
                "host whose master died out-of-band may still read 'connected' "
                "until the next connect re-probes it."
            ),
            "params": [],
        },
    ],
    "emitters": [],
}


HANDLERS = {
    "connect": lambda args: svc.connect(args["host"]),
    "disconnect": lambda args: svc.disconnect(args["host"]),
    "status": lambda _args: svc.status(),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "ssh", API_MANIFEST, HANDLERS,
        on_start=svc.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
