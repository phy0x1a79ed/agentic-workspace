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

from awm import config
from awm.gatewayclient import ServiceAdapter

from awm.httpsfront import certs, proxy, store

log = logging.getLogger("awm.httpsfront.hub_adapter")

HERE = Path(__file__).resolve().parent          # awm/services/httpsfront/awm/httpsfront
SERVICE_DIR = HERE.parents[1]                    # awm/services/httpsfront
CERT_DIR = SERVICE_DIR / ".certs"
SANS_FILE = SERVICE_DIR / ".sans"                # host-specific extra SANs (gitignored)

PORT = int(os.environ.get("AWM_HTTPS_PORT", "8443"))
UPSTREAM = os.environ.get("AWM_HUB_URL", "http://127.0.0.1:7819/")
# "public" narrows the front to the policy allow-list (see proxy.build_app).
PROFILE = (os.environ.get("AWM_EDGE_PROFILE") or "").strip().lower() or None
# 0 → plain HTTP on loopback behind a TLS-terminating nginx; no certs minted.
TLS = os.environ.get("AWM_EDGE_TLS", "1").strip().lower() not in ("0", "false", "no")
# The shared knowledge base, served at /vault on this same listener. On by
# default: off is the visibly broken direction — nobody can reach the vault, and
# somebody says so within the minute. The dangerous direction is a vault
# reachable by something that is not this edge, and that is closed in
# awm.trilium, where the child is bound, rather than here.
VAULT = os.environ.get("AWM_EDGE_VAULT", "1").strip().lower() not in ("0", "false", "no")
VAULT_UPSTREAM = config.VAULT_URL if VAULT else None

# Penpot, served at /penpot on this same listener. Off by default, unlike the
# vault: Penpot's root-level asset paths collide with the vault's own
# (``/api/``, ``/assets/`` — see awm.httpsfront.penpot's module docstring),
# and the vault is the one already relied on, so enabling this is an explicit
# choice rather than something a bare upgrade should flip on.
PENPOT = os.environ.get("AWM_EDGE_PENPOT", "0").strip().lower() not in ("0", "false", "no")

# Whether Penpot also owns ``/`` on this listener. On by default *when Penpot
# is enabled at all*, because Penpot served anywhere but the origin root does
# not work -- its own router cannot parse the pathname and answers every route
# with its login screen, however valid the session (see awm.httpsfront.penpot's
# SHELL docstring for the live evidence). Off is for an edge that fronts
# something else at ``/`` and wants Penpot's asset paths reachable anyway,
# which is a diagnostic shape rather than a working one.
PENPOT_ROOT = os.environ.get(
    "AWM_EDGE_PENPOT_ROOT", "1").strip().lower() not in ("0", "false", "no")

if PENPOT and VAULT:
    # These two cannot share an edge. Both claim /api/ and /assets/ at the
    # root, the proxy resolves the vault first, and Penpot additionally needs
    # `/` itself (see awm.httpsfront.penpot's SHELL docstring), which the
    # vault's own shell and the landing page also want. With both on, Penpot
    # loads a shell whose every data fetch Trilium answers -- an app that
    # looks like it started and then does nothing, which is miserable to
    # debug from the browser side. VAULT defaults on, so this fires for
    # anyone who enables Penpot without knowing to turn Trilium's edge off.
    log.warning(
        "AWM_EDGE_PENPOT and AWM_EDGE_VAULT are both enabled. They cannot "
        "share one edge: both claim /api and /assets at the root, the vault "
        "wins, and Penpot needs / itself as well. Penpot will load its shell "
        "and then fail every request it makes. Set AWM_EDGE_VAULT=0, give one "
        "of them a URL base, or front them on separate origins."
    )
PENPOT_UPSTREAM = config.PENPOT_URL if PENPOT else None

# Live status, filled once the listener comes up.
_STATUS: dict[str, Any] = {
    "listener_port": PORT,
    "tls": False,
    "san": None,
    "upstream": UPSTREAM,
    # The extra upstreams on this listener, and the only place an operator can
    # see whether the edge thinks it is serving the vault or Penpot at all.
    "vault_upstream": VAULT_UPSTREAM,
    "penpot_upstream": PENPOT_UPSTREAM,
    "penpot_at_root": PENPOT and PENPOT_ROOT,
    "serving": False,
    "profile": PROFILE or "default",
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
    # The front's own root, not a page. This runs on every node, and which page
    # is worth landing on differs between them — naming one here was only ever
    # right on the host that happened to serve it.
    st["url_shape"] = f"https://{host or '<host-ip>'}:{st['listener_port']}/"
    return st


HANDLERS = {"status": _h_status}


def _serve_forever(info: dict) -> None:
    """Run the TLS reverse proxy, restarting it on crash. Lives in a daemon
    thread for the life of the process (= the life of the gateway lease)."""
    while True:
        try:
            _STATUS.update(serving=True, tls=TLS, san=info.get("san"))
            proxy.serve(
                port=PORT,
                cert=info.get("cert", ""),
                key=info.get("key", ""),
                ca=info.get("ca", ""),
                upstream=UPSTREAM,
                profile=PROFILE,
                tls=TLS,
                vault_upstream=VAULT_UPSTREAM,
                penpot_upstream=PENPOT_UPSTREAM,
                penpot_root=PENPOT and PENPOT_ROOT,
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

    if TLS:
        sans = certs.resolve_sans(san_file=SANS_FILE)
        info = certs.ensure_certs(CERT_DIR, sans=sans)
        log.info("certs ready (SAN=%s)", info["san"])
    else:
        info = {}
        log.info("AWM_EDGE_TLS=0: plain HTTP on loopback, no certs")

    t = threading.Thread(
        target=_serve_forever, args=(info,), daemon=True, name="httpsfront"
    )
    t.start()
    log.info("front thread launched on :%d → %s (tls %s, profile %s)",
             PORT, UPSTREAM, "on" if TLS else "off", PROFILE or "default")


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
