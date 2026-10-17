"""Hub adapter for the engcyc service — the EngCyc portal behind a reverse proxy.

Registers on the shared ``ServiceAdapter`` loop and supervises one gunicorn
server (see ``portal``). A node with no portal checkout registers idle and says
why in ``status``, so a fleet-wide install leaves every other node untouched.

Run via ``run.sh``:
    python -m awm.engcyc.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.engcyc import portal

log = logging.getLogger("awm.engcyc.hub_adapter")

SUPERVISOR = portal.Supervisor()

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "effect": "read",
            "tool": "engcyc_status",
            "description": (
                "Report the EngCyc portal: why it cannot run here if it cannot, "
                "whether gunicorn is running and at what pid and bind address, "
                "and whether the app answers its field-list request."
            ),
            "params": [],
        },
        {
            "name": "start",
            "effect": "write",
            "tool": "engcyc_start",
            "description": "Start the portal and wait until it answers. Idempotent.",
            "params": [],
            "timeout": 120,
        },
        {
            "name": "stop",
            "effect": "write",
            "tool": "engcyc_stop",
            "description": (
                "Stop the portal. The supervision loop leaves it stopped until "
                "start is called again. Idempotent."
            ),
            "params": [],
            "timeout": 60,
        },
        {
            "name": "restart",
            "effect": "write",
            "tool": "engcyc_restart",
            "description": "Stop the portal and start it again, e.g. to reload meta.tsv.",
            "params": [],
            "timeout": 180,
        },
        {
            "name": "logs",
            "effect": "read",
            "tool": "engcyc_logs",
            "description": "Tail gunicorn's server log.",
            "params": [
                {"name": "tail", "type": "number",
                 "description": "Lines to return (default 200)."},
            ],
        },
    ],
    "emitters": [],
    "sessions": [],
}


async def _h_status(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.snapshot)


async def _h_start(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.start)


async def _h_stop(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.stop)


async def _h_restart(args: dict) -> dict:
    return await asyncio.to_thread(SUPERVISOR.restart)


async def _h_logs(args: dict) -> dict:
    tail = int(args.get("tail") or 200)
    return {"tail": tail, "log": await asyncio.to_thread(SUPERVISOR.logs, tail)}


HANDLERS = {
    "status": _h_status,
    "start": _h_start,
    "stop": _h_stop,
    "restart": _h_restart,
    "logs": _h_logs,
}


async def _health_loop() -> None:
    """Respawn gunicorn whenever it has died. Never exits."""
    while True:
        try:
            await asyncio.sleep(portal.HEALTH_INTERVAL_S)
            res = await asyncio.to_thread(SUPERVISOR.reconcile)
            if res.get("action") != "none":
                log.warning("engcyc: %s", res)
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("engcyc: supervision pass failed")


async def _on_start() -> None:
    why = portal.installed()
    if why:
        log.info("engcyc: idle, %s", why)
        return
    try:
        snap = await asyncio.to_thread(SUPERVISOR.start)
        log.info("engcyc: pid=%s bind=%s probe=%s",
                 snap.get("pid"), snap.get("bind"), snap.get("probe"))
    except Exception:  # noqa: BLE001
        log.exception("engcyc: initial start failed; the loop will retry")
    spawn_supervised("engcyc:health", _health_loop)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("engcyc", API_MANIFEST, HANDLERS, on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())
