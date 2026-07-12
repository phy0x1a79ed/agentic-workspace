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
    "functions": [
        {
            "name": "connect",
            "tool": "ssh_connect",
            "description": (
                "Open a ControlMaster SSH connection to a managed host. "
                "Idempotent — if already connected it does an auth-free liveness "
                "check and returns without a new login. Orchestrates VPN + 2FA "
                "burst automatically for hosts that require them, deduplicating "
                "concurrent requests into a single attempt. Blocks until the "
                "ControlMaster socket is live (up to 120s for cold VPN + 2FA), "
                "then returns success. Returns status 'unavailable' (no login "
                "attempted) if the host is held by the circuit breaker after a "
                "prior failure — recovery is via a Discord /approve. Known hosts: "
                + ", ".join(sorted(KNOWN_HOSTS))
            ),
            "params": [
                {"name": "host", "type": "string", "required": True},
            ],
            "timeout": 120,
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
                "List all managed hosts and their connection state. Reports an "
                "in-memory snapshot of each host's lifecycle state (plus a breaker "
                "lockfile check) — it does NOT probe the network. Per-host status "
                "is one of connected / connecting / disconnecting / disconnected, "
                "or 'unavailable' when the host is held by the circuit breaker. A "
                "host whose master died out-of-band may still read 'connected' "
                "until the next connect re-probes it."
            ),
            "params": [],
        },
        {
            "name": "notify_test",
            "tool": "ssh_notify_test",
            "description": (
                "Fire the Discord lock-alert wire on demand to confirm the ssh "
                "service can reach the operator BEFORE a real lockout depends on "
                "it. Sends through the exact social→Discord path a real breaker "
                "trip uses (same peer selector, edge, bearer, channel) but writes "
                "NO lockfile and mutates no state, and surfaces the send result "
                "instead of swallowing — so a broken notify wire fails loudly. The "
                "message is clearly a test and requests no /approve action."
            ),
            "params": [
                {"name": "host", "type": "string", "required": False},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}


HANDLERS = {
    "connect": lambda args: svc.connect(args["host"]),
    "disconnect": lambda args: svc.disconnect(args["host"]),
    "status": lambda _args: svc.status(),
    "notify_test": lambda args: svc.notify_test(args.get("host", "[selftest]")),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "ssh", API_MANIFEST, HANDLERS, on_start=svc.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
