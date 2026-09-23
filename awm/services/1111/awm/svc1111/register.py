"""Hold the gateway's `kind=url` registration for the webui, for this
process's whole lifetime.

`awm gateway register --url ... --prefix ...` (see `awm/gateway/awm/gateway/cli.py`)
is a foreground command: it POSTs `/hub/register` once and then holds a
websocket lease open until interrupted — on disconnect the hub evicts the
registration. That's fine for a human at a terminal; it is the wrong shape
for a registration that has to survive a gateway restart. So this service
does the same POST-then-hold-the-lease dance itself, from a supervised
background task (`gatewayclient.spawn_supervised`), so the prefix comes back
automatically whenever this service (re)starts — no cron, no manual
`gateway register` left running in a terminal somewhere.

Deliberately no `kind=page` registration anywhere in this module — that's
what keeps `/1111` out of `/ui/*`, `/tools`, and any page listing. Only `awm
gateway list` shows it.
"""

from __future__ import annotations

import logging
import os

import httpx
import websockets

from awm.svc1111.control import WEBUI_PORT

log = logging.getLogger("awm.svc1111.register")

SERVICE_NAME = "1111"
PREFIX = "/1111"


def _ws_base(hub_url: str) -> str:
    return hub_url.replace("https://", "wss://").replace("http://", "ws://")


async def hold_registration() -> None:
    """One register-then-hold cycle. Raises/returns on disconnect so
    `spawn_supervised` re-enters and re-registers from scratch."""
    hub_url = os.environ.get("AWM_HUB_URL", "").rstrip("/")
    if not hub_url:
        raise RuntimeError("AWM_HUB_URL not set; cannot register the URL prefix")

    payload = {
        "name": SERVICE_NAME,
        "prefix": PREFIX,
        "url": f"http://127.0.0.1:{WEBUI_PORT}",
        "strip_prefix": False,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{hub_url}/hub/register", json=payload)
    if resp.status_code >= 400:
        # A 409 here almost always means a previous lease from this same
        # process is still draining after a fast restart — let the caller's
        # respawn backoff ride it out rather than raising a scary traceback.
        log.warning("register %s failed (%s): %s", PREFIX, resp.status_code, resp.text)
        raise RuntimeError(f"register failed: {resp.status_code} {resp.text}")

    body = resp.json()
    lease_path = body["lease_ws_path"]
    ws_url = f"{_ws_base(hub_url)}{lease_path}"

    async with websockets.connect(ws_url, max_size=None, open_timeout=10) as ws:
        first = await ws.recv()
        log.info("1111: url registration held at %s (%s)", PREFIX, first)
        async for _ in ws:
            pass
    log.warning("1111: url registration lease closed; will re-register")
