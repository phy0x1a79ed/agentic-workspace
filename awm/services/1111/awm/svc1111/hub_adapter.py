"""Hub adapter for the `1111` service.

Supervises a stable-diffusion-webui process and its gallery viewer, both
living entirely under a separate, permission-locked OS account (see
``control.py`` for why and how). This service owns lifecycle only —
start/stop/status/restart — never the filesystem the processes run from.

The webui's own HTTP/WS traffic is not proxied through this service's RPC
surface (a bespoke protocol is the wrong tool for an entire Gradio app).
Instead, `on_start` spawns supervised background tasks (`register.py`) that
hold the ordinary external `kind=url` registrations' WS leases for as long as
this process lives — the same POST-then-hold-lease mechanism `awm gateway
register --url ... --prefix /1111` does interactively, done here so the
registration survives restarts without a human leaving a terminal open.
Deliberately no `kind=page` anywhere, so neither `/1111` nor `/1111-view`
appears in `/ui/*` or any page listing.

The viewer is cheap and needs no GPU, so a keeper loop keeps it up whether or
not the webui runs, until someone calls `view_stop`.

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

VIEW_CHECK_S = 60
_view_wanted = True


async def _keep_view_up() -> None:
    while True:
        if _view_wanted and not (await asyncio.to_thread(control.view_status))["running"]:
            result = await asyncio.to_thread(control.view_start)
            log.info("viewer was down; view-start rc=%s %s", result["rc"], result.get("stdout", ""))
        await asyncio.sleep(VIEW_CHECK_S)


def _set_view_wanted(wanted: bool, action):
    def handler(args):
        global _view_wanted
        _view_wanted = wanted
        return action()
    return handler


async def _on_start() -> None:
    """Spawn the lease holders and the viewer keeper, then return
    immediately (see the ready-ASAP contract in AGENTS.md)."""
    spawn_supervised("1111:url-registration", lambda: register.hold_registration(
        "1111", "/1111", control.WEBUI_PORT))
    spawn_supervised("1111:view-registration", lambda: register.hold_registration(
        "1111-view", "/1111-view", control.VIEW_PORT))
    spawn_supervised("1111:view-keeper", _keep_view_up)


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
        _fn("view_start", "Start the gallery viewer and keep it running."),
        _fn("view_stop", "Stop the gallery viewer and stop restarting it."),
        _fn("view_restart", "Restart the gallery viewer."),
        _fn("view_status", "Report whether the gallery viewer is running."),
    ],
    "emitters": [],
    "sessions": [],
}

HANDLERS = {
    "start": lambda args: control.start(),
    "stop": lambda args: control.stop(),
    "restart": lambda args: control.restart(),
    "status": lambda args: control.status(),
    "view_start": _set_view_wanted(True, control.view_start),
    "view_stop": _set_view_wanted(False, control.view_stop),
    "view_restart": _set_view_wanted(True, control.view_restart),
    "view_status": lambda args: control.view_status(),
}


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("1111", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
