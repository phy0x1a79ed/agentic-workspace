"""The mesh-facing HTTPS front for the DeepSeek Harness GUI.

The harness binds plain HTTP on loopback with no auth of its own — the right
default, and we keep it. This front is what makes it reachable from a browser
anywhere on the ZeroTier mesh without weakening that: a TLS listener on
``0.0.0.0:<port>`` that authenticates every request against awm's edge session
and reverse-proxies the survivors to the loopback harness.

Almost none of that is written here. ``awm.httpsfront`` already solves it and
takes the upstream as a parameter, so this is a configuration of an existing
component rather than a second implementation.

**Why not a gateway mount.** ``kind=url`` looks like it should work, and it
cannot, in three independent places: the gateway's url proxy forwards the full
path without stripping the mount prefix, the harness's frontend is built with an
absolute asset base and has no base-path option, and the gateway's WebSocket
bridge forwards no headers at all. A dedicated front on its own port is the
design, not a shortcut around one. Don't re-derive this.

**Why the Origin rewrite.** Every ``/api`` request passes a browser-trust fence
in the harness: the ``Host`` must be loopback or a ``--trusted-host`` grant, and
a present ``Origin`` must equal it. httpsfront drops the inbound ``Host`` so
httpx derives it from the upstream URL, which satisfies the first half with no
grant and no weakening of the harness's own posture. It forwards ``Origin``
verbatim, though, which leaves the fence comparing a mesh origin to a loopback
host — every call 403s, handshakes included. ``rewrite_origin=True`` closes
exactly that gap, on the WebSocket path as well as the HTTP one; a rewrite on
HTTP alone yields a GUI that loads and then silently never streams.

**What the rewrite does not buy.** The fence is a server check. The harness's
*client* independently tests ``isLoopbackHostname(location.hostname)`` to decide
whether its settings mirror is host-backed or in-memory, so on a mesh address
the Settings pages report that settings are unavailable. That is ``location``,
not a header; nothing here reaches it. See ``INSTALL.md`` — the model route,
which is what anyone needs from that page, is a verb instead.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from awm import config
from awm.httpsfront import certs, proxy

from awm.dsh import harness

log = logging.getLogger("awm.dsh.front")

CERT_DIR = harness.SERVICE_DIR / ".certs"
SANS_FILE = harness.SERVICE_DIR / ".sans"

#: Where a leaf is borrowed from when this node cannot mint one. See
#: :func:`_borrow_leaf`.
HTTPSFRONT_CERT_DIR = harness.SERVICE_DIR.parent / "httpsfront" / ".certs"

#: Mesh-facing TLS port for the harness GUI.
PORT = int(os.environ.get("DSH_FRONT_PORT", "12301"))

_STATUS: dict[str, Any] = {}


def status() -> dict:
    return dict(_STATUS)


def origin(port: int = PORT) -> str:
    """The URL a browser on the mesh opens.

    The fleet mesh address specifically, not merely the first non-loopback one
    the host has: this node also carries a LAN address and a docker bridge, and
    a link to either is a link the phone this page is read on cannot follow.
    Falls back to loopback so the page shows a URL that at least works from here
    rather than one that works nowhere.
    """
    return f"https://{config.mesh_address() or '127.0.0.1'}:{port}"


def _borrow_leaf() -> None:
    """Copy httpsfront's leaf into our cert dir when we have none of our own.

    This node is a deliberate *trust consumer*: it holds ``ca.pem`` without
    ``ca-key.pem``, so it cannot sign, and ``ensure_certs`` refuses rather than
    minting a fleet-incompatible root. The leaf it validates is port-independent
    and its SAN set already covers this host's mesh address, so a second front
    on a second port needs the same pair rather than a new one — the same
    arrangement ``claude-science`` runs on. Copying is what keeps that a fact of
    this service's install rather than two services writing one directory.
    """
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("cert.pem", "key.pem"):
        dst, src = CERT_DIR / name, HTTPSFRONT_CERT_DIR / name
        if dst.exists() or not src.exists():
            continue
        shutil.copyfile(src, dst)
        if name == "key.pem":
            dst.chmod(0o600)
        log.info("dsh front: borrowed %s from %s", name, HTTPSFRONT_CERT_DIR)


def _serve_forever() -> None:
    """Mint-or-validate certs and run the TLS front, restarting it if it falls over.

    Mirrors httpsfront's own supervision thread. A crash here must not take the
    service down: the registration, the verbs and the harness itself all stay
    useful, and ``status`` is how anyone finds out the front is the broken part.
    """
    upstream = f"http://127.0.0.1:{harness.PORT}/"
    _STATUS.update({"listener_port": PORT, "upstream": upstream, "tls": False,
                    "san": None, "serving": False, "error": None,
                    "url": origin()})
    backoff = 1.0
    while True:
        try:
            _borrow_leaf()
            sans = certs.resolve_sans(san_file=SANS_FILE)
            paths = certs.ensure_certs(CERT_DIR, sans=sans)
            _STATUS.update({"tls": True, "san": paths.get("san"), "error": None,
                            "serving": True})
            log.info("dsh front: https://0.0.0.0:%d → %s (san=%s)",
                     PORT, upstream, paths.get("san"))
            backoff = 1.0
            proxy.serve(
                port=PORT,
                cert=str(paths["cert"]),
                key=str(paths["key"]),
                ca=str(paths["ca"]),
                upstream=upstream,
                # `/` belongs to the harness's SPA, not to awm's page index.
                landing=False,
                # The harness's Origin-equals-Host fence; see the module docstring.
                rewrite_origin=True,
            )
            _STATUS["serving"] = False
            log.warning("dsh front: listener returned; restarting")
        except Exception as exc:  # noqa: BLE001 — never let the thread die
            _STATUS.update({"serving": False,
                            "error": f"{type(exc).__name__}: {exc}"})
            log.exception("dsh front: failed; retrying in %.1fs", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def start() -> None:
    """Launch the front in a daemon thread."""
    threading.Thread(target=_serve_forever, name="dsh-front", daemon=True).start()
