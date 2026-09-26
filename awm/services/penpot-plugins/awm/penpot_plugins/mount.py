"""Register the ``local/`` plugin tree as a gateway ``kind=static`` mount.

Same shape as ``awm.drawio.mount`` and ``awm.fileviewer.server``: the control
WS does not cover mounts, so this runs its own register + hold-lease +
reconnect loop and re-registers on any drop (network hiccup, or a gateway
restart that wipes the in-memory registry — without the loop the mount would
silently 404 until someone noticed and bounced the service).

No mask, unlike fileviewer's ``/files``: this mount exposes only
``local/``, a small tree we author ourselves, not an arbitrary filesystem
root — everything under it is a published plugin asset by construction.
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

log = logging.getLogger("awm.penpot_plugins.mount")

SERVICE_NAME = os.environ.get("AWM_SERVICE_NAME", "penpot-plugins")
MOUNT_PREFIX = os.environ.get("PENPOT_PLUGINS_MOUNT_PREFIX", "/penpot-plugins")


def local_root() -> Path:
    """Where the plugin trees live: ``local/`` beside this package.

    Override with ``PENPOT_PLUGINS_ROOT`` (mainly for tests, to point at a
    scratch tree instead of the real one).
    """
    override = os.environ.get("PENPOT_PLUGINS_ROOT")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[2] / "local"


class _State:
    """Live mount status for the ``status`` verb (never raises)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mounted = False
        self.service_id: str | None = None
        self.reason: str = "not started"

    def snapshot(self) -> dict:
        with self.lock:
            root = local_root()
            plugins = sorted(
                p.name for p in root.iterdir()
                if p.is_dir() and not p.name.startswith("_")
                and (p / "manifest.json").is_file()
            ) if root.is_dir() else []
            return {
                "mounted": self.mounted,
                "prefix": MOUNT_PREFIX,
                "root": str(root),
                "plugins": plugins,
                # Origin-relative shape a Plugin Manager install URL follows —
                # host/port is whatever origin actually serves the gateway.
                "install_url_shape": f"{MOUNT_PREFIX}/<name>/manifest.json",
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
    """Register the plugin-hosting mount and hold its lease forever."""
    hub_url = os.environ.get("AWM_HUB_URL", "").rstrip("/")
    if not hub_url:
        with STATE.lock:
            STATE.reason = "AWM_HUB_URL not set"
        log.error("AWM_HUB_URL not set; penpot-plugins mount cannot register")
        return

    root = local_root()
    if not root.is_dir():
        # Not fatal: the process still supervises fine, but nothing installs
        # until the local/ tree exists — say so plainly rather than a bare 404.
        with STATE.lock:
            STATE.reason = f"local plugin root missing at {root}"
        log.warning("penpot-plugins local root missing at %s — mount will "
                    "404 until it exists", root)

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
            log.info("penpot-plugins mount up: %s → %s (id=%s)",
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
            log.info("penpot-plugins mount lease closed; re-registering")
        except Exception as exc:  # noqa: BLE001 — stay up across any fault
            with STATE.lock:
                STATE.reason = f"{type(exc).__name__}: {exc}"
            log.warning("penpot-plugins mount lost (%s); re-registering in %.1fs",
                        exc, backoff)
        finally:
            with STATE.lock:
                STATE.mounted = False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)
