"""The mesh-facing HTTPS front for the Hermes Agent dashboard.

The dashboard binds plain HTTP on loopback with no auth of its own — the right
default, and we keep it. This front is what makes it reachable from a browser
anywhere on the ZeroTier mesh without weakening that: a TLS listener on
``0.0.0.0:<port>`` that authenticates every request against awm's edge session
and reverse-proxies the survivors to the loopback dashboard.

Almost none of that is written here. ``awm.httpsfront`` already solves it and
takes the upstream as a parameter, so this is a configuration of an existing
component rather than a second implementation — the arrangement ``dsh`` and
``claude-science`` also run.

**Why the dashboard must own an origin, not a path prefix.** It reads
``X-Forwarded-Prefix`` and rewrites ``index.html`` and its stylesheets, which
makes a subpath mount look like it works: the shell paints and the first route
renders. It is not enough. The SPA's *lazy* route chunks are fetched by a
loader whose asset base was frozen at ``/`` when the bundle was built, so under
any prefix each one is requested from the server root and 404s. Script preloads
fail quietly, but a stylesheet preload is awaited, so the first route that
carries CSS — chat — throws and paints nothing. A build-time constant is not a
header-addressable property: no gateway flag can reach it, and mounting this
upstream anywhere but ``/`` is not a thing that can be made to work. Serving at
the root of our own listener makes the baked-in base correct by construction,
which is why there is no rewriting here and nothing that can drift.

**Why the Origin rewrite.** The dashboard re-runs its DNS-rebinding guard on
every WebSocket upgrade, comparing ``Host`` *and* ``Origin`` against the
interface it bound. httpsfront drops the inbound ``Host`` so httpx derives one
from the upstream URL, which satisfies the first half. It forwards ``Origin``
verbatim, though, which leaves the guard holding a mesh origin against a
loopback bind — ``origin_mismatch``, and the upgrade is refused before it is
accepted. ``rewrite_origin=True`` closes exactly that gap. It is not cosmetic:
without it the GUI loads perfectly and then silently never streams, because
every route in this dashboard that matters is a WebSocket.
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

from awm.hermes import daemon

log = logging.getLogger("awm.hermes.front")

#: …/awm/services/hermes — this service's own directory, wherever this copy of
#: the code lives (the deployed tree in prod, a worktree under `awm dev shadow`).
#: Three levels up from this file, not two: the dist nests its PEP 420 namespace
#: as ``<service>/awm/hermes/``.
SERVICE_DIR = Path(__file__).resolve().parents[2]

CERT_DIR = SERVICE_DIR / ".certs"
SANS_FILE = SERVICE_DIR / ".sans"

#: Mesh-facing TLS port for the dashboard. Continues the per-service band:
#: httpsfront 12100, claude-science 12201/12202, dsh 12301.
PORT = int(os.environ.get("HERMES_FRONT_PORT", "12401"))

#: The gateway mount the landing page is served at. Not ours to register — the
#: gateway discovers `awm/pages/hermes/dist` — but `url` reports it, because
#: "where do I find this" and "where does the app live" are different answers.
LANDING_PREFIX = "/ui/hermes"

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


def _declared_edge() -> str:
    """``AWM_EDGE_URL`` as this node declares it, from the environment or, if
    it is not there, from the canonical workspace's env file.

    The daemon loads that file at start-up and its services inherit the result,
    so the environment answers in production. A shadow overlay does not inherit
    it — it runs against an isolated ``.awm-shadow`` root — and
    ``config.edge_url``'s fallback then guesses ``AWM_HTTPS_PORT``'s default,
    which on a node whose edge is elsewhere is a link that goes nowhere. Read
    rather than merged: a service has no business mutating the process
    environment to answer a question about a URL.
    """
    if declared := (os.environ.get("AWM_EDGE_URL") or "").strip():
        return declared
    try:
        text = (config.canonical_workspace() / ".awm" / "env").read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        key, _, value = line.strip().removeprefix("export ").partition("=")
        if key.strip() == "AWM_EDGE_URL":
            return value.strip().strip("'\"")
    return ""


def landing_url() -> str:
    """The awm page that reports on this service, on the shared edge."""
    edge = (_declared_edge() or config.edge_url() or "").rstrip("/")
    return f"{edge}{LANDING_PREFIX}/" if edge else f"{LANDING_PREFIX}/"


def _leaf_sources() -> list[Path]:
    """Cert dirs to borrow a leaf from, best first.

    The sibling ``httpsfront`` in *this* copy of the tree comes first: when a
    whole sandbox runs from a worktree, that is the front that minted the leaf.
    The canonical workspace is the fallback, and it is the one that answers when
    only this service is shadowed out of a worktree — the common case, since a
    worktree carries no ``.certs`` of its own (they are gitignored state).

    Canonical, not ``WORKSPACE_ROOT``: a shadow overlay deliberately runs with
    an isolated local root (``.awm-shadow``) so its per-service DBs cannot
    collide with the base it shadows, and the hub reports the real workspace on
    register. A leaf is a node-level asset, so it lives in the real one. Reading
    the local root here finds an empty directory and the front never comes up.
    """
    return [
        SERVICE_DIR.parent / "httpsfront" / ".certs",
        config.canonical_workspace() / "awm" / "services" / "httpsfront" / ".certs",
    ]


def _borrow_leaf() -> None:
    """Copy a leaf into our cert dir when this node cannot mint one.

    This node is a deliberate *trust consumer*: it holds ``ca.pem`` without
    ``ca-key.pem``, so it cannot sign, and ``ensure_certs`` refuses rather than
    minting a fleet-incompatible root. The leaf it validates is port-independent
    and its SAN set already covers this host's mesh address, so a second front
    on a second port needs the same pair rather than a new one. Copying is what
    keeps that a fact of this service's start-up rather than two services
    writing one directory.
    """
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    for src_dir in _leaf_sources():
        if not (src_dir / "cert.pem").is_file() or not (src_dir / "key.pem").is_file():
            continue
        for name in ("cert.pem", "key.pem"):
            dst, src = CERT_DIR / name, src_dir / name
            if dst.exists():
                continue
            shutil.copyfile(src, dst)
            if name == "key.pem":
                dst.chmod(0o600)
            log.info("hermes front: borrowed %s from %s", name, src_dir)
        return


def _serve_forever() -> None:
    """Mint-or-validate certs and run the TLS front, restarting it if it falls over.

    Mirrors httpsfront's own supervision thread. A crash here must not take the
    service down: the registration, the verbs and the dashboard itself all stay
    useful, and ``status`` is how anyone finds out the front is the broken part.
    """
    upstream = f"http://127.0.0.1:{daemon.PORT}/"
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
            log.info("hermes front: https://0.0.0.0:%d → %s (san=%s)",
                     PORT, upstream, paths.get("san"))
            backoff = 1.0
            proxy.serve(
                port=PORT,
                cert=str(paths["cert"]),
                key=str(paths["key"]),
                ca=str(paths["ca"]),
                upstream=upstream,
                # `/` belongs to the dashboard's SPA, not to awm's page index.
                landing=False,
                # The dashboard's Host/Origin guard; see the module docstring.
                rewrite_origin=True,
            )
            _STATUS["serving"] = False
            log.warning("hermes front: listener returned; restarting")
        except Exception as exc:  # noqa: BLE001 — never let the thread die
            _STATUS.update({"serving": False,
                            "error": f"{type(exc).__name__}: {exc}"})
            log.exception("hermes front: failed; retrying in %.1fs", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def start() -> None:
    """Launch the front in a daemon thread."""
    threading.Thread(target=_serve_forever, name="hermes-front", daemon=True).start()
