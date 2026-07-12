"""Hub adapter for the `dev` service.

Boots `dev` as a gateway-registered service. Its functions (start/stop/restart/
seed/status) are projected into the catalog as ``dev_<fn>`` tools and shell out
to the worktree's ``awm/gateway/dev/run.sh`` (see ``dev.py``).

Self-shadow (UNIQUE to this service): if the gateway that spawned us set
``AWM_SHADOW_HUB_URL`` (the dev sandbox does; prod does not), we pass it to
``ServiceAdapter.run(shadow_hub_url=…)``, which — in addition to the normal base
registration on our own hub — registers a self-cleaning *overlay* of ``/svc/dev``
onto that hub (prod). So ``awm dev <op>`` aimed at prod is served by the live
sandbox's worktree code, and stopping the sandbox drops the overlay and hands
``/svc/dev`` back to prod's base. Only `dev` opts into this; feature services
call ``run()`` with no shadow target.

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


def _fn(name: str, description: str) -> dict[str, Any]:
    return {"name": name, "tool": f"dev_{name}", "description": description,
            "params": []}


API_MANIFEST: dict[str, Any] = {
    "functions": [
        _fn("start", "Start this worktree's dev sandbox (gateway + bootstrapped "
                     "services). Served by the sandbox's dev overlay when one is "
                     "live; inert on the prod base."),
        _fn("stop", "Stop this worktree's dev sandbox (services + gateway)."),
        _fn("restart", "Restart this worktree's dev sandbox."),
        _fn("seed", "(Re)seed the dev sandbox DB without restarting."),
        _fn("status", "Show what this worktree's dev sandbox is running."),
    ],
    "emitters": [],
    "sessions": [],
}

HANDLERS = {
    "start": lambda args: dev.run_lifecycle("start"),
    "stop": lambda args: dev.run_lifecycle("stop"),
    "restart": lambda args: dev.run_lifecycle("restart"),
    "seed": lambda args: dev.run_lifecycle("seed"),
    "status": lambda args: dev.run_lifecycle("status"),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("dev", API_MANIFEST, HANDLERS).run(
        shadow_hub_url=dev.SHADOW_HUB_URL or None,
    )


if __name__ == "__main__":
    asyncio.run(main())
