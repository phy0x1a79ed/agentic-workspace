"""``/_signin``: the zero-click owner sign-in behind auth's ``gui`` front.

The workbench has its own owner authentication on top of awm's, so a browser
that has already logged into awm would still face a nonce only the CLI can
satisfy. This route mints a fresh single-use nonce and auto-submits it,
same-origin, to the workbench's ``/api/auth/nonce``.

The manifest's ``paths`` table sends only this prefix here, so auth's edge
session gates it like every proxied path. ``X-Awm-As``, which only auth writes
on that hop and which a page in a local browser cannot send without a preflight
this server never answers, keeps it from being a local open door.
"""

from __future__ import annotations

import html
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from awm.claude_science import api

log = logging.getLogger("awm.claude_science.signin")

#: Loopback port for this route. The ``paths`` entry in ``awm-plugin.toml`` names it too.
PORT = 12210


def _page(nonce: str, dest: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Signing in…</title>"
        "<body style=\"font:15px system-ui,sans-serif;max-width:28em;"
        "margin:5em auto;padding:0 1em;color:#222\">"
        "<h2>Signing in…</h2>"
        "<p>If this doesn't proceed automatically, use the button.</p>"
        "<form id=f method=post action=\"/api/auth/nonce\">"
        f"<input type=hidden name=nonce value=\"{html.escape(nonce)}\">"
        f"<input type=hidden name=dest value=\"{html.escape(dest)}\">"
        "<button type=submit>Sign in</button></form>"
        "<script>document.getElementById('f').submit()</script>"
    )


def _handler(supervisor: Any) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: str) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            # The embedded nonce is single-use, so a cached copy is a page that cannot work.
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            if parts.path != "/_signin" or not self.headers.get("x-awm-as"):
                self._reply(404, "not found")
                return
            try:
                nonce = api.nonce_from(supervisor.login_url())
            except api.WorkbenchError:
                self._reply(502, "<h2>Sign-in unavailable</h2><p>No nonce in "
                                 "<code>claude-science url</code> output.</p>")
                return
            except Exception as exc:  # noqa: BLE001 — report, never 500
                log.warning("signin: minting a nonce failed: %s", exc)
                self._reply(502, "<h2>Sign-in unavailable</h2><p>Could not mint a login nonce. "
                                 "Is the workbench running?</p>")
                return
            self._reply(200, _page(nonce, parse_qs(parts.query).get("dest", ["/"])[0]))

        def log_message(self, *_args: Any) -> None:
            pass

    return Handler


def start(supervisor: Any) -> None:
    """Serve the route on loopback from a daemon thread."""
    server = ThreadingHTTPServer(("127.0.0.1", PORT), _handler(supervisor))
    threading.Thread(target=server.serve_forever, name="claude-science-signin", daemon=True).start()
