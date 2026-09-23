"""Hold the gateway's `kind=url` registrations for the webui and the gallery
viewer, for this process's whole lifetime.

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
what keeps `/1111` and `/1111-view` out of `/ui/*`, `/tools`, and any page listing. Only `awm
gateway list` shows it.
"""

from __future__ import annotations

import logging
import os

import httpx
import websockets

log = logging.getLogger("awm.svc1111.register")


def _ws_base(hub_url: str) -> str:
    return hub_url.replace("https://", "wss://").replace("http://", "ws://")


async def hold_registration(name: str, prefix: str, port: int) -> None:
    """One register-then-hold cycle. Raises/returns on disconnect so
    `spawn_supervised` re-enters and re-registers from scratch."""
    hub_url = os.environ.get("AWM_HUB_URL", "").rstrip("/")
    if not hub_url:
        raise RuntimeError(f"AWM_HUB_URL not set; cannot register {prefix}")

    payload = {
        "name": name,
        "prefix": prefix,
        "url": f"http://127.0.0.1:{port}",
        # Both upstreams serve at their own root — without this the gateway
        # forwards the full "/<prefix>/..." path and every request 404s.
        "strip_prefix": True,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{hub_url}/hub/register", json=payload)
    if resp.status_code >= 400:
        # A 409 here almost always means a previous lease from this same
        # process is still draining after a fast restart — let the caller's
        # respawn backoff ride it out rather than raising a scary traceback.
        log.warning("register %s failed (%s): %s", prefix, resp.status_code, resp.text)
        raise RuntimeError(f"register failed: {resp.status_code} {resp.text}")

    body = resp.json()
    lease_path = body["lease_ws_path"]
    ws_url = f"{_ws_base(hub_url)}{lease_path}"

    async with websockets.connect(ws_url, max_size=None, open_timeout=10) as ws:
        first = await ws.recv()
        log.info("url registration held at %s (%s)", prefix, first)
        async for _ in ws:
            pass
    log.warning("url registration lease for %s closed; will re-register", prefix)
