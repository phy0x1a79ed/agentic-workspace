"""Hub adapter for the penpot-plugins service.

Boots as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve →
reconnect). The gateway injects ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — there is no token.

Two registrations, one process, the ``fileviewer``/``drawio`` pattern:

* the ``ServiceAdapter`` control WS (``kind=service`` at
  ``/svc/penpot-plugins``) buys **supervision + a status surface**;
* a separate ``kind=static`` **mount** at ``/penpot-plugins``
  (``mount.hold_mount``, launched as a background task from ``on_start``) is
  what actually serves the plugin bytes. The control WS does not cover
  mounts, so the mount runs its own register/hold-lease/reconnect loop.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.penpot_plugins.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.penpot_plugins import mount

log = logging.getLogger("awm.penpot_plugins.hub_adapter")


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "penpot_plugins_status",
            "description": (
                "Report the penpot-plugins static mount: its origin-relative "
                "prefix (default /penpot-plugins), the local/ tree it "
                "exposes, which plugin folders it found (each one that carries "
                "a manifest.json), and whether the mount lease is currently "
                "held. A plugin's install URL is "
                "<prefix>/<name>/manifest.json — paste it into Penpot's "
                "in-app Plugin Manager."
            ),
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [],
}


def _h_status(_args: dict) -> dict:
    return mount.status()  # never raises, even mid-reconnect


HANDLERS = {
    "status": _h_status,
}


def _on_start() -> None:
    """Launch the static-mount register/hold-lease loop as a background task.

    ``hold_mount`` never returns, so it must be a task, not awaited — awaiting
    it would keep the service permanently mid-initialisation and the gateway
    would eventually reap it as unready (see AGENTS.md's ready-ASAP contract).
    """
    asyncio.create_task(mount.hold_mount())
    log.info("penpot-plugins mount task launched (%s → %s)",
              mount.MOUNT_PREFIX, mount.local_root())


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "penpot-plugins",
        API_MANIFEST,
        HANDLERS,
        on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
