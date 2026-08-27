"""One mesh-facing HTTPS front per user, in front of that user's server.

Each Trilium child binds plain HTTP on loopback with its own login. This is
what makes it reachable from a browser anywhere on the ZeroTier mesh without
weakening that: a TLS listener on `0.0.0.0:<port>` that authenticates every
request against awm's edge session and reverse-proxies the survivors to one
user's loopback server.

Almost none of that is written here. `awm.httpsfront` already solves it and
takes the upstream as a parameter, so this is a configuration of an existing
component rather than a second implementation.

**Two gates, and they are not redundant.** The edge session says "someone who
can log into awm", which is one shared password for the whole workspace.
Trilium's own login says *which person*. Removing either collapses the
distinction the whole design rests on.

**Why not a gateway mount.** `kind=url` looks like it should work and does not,
for the reasons dsh's `front.py` records: the gateway's url proxy forwards the
full path without stripping the mount prefix, and its WebSocket bridge forwards
no headers at all. Trilium's client holds a WebSocket open for every change it
renders, so the second one alone is fatal. A dedicated front per user is the
design, not a shortcut around one. Don't re-derive this.

**Why no Origin rewrite.** dsh needs one because the harness compares `Origin`
to `Host`. Trilium does not: its CSRF protection is a double-submit cookie
(`csrf-csrf`), which travels correctly through an unmodified proxy. Setting
`rewrite_origin` here would hide nothing and buy nothing.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from typing import Any

from awm import config
from awm.httpsfront import certs, proxy

from awm.trilium import instances
from awm.trilium.instances import Instance

log = logging.getLogger("awm.trilium.front")

#: One leaf for every listener. The certificate is port-independent and its SAN
#: set already covers this host, so N fronts need the same pair rather than N.
CERT_DIR = instances.SERVICE_DIR / ".certs"
SANS_FILE = instances.SERVICE_DIR / ".sans"

#: Where a leaf is borrowed from when this node cannot mint one.
HTTPSFRONT_CERT_DIR = instances.SERVICE_DIR.parent / "httpsfront" / ".certs"

_STATUS: dict[str, dict[str, Any]] = {}
_THREADS: dict[str, threading.Thread] = {}
_LOCK = threading.RLock()


def status() -> list[dict]:
    with _LOCK:
        return [dict(v) for _, v in sorted(_STATUS.items())]


def status_for(user: str) -> dict:
    with _LOCK:
        return dict(_STATUS.get(user) or {})


def origin(inst: Instance) -> str:
    """The URL a browser on the mesh opens for this user.

    The fleet mesh address specifically, not merely the first non-loopback one
    the host has: this node also carries a LAN address and a docker bridge, and
    a link to either is a link the phone this page is read on cannot follow.
    Falls back to loopback so the page shows a URL that at least works from
    here rather than one that works nowhere.
    """
    host = config.mesh_address() or "127.0.0.1"
    return f"https://{host}:{inst.front_port}"


def _borrow_leaf() -> None:
    """Copy httpsfront's leaf into our cert dir when we have none of our own.

    A node may be a deliberate *trust consumer*: it holds `ca.pem` without
    `ca-key.pem`, so it cannot sign, and `ensure_certs` refuses rather than
    minting a fleet-incompatible root. The leaf it validates is port-
    independent and its SAN set already covers this host's mesh address, so
    borrowing is what keeps that a fact of this service's install rather than
    two services writing one directory.
    """
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("cert.pem", "key.pem"):
        dst, src = CERT_DIR / name, HTTPSFRONT_CERT_DIR / name
        if dst.exists() or not src.exists():
            continue
        shutil.copyfile(src, dst)
        if name == "key.pem":
            dst.chmod(0o600)
        log.info("trilium front: borrowed %s from %s", name, HTTPSFRONT_CERT_DIR)


def _serve_forever(inst: Instance) -> None:
    """Mint-or-validate certs and run one user's TLS front, restarting it if it
    falls over.

    Mirrors httpsfront's own supervision thread. A crash here must not take the
    service down: the registration, the verbs and every other user's front all
    stay useful, and `status` is how anyone finds out this one is the broken
    part.
    """
    upstream = f"http://127.0.0.1:{inst.upstream_port}/"
    with _LOCK:
        _STATUS[inst.user] = {"user": inst.user, "listener_port": inst.front_port,
                              "upstream": upstream, "tls": False, "san": None,
                              "serving": False, "error": None,
                              "url": origin(inst)}
    backoff = 1.0
    while True:
        try:
            _borrow_leaf()
            sans = certs.resolve_sans(san_file=SANS_FILE)
            paths = certs.ensure_certs(CERT_DIR, sans=sans)
            with _LOCK:
                _STATUS[inst.user].update({"tls": True, "san": paths.get("san"),
                                           "error": None, "serving": True})
            log.info("trilium front[%s]: https://0.0.0.0:%d → %s (san=%s)",
                     inst.user, inst.front_port, upstream, paths.get("san"))
            backoff = 1.0
            proxy.serve(
                port=inst.front_port,
                cert=str(paths["cert"]),
                key=str(paths["key"]),
                ca=str(paths["ca"]),
                upstream=upstream,
                # `/` belongs to Trilium's own application, not to awm's page index.
                landing=False,
            )
            with _LOCK:
                _STATUS[inst.user]["serving"] = False
            log.warning("trilium front[%s]: listener returned; restarting", inst.user)
        except Exception as exc:  # noqa: BLE001 — never let the thread die
            with _LOCK:
                _STATUS[inst.user].update(
                    {"serving": False, "error": f"{type(exc).__name__}: {exc}"})
            log.exception("trilium front[%s]: failed; retrying in %.1fs",
                          inst.user, backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def sync() -> list[str]:
    """Raise a front for every user that has none. Returns the users started.

    Idempotent, and called from the same loop that reconciles the children, so
    a user added while the service runs gets a listener without a restart. A
    node where something else is the public edge sets `TRILIUM_FRONTS=0` and
    gets none — see `instances.FRONTS_ENABLED`.
    A front is never torn down: a listener whose upstream went away answers 502
    rather than refusing the connection, which is the more legible failure, and
    threads holding a bound port cannot be reclaimed cleanly anyway.
    """
    if not instances.FRONTS_ENABLED:
        return []
    started = []
    for inst in instances.instances():
        with _LOCK:
            live = _THREADS.get(inst.user)
            if live is not None and live.is_alive():
                continue
            t = threading.Thread(target=_serve_forever, args=(inst,),
                                 name=f"trilium-front-{inst.user}", daemon=True)
            _THREADS[inst.user] = t
        t.start()
        started.append(inst.user)
    return started
