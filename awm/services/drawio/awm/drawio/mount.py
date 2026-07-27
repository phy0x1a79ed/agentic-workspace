"""Register the drawio web client as a gateway ``kind=static`` mount.

The editor is a ~150 MB tree of JavaScript, CSS, fonts and stencil images that
``install.sh`` clones and patches into ``webapp/``. Those bytes cannot ride an
RPC verb, so they get their own ``kind=static`` mount at ``/drawio-app`` — the
same two-registrations-one-process shape ``fileviewer`` uses.

The control WS does not cover mounts, so this runs its own register + hold-lease
+ reconnect loop. Gateway registry records are in-memory, so a gateway restart
drops the mount; without the reconnect loop the editor would silently 404 until
someone noticed and bounced the service.

Unlike fileviewer's mount this one exposes a fixed directory and needs no mask:
everything under ``webapp/`` is a published web asset by construction.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import threading
from pathlib import Path

import httpx
import websockets

log = logging.getLogger("awm.drawio.mount")

SERVICE_NAME = os.environ.get("AWM_SERVICE_NAME", "drawio")
MOUNT_PREFIX = os.environ.get("DRAWIO_MOUNT_PREFIX", "/drawio-app")


def app_root() -> Path:
    """Where ``install.sh`` puts the patched web client.

    Defaults to ``webapp/`` beside the service folder — gitignored, since it is
    a reproducible clone rather than source.
    """
    override = os.environ.get("DRAWIO_APP_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "webapp"


class _State:
    """Live mount status for the service-status verb (never raises)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mounted = False
        self.service_id: str | None = None
        self.reason: str = "not started"

    def snapshot(self) -> dict:
        with self.lock:
            root = app_root()
            return {
                "mounted": self.mounted,
                "prefix": MOUNT_PREFIX,
                "root": str(root),
                "installed": (root / "index.html").is_file(),
                "service_id": self.service_id,
                "reason": self.reason,
            }


STATE = _State()


def status() -> dict:
    return STATE.snapshot()


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # loopback, self-signed gateway cert
    return ctx


async def hold_mount() -> None:
    """Register the editor's static mount and hold its lease forever."""
    hub_url = os.environ.get("AWM_HUB_URL", "").rstrip("/")
    if not hub_url:
        with STATE.lock:
            STATE.reason = "AWM_HUB_URL not set"
        log.error("AWM_HUB_URL not set; drawio app mount cannot register")
        return

    root = app_root()
    if not (root / "index.html").is_file():
        # Not fatal: every verb still works, and an agent can build a diagram
        # headlessly. Only the browser editor is unavailable until install.sh
        # has run, and saying so plainly beats an unexplained 404.
        with STATE.lock:
            STATE.reason = f"web client not installed at {root}; run install.sh"
        log.warning("drawio web client missing at %s — editor unavailable "
                    "until install.sh runs; verbs are unaffected", root)
        return

    ws_base = hub_url.replace("https://", "wss://").replace("http://", "ws://")
    ssl_ctx = _ssl_ctx() if ws_base.startswith("wss://") else None

    backoff = 1.0
    while True:
        try:
            async with httpx.AsyncClient(verify=False, timeout=15) as cli:
                r = await cli.post(f"{hub_url}/hub/register", json={
                    "name": SERVICE_NAME,
                    "prefix": MOUNT_PREFIX,
                    "static": {"dir": str(root)},
                })
                r.raise_for_status()
            body = r.json()
            sid, lease_path = body["service_id"], body["lease_ws_path"]
            log.info("drawio app mount up: %s → %s (id=%s)",
                     MOUNT_PREFIX, root, sid)
            with STATE.lock:
                STATE.mounted, STATE.service_id = True, sid
                STATE.reason = "ok"
            backoff = 1.0
            async with websockets.connect(
                f"{ws_base}{lease_path}",
                ssl=ssl_ctx, max_size=None, open_timeout=10,
            ) as ws:
                async for _ in ws:   # first frame is "ready"; then just hold
                    pass
            log.info("drawio app mount lease closed; re-registering")
        except Exception as exc:  # noqa: BLE001 — stay up across any fault
            with STATE.lock:
                STATE.reason = f"{type(exc).__name__}: {exc}"
            log.warning("drawio app mount lost (%s); re-registering in %.1fs",
                        exc, backoff)
        finally:
            with STATE.lock:
                STATE.mounted = False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)
