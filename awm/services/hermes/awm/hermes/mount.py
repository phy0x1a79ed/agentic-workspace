"""The dashboard's public mount: a ``kind=url`` record at ``/ui/hermes``.

``claude-science`` puts its workbench on the mesh behind a dedicated TLS front
because the hub's url proxy strips ``Cookie``, forwards no headers at all on a
WebSocket upgrade, and comma-collapses ``Set-Cookie``. None of those bind here,
and the difference is worth stating because the conclusion looks contradictory
next to that service's:

  - the dashboard binds loopback, where its auth gate is off and the SPA
    authenticates with a token injected into ``index.html``;
  - its REST calls carry that token in ``X-Hermes-Session-Token``, not in the
    stripped ``Authorization``;
  - its WebSockets carry it as a ``?token=`` query parameter, which survives a
    header-less upgrade.

So the only thing missing was the path. The proxy forwards ``request.url.path``
verbatim, and the dashboard serves at the root — hence the registration's
``strip_prefix``, which peels the mount prefix off and sends it back as
``X-Forwarded-Prefix``. The dashboard reads exactly that header to set
``window.__HERMES_BASE_PATH__``, which the SPA prepends to every ``/api/…``
URL it builds. Re-check this the day the dashboard is bound non-loopback: at
that point it fails closed and starts using cookies, and the claude-science
verdict applies again.

The mount dies with this service. The dashboard does not.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx
import websockets

from awm.hermes import daemon

log = logging.getLogger("awm.hermes.mount")

PREFIX = os.environ.get("HERMES_MOUNT_PREFIX", "/ui/hermes")

_STATE: dict[str, Any] = {"prefix": PREFIX, "mounted": False, "error": None}


def status() -> dict[str, Any]:
    return dict(_STATE)


def public_url() -> str:
    """Where a browser holding an awm session reaches the dashboard."""
    edge = os.environ.get("AWM_EDGE_URL", "").rstrip("/")
    return f"{edge}{PREFIX}/" if edge else f"{PREFIX}/"


async def hold() -> None:
    """Register the mount and hold its lease, re-registering across faults.

    Registry records are in-memory, so a gateway restart drops the mount; the
    loop puts it back. Never returns — ``spawn_supervised`` treats a return as
    a defect, which is the right reading for this.
    """
    hub_url = os.environ.get("AWM_HUB_URL", "").rstrip("/")
    if not hub_url:
        _STATE["error"] = "AWM_HUB_URL unset; not mounted"
        log.error("hermes mount: AWM_HUB_URL not set; cannot register")
        await asyncio.Event().wait()
    ws_base = hub_url.replace("https://", "wss://").replace("http://", "ws://")
    target = f"http://127.0.0.1:{daemon.PORT}"

    backoff = 1.0
    while True:
        try:
            async with httpx.AsyncClient(verify=False, timeout=15) as cli:
                r = await cli.post(f"{hub_url}/hub/register", json={
                    "name": "hermes-ui",
                    "prefix": PREFIX,
                    "url": target,
                    "strip_prefix": True,
                })
                r.raise_for_status()
            body = r.json()
            if not body.get("strip_prefix"):
                # An older gateway accepted the registration and ignored the
                # flag. Say so: the mount will 404 on every request and the
                # cause is invisible from the browser.
                _STATE["error"] = (
                    "gateway accepted the mount without strip_prefix — it "
                    "predates the flag; the dashboard will 404 under the prefix"
                )
                log.error("hermes mount: %s", _STATE["error"])
            else:
                _STATE["error"] = None
            _STATE["mounted"] = True
            log.info("hermes mount: %s → %s (id=%s)", PREFIX, target,
                     body.get("service_id"))
            backoff = 1.0
            async with websockets.connect(
                f"{ws_base}{body['lease_ws_path']}",
                max_size=None, open_timeout=10,
            ) as ws:
                # First frame is "ready"; then hold the socket open. Closing it
                # is what evicts the mount.
                async for _ in ws:
                    pass
        except Exception as exc:  # noqa: BLE001 — stay up across any fault
            log.warning("hermes mount lost (%s); retrying in %.1fs", exc, backoff)
            _STATE["error"] = str(exc)
        finally:
            _STATE["mounted"] = False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)
