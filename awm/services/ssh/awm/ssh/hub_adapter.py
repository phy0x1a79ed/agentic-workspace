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
                "Idempotent — returns immediately if already connected. "
                "Orchestrates VPN + 2FA burst automatically for hosts "
                "that require them. Blocks until the ControlMaster socket "
                "is live (up to 120s for cold VPN + 2FA), then returns success. "
                "Known hosts: "
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
                "Close the ControlMaster SSH connection to a host. "
                "Cancels any in-progress connect for that host. "
                "Waits for the control socket to be removed."
            ),
            "params": [
                {"name": "host", "type": "string", "required": True},
            ],
        },
        {
            "name": "status",
            "tool": "ssh_status",
            "description": (
                "List all managed hosts and their connection state. "
                "Scans ~/.ssh/live_connections/ for active ControlMaster "
                "sockets and reports connected/connecting/disconnected "
                "per host."
            ),
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [],
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
        "ssh", API_MANIFEST, HANDLERS, on_start=svc.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
