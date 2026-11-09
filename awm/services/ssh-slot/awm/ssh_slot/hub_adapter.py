"""The ``ssh-slot`` singleton: grants the one in-flight login per lockout-sensitive host.

A requester holds an open ``lease`` session for one connect attempt. See
``SSHService._lease_session`` and FEDERATION.md § Connection-slot arbiter.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.ssh.service import SSHService

svc = SSHService()

API_MANIFEST: dict[str, Any] = {
    "description": "The fleet's single-attempt arbiter for lockout-sensitive ssh hosts.",
    "functions": [
        {
            "name": "status",
            "tool": "ssh_slot_status",
            "description": "Each host's hold state and the open /approve windows.",
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [{"kind": "lease", "transport": "direct"}],
}

HANDLERS = {"status": lambda _args: svc.status()}


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await ServiceAdapter(
        "ssh-slot", API_MANIFEST, HANDLERS,
        session_handlers={"lease": svc._lease_session},
        on_start=svc.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
