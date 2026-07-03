"""Hub adapter for the fileviewer service.

Boots fileviewer as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve →
reconnect). The gateway injects only ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — there is no token.

What the gateway registration buys here is **supervision + a status surface**,
NOT the file transport. The hub function channel is JSON-only — a handler's
return value is always JSON-serialized — so it can't hand a browser raw file
bytes with a real ``Content-Type``. So the file bytes ride a self-contained
loopback HTTP listener (``server``), launched in a daemon thread from
``on_start``. When the gateway drains/respawns, this process exits and the
listener dies with it — one supervised lifetime, exactly like ``mic``.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.fileviewer.hub_adapter

Functions (the file bytes never touch the hub):
  - status  (tool ``fileviewer_status``) — listener bind address, port,
            liveness, and request count.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.fileviewer import server

log = logging.getLogger("awm.fileviewer.hub_adapter")

PORT = int(os.environ.get("FILEVIEWER_PORT", "12210"))
BIND = os.environ.get("FILEVIEWER_BIND", "127.0.0.1")


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "fileviewer_status",
            "description": (
                "Report the fileviewer listener's bind address, port, "
                "liveness, and served-request count. The URL shape is "
                "http://<bind>:<port>/?path=/absolute/path/to/file — point it "
                "at any readable file and the browser renders it (SVG/HTML/"
                "images/text); a bad path returns a 404 not-found page."
            ),
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- function handlers ------------------------------------------------------


def _h_status(_args: dict) -> dict:
    # Must never raise even if the listener thread died — report last-known.
    return server.status()


HANDLERS = {
    "status": _h_status,
}


# -- startup orchestration --------------------------------------------------


def _serve_forever() -> None:
    """Run the file-serving listener, restarting it on crash. Lives in a
    daemon thread for the life of the process (= the life of the gateway
    lease), so a gateway drain/respawn kills it cleanly with the process."""
    while True:
        try:
            server.serve(port=PORT, bind=BIND)
        except Exception:  # noqa: BLE001
            log.exception("fileviewer listener crashed; restarting in 2s")
            time.sleep(2)


def _on_start() -> None:
    """Launch the loopback file-serving listener in a daemon thread.

    Runs once before the first control-WS connect. Doesn't block the adapter's
    async loop — the serving call lives entirely in the thread.
    """
    t = threading.Thread(target=_serve_forever, daemon=True, name="fileviewer")
    t.start()
    log.info("fileviewer listener thread launched on %s:%d", BIND, PORT)


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
