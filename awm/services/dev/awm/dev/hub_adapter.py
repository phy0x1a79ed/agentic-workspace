"""Hub adapter for the `dev` service.

Boots `dev` as a gateway-registered service. Its functions (start/stop/restart/
seed/status) are published as ``dev_<fn>`` and shell out to the named
worktree's ``awm/services/dev/sandbox/run.sh`` (see ``dev.py``).

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.dev.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.dev import dev

log = logging.getLogger("awm.dev.hub_adapter")

_WORKTREE = {"name": "worktree", "type": "string", "required": True,
             "description": "Absolute path of the worktree whose sandbox to drive, under the workspace's projects/."}


def _fn(name: str, description: str, effect: str = "write") -> dict[str, Any]:
    return {"name": name, "tool": f"dev_{name}", "description": description,
            "params": [_WORKTREE], "effect": effect, "timeout": 120}


API_MANIFEST: dict[str, Any] = {
    "functions": [
        _fn("start", "Start the worktree's dev sandbox: its Rust gateway on the "
                     "worktree's port band, which starts the worktree's services."),
        _fn("stop", "Stop the worktree's dev sandbox (gateway and services)."),
        _fn("restart", "Restart the worktree's dev sandbox."),
        _fn("seed", "(Re)seed the worktree's dev sandbox without restarting."),
        _fn("status", "Show what the worktree's dev sandbox is running.", "read"),
    ],
    "emitters": [],
    "sessions": [],
}

HANDLERS = {
    action: (lambda args, action=action: dev.run_lifecycle(action, args.get("worktree", "")))
    for action in dev.LIFECYCLE_ACTIONS
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("dev", API_MANIFEST, HANDLERS).run()


if __name__ == "__main__":
    asyncio.run(main())
