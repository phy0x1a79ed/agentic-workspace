"""Hub adapter for the fileviewer service.

Boots fileviewer as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve →
reconnect). The gateway injects ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — there is no token.

Two registrations, one process:

* the ``ServiceAdapter`` control WS (``kind=service`` at ``/svc/fileviewer``)
  buys **supervision + a status surface** — the gateway respawns the process and
  exposes ``fileviewer_status``;
* a separate ``kind=static`` **mount** at ``/files`` (``server.hold_mount``,
  launched as a background task from ``on_start``) is what actually serves the
  file bytes, through the gateway's ``serve_static`` + the HTTPS front. The
  control WS does not cover the mount, so the mount runs its own
  register/hold-lease/reconnect loop (see ``server``).

This retires the old bespoke loopback ``http.server`` listener: bytes now ride
the gateway the standard awm way, so links are origin-relative (``/files/…``) and
reach any device that can open the notes page — not just the server's loopback.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.fileviewer.hub_adapter

Functions:
  - status  (tool ``fileviewer_status``) — mount prefix, exposed root, mask size,
            liveness, and the origin-relative URL shape.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.fileviewer import server

log = logging.getLogger("awm.fileviewer.hub_adapter")


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "fileviewer_status",
            "description": (
                "Report the fileviewer static mount: its origin-relative prefix "
                "(default /files), the filesystem root it exposes, whether the "
                "mount lease is currently held, and how many mask (deny) globs "
                "are hiding secrets. The URL shape is /files/<absolute-path> — an "
                "origin-relative link that the gateway serves with a correct "
                "Content-Type through the HTTPS front, so it renders on any "
                "device viewing the notes page. Masked or missing paths 404."
            ),
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- function handlers ------------------------------------------------------


def _h_status(_args: dict) -> dict:
    # Must never raise even if the mount loop is mid-reconnect — report last-known.
    return server.status()


HANDLERS = {
    "status": _h_status,
}


# -- startup orchestration --------------------------------------------------


def _on_start() -> None:
    """Launch the static-mount register/hold-lease loop as a background task.

    Runs once inside the adapter's event loop, alongside the control-WS
    registration. ``hold_mount`` never returns, so it must be a task, not awaited —
    awaiting it would keep the service permanently mid-initialisation."""
    asyncio.create_task(server.hold_mount())
    log.info("fileviewer static mount task launched (%s → %s)",
             server.MOUNT_PREFIX, server.MOUNT_ROOT)


# -- boot -------------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "fileviewer",
        API_MANIFEST,
        HANDLERS,
        on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
