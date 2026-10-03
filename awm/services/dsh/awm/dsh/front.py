"""The mesh-facing HTTPS front for the DeepSeek Harness GUI.

The harness binds plain HTTP on loopback with no auth of its own — the right
default, and we keep it. This front is what makes it reachable from a browser
anywhere on the ZeroTier mesh without weakening that: a TLS listener on
``0.0.0.0:<port>`` that authenticates every request against awm's edge session
and reverse-proxies the survivors to the loopback harness.

Almost none of that is written here. ``awm.httpsfront`` already solves it and
takes the upstream as a parameter, so this is a configuration of an existing
component rather than a second implementation.

**Why not a gateway mount.** ``kind=page`` and ``kind=static`` serve a dist
directory and refuse WebSockets outright, so neither can host a SPA that needs
``/api`` and a stream on its own origin. ``kind=url`` can carry both — the hub
middleware does strip the mount prefix and does bridge the WebSocket — and two
blockers remain anyway: the harness frontend is built with an absolute asset
base and has no base-path option, and ``proxy_http`` strips ``Host`` while
forwarding ``Origin`` verbatim, so the harness's Origin-equals-Host fence would
403 every ``/api`` call with no ``rewrite_origin`` to close it. A dedicated
front on its own port is the design, not a shortcut around one. Don't
re-derive this.

**Why the Origin rewrite.** Every ``/api`` request passes a browser-trust fence
in the harness: the ``Host`` must be loopback or a ``--trusted-host`` grant, and
a present ``Origin`` must equal it. httpsfront drops the inbound ``Host`` so
httpx derives it from the upstream URL, which satisfies the first half with no
grant and no weakening of the harness's own posture. It forwards ``Origin``
verbatim, though, which leaves the fence comparing a mesh origin to a loopback
host — every call 403s, handshakes included. ``rewrite_origin=True`` closes
exactly that gap, on the WebSocket path as well as the HTTP one; a rewrite on
HTTP alone yields a GUI that loads and then silently never streams.

**What the rewrite does not buy.** The fence is a server check, and the
harness's *client* makes its own decisions from ``location``. It once admitted
the host-backed settings mirror on a loopback hostname alone, which left the
Settings pages reporting that settings are unavailable on a mesh address; the
fork now also admits an ``https:`` page, because a TLS page cannot have reached
the browser except through this front. ``dsh model`` remains the way to set the
model route without the GUI.
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

#: The SAN declarations behind that borrowed leaf. This service keeps no
#: ``.sans`` of its own, so a hand-recorded address lives there, and
#: :func:`_declared_mesh_address` reads the file that actually put the address
#: into the leaf this front serves.
HTTPSFRONT_SANS_FILE = harness.SERVICE_DIR.parent / "httpsfront" / ".sans"

#: Mesh-facing TLS port for the harness GUI.
#:
#: Inside the range this host forwards into WSL and admits through the Windows
#: firewall, which is what makes the mesh URL reachable at all. Every other awm
#: front already sits there — httpsfront on 12100, claude-science on 12201/2 —
#: and this service was the one outlier, so its URL worked from nowhere but
#: loopback. Moving the port is cheaper than widening a portproxy table and a
#: firewall rule for one service.
PORT = int(os.environ.get("DSH_FRONT_PORT", "12130"))

_STATUS: dict[str, Any] = {}


def status() -> dict:
    return dict(_STATUS)


def _declared_mesh_address() -> str | None:
    """The mesh address recorded in ``.sans``, for a host that cannot see its own.

    A WSL node has no ZeroTier address of its own: the client runs on the
    Windows host, and traffic reaches this listener through a port forward, so
    ``config.mesh_address()`` enumerating local interfaces finds nothing. The
    address is already written down for exactly this reason — it is a ``.sans``
    entry, which is how it reached the leaf this front serves — so read those
    files rather than adding a second place to record one fact: this service's
    own, then httpsfront's, whose leaf this service borrows. Entries outside the mesh subnet (the LAN
    address, the docker bridge) are skipped: a link to one of those is a link
    the phone this page is read on cannot follow.
    """
    import ipaddress
    try:
        net = ipaddress.ip_network(
            os.environ.get(config.MESH_SUBNET_ENV) or config.DEFAULT_MESH_SUBNET,
            strict=False)
    except ValueError:
        return None
    for path in (SANS_FILE, HTTPSFRONT_SANS_FILE):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            # Absent or unreadable: that file simply declares nothing.
            continue
        for line in lines:
            entry = line.split("#", 1)[0].strip()
            if not entry:
                continue
            try:
                addr = ipaddress.ip_address(entry)
            except ValueError:
                continue
            if addr in net:
                return str(addr)
    return None


def origin(port: int = PORT) -> str:
    """The URL a browser on the mesh opens.

    The fleet mesh address specifically, not merely the first non-loopback one
    the host has: this node also carries a LAN address and a docker bridge, and
    a link to either is a link the phone this page is read on cannot follow.
    Prefers the address this host can enumerate, falls back to the one declared
    in ``.sans`` (see :func:`_declared_mesh_address`), and only then to loopback
    so the page shows a URL that at least works from here rather than one that
    works nowhere.
    """
    host = config.mesh_address() or _declared_mesh_address() or "127.0.0.1"
    return f"https://{host}:{port}"


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
