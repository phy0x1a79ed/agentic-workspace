"""Hub adapter for the httpsfront service — the whole-gateway HTTPS front.

Boots as a gateway-registered process on the shared
``awm.gatewayclient.ServiceAdapter`` loop (register → ready → serve →
reconnect). The gateway injects ``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` /
``AWM_SERVICE_ID`` — no token.

What the gateway registration buys here is **supervision + a status surface**,
NOT the traffic. The TLS reverse proxy rides a self-contained off-host HTTPS
listener (``proxy``), launched in a daemon thread from ``on_start``, because the
loopback-only hub can't serve a secure context to a phone/laptop. When the
gateway drains/respawns, this process exits and the listener dies with it — one
supervised lifetime (exactly like ``mic``).

The proxy target is ``AWM_HUB_URL`` (the very gateway that spawned us), so a dev
sandbox front proxies its sandbox, and prod's proxies prod — no hardcoded port.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.httpsfront.hub_adapter

Functions:
  - status  (tool ``httpsfront_status``) — listener port, TLS state, SAN set,
            the upstream it fronts, and the /ca.crt URL shape.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.httpsfront import certs, proxy, store

log = logging.getLogger("awm.httpsfront.hub_adapter")

HERE = Path(__file__).resolve().parent          # awm/services/httpsfront/awm/httpsfront
SERVICE_DIR = HERE.parents[1]                    # awm/services/httpsfront
CERT_DIR = SERVICE_DIR / ".certs"
SANS_FILE = SERVICE_DIR / ".sans"                # host-specific extra SANs (gitignored)

PORT = int(os.environ.get("AWM_HTTPS_PORT", "8443"))
UPSTREAM = os.environ.get("AWM_HUB_URL", "http://127.0.0.1:7819/")

# Live status, filled once the listener comes up.
_STATUS: dict[str, Any] = {
    "listener_port": PORT,
    "tls": False,
    "san": None,
    "upstream": UPSTREAM,
    "serving": False,
}


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "description": (
                "Report the HTTPS front's listener port, TLS state, the SAN set "
                "of the leaf cert, the loopback gateway it fronts, and the "
                "/ca.crt URL shape for installing the root CA on a device."
            ),
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [],
}


def _h_status(args: dict) -> dict:
    st = dict(_STATUS)
    host = None
    for tok in certs.default_sans():
        if tok.startswith("IP:") and not tok.endswith("127.0.0.1"):
            host = tok.split(":", 1)[1]
            break
    st["ca_url"] = (
        f"https://{host or '<host-ip>'}:{st['listener_port']}/ca.crt"
    )
    st["url_shape"] = f"https://{host or '<host-ip>'}:{st['listener_port']}/ui/notes/"
    return st


HANDLERS = {"status": _h_status}


def _serve_forever(info: dict) -> None:
    """Run the TLS reverse proxy, restarting it on crash. Lives in a daemon
    thread for the life of the process (= the life of the gateway lease)."""
    while True:
        try:
            _STATUS.update(serving=True, tls=True, san=info["san"])
            proxy.serve(
                port=PORT,
                cert=info["cert"],
                key=info["key"],
                ca=info["ca"],
                upstream=UPSTREAM,
            )
        except Exception:  # noqa: BLE001
            log.exception("https front listener crashed; restarting in 2s")
            _STATUS.update(serving=False)
            time.sleep(2)


def _on_start() -> None:
    """Mint/reuse the TLS cert, then launch the HTTPS front in a daemon thread.

    Runs once, alongside registration. A cert failure is fatal since
    the listener can't come up without TLS.
    """
    store.init()

    sans = certs.resolve_sans(san_file=SANS_FILE)
    info = certs.ensure_certs(CERT_DIR, sans=sans)
    log.info("certs ready (SAN=%s)", info["san"])

    t = threading.Thread(
        target=_serve_forever, args=(info,), daemon=True, name="httpsfront"
    )
    t.start()
    log.info("https front thread launched on :%d → %s (tls on)", PORT, UPSTREAM)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "httpsfront",
        API_MANIFEST,
        HANDLERS,
        on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
