"""Hub adapter for the `1111` service.

Supervises a stable-diffusion-webui process that lives entirely under a
separate, permission-locked OS account (see ``control.py`` for why and how).
This service owns lifecycle only — start/stop/status/restart — never the
filesystem the process runs from.

The webui's own HTTP/WS traffic is not proxied through this service's RPC
surface (a bespoke protocol is the wrong tool for an entire Gradio app).
Instead, `on_start` spawns a supervised background task (`register.py`) that
holds the ordinary external `kind=url` registration's WS lease for as long as
this process lives — the same POST-then-hold-lease mechanism `awm gateway
register --url ... --prefix /1111` does interactively, done here so the
registration survives restarts without a human leaving a terminal open.
Deliberately no `kind=page` anywhere, so `/1111` never appears in `/ui/*` or
any page listing.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.svc1111.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.svc1111 import control, register

log = logging.getLogger("awm.svc1111.hub_adapter")


async def _on_start() -> None:
    """Fire the URL-registration loop in the background and return
    immediately — on_start must not block on something meant to run forever
    (see the ready-ASAP contract in AGENTS.md)."""
    spawn_supervised("1111:url-registration", register.hold_registration)


def _fn(name: str, description: str) -> dict[str, Any]:
    return {"name": name, "tool": f"1111_{name}", "description": description,
            "params": []}


API_MANIFEST: dict[str, Any] = {
    "description": (
        "Lifecycle control only. Runs the webui as a separate OS account via "
        "a narrow sudo bridge; this service has no read access to that "
        "account's files."
    ),
    "functions": [
        _fn("start", "Start the webui process."),
        _fn("stop", "Stop the webui process."),
        _fn("restart", "Restart the webui process."),
        _fn("status", "Report whether the webui process is running."),
    ],
    "emitters": [],
    "sessions": [],
}

HANDLERS = {
    "start": lambda args: control.start(),
    "stop": lambda args: control.stop(),
    "restart": lambda args: control.restart(),
    "status": lambda args: control.status(),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("1111", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
