"""The two mesh-facing HTTPS fronts for the Claude Science workbench.

The workbench binds plain HTTP on loopback with no auth of its own — the right
default, and we keep it. These fronts are what make it reachable from a phone
on the ZeroTier mesh without weakening that: TLS listeners on
``0.0.0.0:<port>`` that authenticate every request against awm's edge session
and reverse-proxy the survivors to the loopback daemon.

Almost none of that is written here. ``awm.httpsfront`` already solves it for
the gateway and takes the upstream as a parameter, so this is a configuration
of an existing component rather than a second implementation: ``ensure_certs``
mints a leaf off the shared root CA (devices that already trust awm need no new
setup), ``AuthGate`` verifies the ``awm_session`` cookie offline and fails
closed, and the proxy bridges HTTP and WebSockets alike, forwarding the
``Origin`` and ``X-Forwarded-Host`` the workbench needs.

**Why two.** The workbench serves generated HTML previews from a second port on
purpose: a page Claude wrote must not be same-origin with the session that
wrote it, or it could read that session. Collapsing the two onto one origin —
or onto one gateway path prefix — would quietly delete that boundary. So the
separation is carried all the way out: two ports, two origins, two fronts.

**Why not a gateway mount.** It is tempting: the binary has ``--base-path`` and
the gateway has ``kind=url``. But the gateway's url proxy strips ``Cookie`` on
the second hop and forwards no headers at all on WebSocket upgrades, and the
workbench authenticates with cookies and origin-checks its upgrades. It would
fail in three places at once. The dedicated fronts are the design, not a
shortcut around one.

**What the sign-in route is for.** The workbench has its own owner
authentication on top of awm's, so a browser that has already logged into awm
would still face a second, nonce-shaped gate that only the CLI can satisfy.
``/_signin`` mints a fresh single-use nonce server-side and auto-submits it, so
the bookmark just works. It is registered through ``extra_routes``, which means
httpsfront gates it with the edge session exactly like every proxied path —
the difference between a convenience and an open door on the mesh.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import threading
import time
from typing import Any

from awm.httpsfront import certs, proxy
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from awm.claude_science import api, daemon

log = logging.getLogger("awm.claude_science.front")

CERT_DIR = daemon.SERVICE_DIR / ".certs"
SANS_FILE = daemon.SERVICE_DIR / ".sans"

#: Mesh-facing TLS port for the workbench UI.
PORT = int(os.environ.get("CLAUDE_SCIENCE_FRONT_PORT", "12201"))

#: Mesh-facing TLS port for generated-HTML previews. A distinct browser origin
#: from PORT, which is the whole point — see the module docstring.
SANDBOX_PORT = int(os.environ.get("CLAUDE_SCIENCE_SANDBOX_FRONT_PORT", "12202"))

_STATUS: dict[str, dict[str, Any]] = {}


def status() -> dict:
    return {name: dict(state) for name, state in _STATUS.items()}


# ---------------------------------------------------------------------------
# The sign-in shim
# ---------------------------------------------------------------------------

async def _signin(request: Request) -> Response:
    """Zero-click owner sign-in for a caller who already holds an awm session.

    Mints a fresh single-use nonce and returns a page that auto-submits it,
    same-origin, to the workbench's own ``/api/auth/nonce`` — which sets the
    owner cookie and redirects into the app.

    httpsfront has already authenticated this request (``extra_routes`` are
    gated), so the nonce is handed only to someone who could log into awm. That
    is the entire difference between this and the hand-rolled front it replaces,
    where the same route was reachable by anyone who could route to the port.
    """
    if _supervisor is None:  # pragma: no cover — start() always attaches first
        return HTMLResponse("<h2>Sign-in unavailable</h2>", status_code=503)
    try:
        # Shells out to the binary, so never on the event loop.
        url = await asyncio.to_thread(_supervisor.login_url)
    except Exception as exc:  # noqa: BLE001 — report, never 500
        log.warning("signin: minting a nonce failed: %s", exc)
        return HTMLResponse(
            "<h2>Sign-in unavailable</h2><p>Could not mint a login nonce. "
            "Is the workbench running?</p>", status_code=502)

    try:
        nonce = html.escape(api.nonce_from(url))
    except api.WorkbenchError:
        return HTMLResponse(
            "<h2>Sign-in unavailable</h2><p>No nonce in "
            "<code>claude-science url</code> output.</p>", status_code=502)
    dest = html.escape(request.query_params.get("dest", "/"))
    page = (
        "<!doctype html><meta charset=utf-8><title>Signing in…</title>"
        "<body style=\"font:15px system-ui,sans-serif;max-width:28em;"
        "margin:5em auto;padding:0 1em;color:#222\">"
        "<h2>Signing in…</h2>"
        "<p>If this doesn't proceed automatically, use the button.</p>"
        "<form id=f method=post action=\"/api/auth/nonce\">"
        f"<input type=hidden name=nonce value=\"{nonce}\">"
        f"<input type=hidden name=dest value=\"{dest}\">"
        "<button type=submit>Sign in</button></form>"
        "<script>document.getElementById('f').submit()</script>"
    )
    # no-store: the embedded nonce is single-use, so a cached copy is a page
    # that is guaranteed not to work.
    return HTMLResponse(page, headers={"cache-control": "no-store"})


#: The live supervisor, attached by the adapter at startup. A module global
#: rather than a constructor argument because the route is registered by path,
#: and this keeps the import one-way: the adapter imports us, never the reverse.
_supervisor: Any | None = None


def attach(supervisor: Any) -> None:
    global _supervisor
    _supervisor = supervisor


# ---------------------------------------------------------------------------
# The listeners
# ---------------------------------------------------------------------------

def _serve_forever(name: str, port: int, upstream_port: int,
                   extra_routes: list | None) -> None:
    """Mint certs and run one TLS front, restarting it if it ever falls over.

    Mirrors httpsfront's own supervision thread. A crash here must not take the
    service down: the gateway registration, the verbs, and the workbench itself
    all stay useful, and ``status`` is how anyone finds out that the front is
    the broken part.
    """
    upstream = f"http://127.0.0.1:{upstream_port}/"
    state = _STATUS.setdefault(name, {})
    state.update({"listener_port": port, "upstream": upstream,
                  "tls": False, "san": None, "serving": False, "error": None})
    backoff = 1.0
    while True:
        try:
            # resolve_sans unions the auto-enumerated host addresses with
            # AWM_TLS_EXTRA_SANS and a host-specific file, in a stable order so
            # the leaf only re-mints on a real change. The file is how the
            # Windows-side ZeroTier address gets in — it is invisible from
            # inside WSL, so nothing can enumerate it.
            sans = certs.resolve_sans(san_file=SANS_FILE)
            paths = certs.ensure_certs(CERT_DIR, sans=sans)
            state.update({"tls": True, "san": sans, "error": None,
                          "serving": True})
            log.info("claude-science front %s: https://0.0.0.0:%d → %s (san=%s)",
                     name, port, upstream, sans)
            backoff = 1.0
            proxy.serve(
                port=port,
                cert=str(paths["cert"]),
                key=str(paths["key"]),
                ca=str(paths["ca"]),
                upstream=upstream,
                # `/` belongs to the workbench's SPA, not to awm's page index.
                landing=False,
                extra_routes=extra_routes,
            )
            state["serving"] = False
            log.warning("claude-science front %s: listener returned; restarting",
                        name)
        except Exception as exc:  # noqa: BLE001 — never let the thread die
            state.update({"serving": False,
                          "error": f"{type(exc).__name__}: {exc}"})
            log.exception("claude-science front %s: failed; retrying in %.1fs",
                          name, backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def start() -> None:
    """Launch both fronts in daemon threads. Idempotent per process."""
    specs = (
        # The UI front carries the sign-in shim.
        ("main", PORT, daemon.PORT, [("/_signin", ["GET"], _signin)]),
        # The preview front deliberately carries nothing extra: it serves pages
        # Claude generated, and an endpoint that mints owner credentials has no
        # business on that origin.
        ("sandbox", SANDBOX_PORT, daemon.SANDBOX_PORT, None),
    )
    for name, port, upstream_port, extra in specs:
        threading.Thread(
            target=_serve_forever, args=(name, port, upstream_port, extra),
            name=f"claude-science-front-{name}", daemon=True,
        ).start()
