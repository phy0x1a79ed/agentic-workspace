"""Hub adapter for the fileviewer service.

Boots fileviewer as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve →
reconnect). The gateway injects ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` / ``AWM_HUB_TOKEN_FILE``.

The process runs a loopback HTTP listener (``server.start``) and registers its
URL as the service's ``mount``. The gateway serves it at ``/s/fileviewer/…``, so
links are origin-relative and reach any device that can open the notes page.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.fileviewer.hub_adapter

Functions:
  - status  (tool ``fileviewer_status``) — mount prefix, exposed root, mask size,
            listener URL, and the origin-relative URL shape.
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
                "Report the fileviewer mount: its origin-relative prefix "
                "(/s/fileviewer), the filesystem root it exposes, whether the "
                "listener is up, and how many mask (deny) globs are hiding "
                "secrets. The URL shape is /s/fileviewer/<absolute-path> — an "
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
    return server.status()


HANDLERS = {
    "status": _h_status,
}


# -- boot -------------------------------------------------------------------


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    srv = server.start()
    await ServiceAdapter(
        "fileviewer",
        API_MANIFEST,
        HANDLERS,
        mount=srv.url,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
