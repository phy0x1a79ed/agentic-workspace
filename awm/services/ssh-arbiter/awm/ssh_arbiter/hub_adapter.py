"""The ``ssh-arbiter`` singleton. A requester holds an open ``lease`` session for one login."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter
from awm.ssh_arbiter.service import SSHArbiter

svc = SSHArbiter()

API_MANIFEST: dict[str, Any] = {
    "description": "The fleet's single-attempt arbiter for lockout-sensitive ssh hosts.",
    "functions": [
        {
            "name": "status",
            "tool": "ssh-arbiter_status",
            "description": "Each held host, the open leases and /approve windows, and the /approve wire's health.",
            "params": [],
        },
        {
            "name": "notify_test",
            "tool": "ssh-arbiter_notify_test",
            "description": "Send a test page down the lock-alert wire. Writes no hold.",
            "params": [{"name": "host", "type": "string", "required": False}],
        },
        {
            "name": "receive_test",
            "tool": "ssh-arbiter_receive_test",
            "description": "Prove an operator /approve still reaches this service. Opens no window.",
            "params": [{"name": "timeout", "type": "number", "required": False}],
        },
    ],
    "emitters": [],
    "sessions": [{"kind": "lease", "transport": "direct"}],
}

HANDLERS = {
    "status": lambda _args: svc.status(),
    "notify_test": lambda args: svc.notify_test(args.get("host", "[selftest]")),
    "receive_test": lambda args: svc.receive_test(float(args.get("timeout") or 10.0)),
}


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await ServiceAdapter(
        "ssh-arbiter", API_MANIFEST, HANDLERS,
        session_handlers={"lease": svc.lease_session},
        on_start=svc.init,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
