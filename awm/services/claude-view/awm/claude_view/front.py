"""The mesh-facing HTTPS front for claude-view.

The upstream binary binds loopback plain HTTP with no auth of its own — that is
the right default and we keep it. This module is what makes it reachable from a
phone on the ZeroTier mesh without weakening that: a TLS listener on
``0.0.0.0:<port>`` that authenticates every request against awm's edge session
and reverse-proxies the survivors to ``127.0.0.1:47892``.

Almost none of that is written here. ``awm.httpsfront`` already solves it for
the gateway, and its ``proxy.serve()`` takes the upstream as a parameter, so
this is a configuration of an existing component rather than a second
implementation: ``certs.ensure_certs`` mints a leaf off the shared remote-audio
CA (devices that already trust awm need no new setup), ``auth.AuthGate``
verifies the ``awm_session`` cookie offline and fails closed, and the proxy
bridges HTTP and WebSockets alike. The one thing we change is ``landing=False``
— the gateway front owns ``/`` with an index of ``/ui/*`` pages, and here ``/``
belongs to claude-view's SPA.

Single sign-on falls out of cookie scoping: cookies are keyed by host and
ignore port, so the session minted by logging in at ``:12100`` is sent to this
port too. There is deliberately no convenience sign-in route — this front
serves the fleet's complete conversation transcripts, and a bookmarkable
auto-authorize URL would hand that to any device that reached the mesh.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from awm.httpsfront import certs, proxy

from awm.claude_view import binary

log = logging.getLogger("awm.claude_view.front")

SERVICE_DIR = binary.SERVICE_DIR
CERT_DIR = SERVICE_DIR / ".certs"
SANS_FILE = SERVICE_DIR / ".sans"

#: Mesh-facing TLS port. Inside the 12100–12150 band that the Windows
#: portproxy script already forwards wholesale, so reaching this from a mesh
#: device needs no elevated re-run of that script.
PORT = int(os.environ.get("CLAUDE_VIEW_FRONT_PORT", "12110"))

_STATUS: dict[str, Any] = {
    "listener_port": PORT,
    "tls": False,
    "san": None,
    "upstream": f"http://127.0.0.1:{binary.PORT}/",
    "serving": False,
    "error": None,
}


def status() -> dict:
    return dict(_STATUS)


def _serve_forever() -> None:
    """Mint certs and run the TLS front, restarting it if it ever falls over.

    Mirrors httpsfront's own supervision thread. A crash here must not take the
    service down: the gateway registration, the ``status`` verb, and the
    supervised binary all stay useful, and ``status`` is how anyone finds out
    the front is the broken part.
    """
    backoff = 1.0
    while True:
        try:
            # resolve_sans already unions the auto-enumerated host addresses
            # with AWM_TLS_EXTRA_SANS and a host-specific file, normalising and
            # deduping in a stable order so the leaf only re-mints on a real
            # change. The file is how the Windows-side ZeroTier IP gets in — it
            # is invisible from inside WSL, so nothing can enumerate it.
            sans = certs.resolve_sans(san_file=SANS_FILE)
            paths = certs.ensure_certs(CERT_DIR, sans=sans)
            _STATUS.update({"tls": True, "san": sans, "error": None,
                            "serving": True})
            log.info("claude-view front: https://0.0.0.0:%d → %s (san=%s)",
                     PORT, _STATUS["upstream"], sans)
            backoff = 1.0
            proxy.serve(
                port=PORT,
                cert=str(paths["cert"]),
                key=str(paths["key"]),
                ca=str(paths["ca"]),
                upstream=_STATUS["upstream"],
                # /  belongs to claude-view's SPA, not to awm's page index.
                landing=False,
            )
            _STATUS["serving"] = False
            log.warning("claude-view front: listener returned; restarting")
        except Exception as exc:  # noqa: BLE001 — never let the thread die
            _STATUS.update({"serving": False,
                            "error": f"{type(exc).__name__}: {exc}"})
            log.exception("claude-view front: failed; retrying in %.1fs", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def start() -> None:
    """Launch the front in a daemon thread. Idempotent per process."""
    t = threading.Thread(target=_serve_forever, name="claude-view-front",
                         daemon=True)
    t.start()
