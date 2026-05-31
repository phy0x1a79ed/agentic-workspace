"""Static-directory serving for ``kind="static"`` registrations.

Mirrors ``proxy.py`` in role: where ``proxy_http`` forwards a request to a
registered URL, ``serve_static`` answers it from a registered directory.

The hub uses ``starlette.responses.FileResponse`` for the actual byte
shipping — we don't mount a ``StaticFiles`` sub-app because the prefix is
chosen per record at registration time, not at app-construction time.

Auto-shell: if the registered dir has no ``index.html`` and the record
carries an ``entry``, ``serve_static`` synthesises a minimal ESM page at
the prefix root so a "naked bundle" (just ``main.js`` ± css) is viewable
without the scope hand-authoring HTML.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path

from fastapi import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    Response,
)

from awm.services.hub.registry import ServiceRecord

log = logging.getLogger("awm.hub.static")


async def serve_static(request: Request, rec: ServiceRecord) -> Response:
    """Resolve the request path against ``rec.static_dir`` and return a Response.

    Path resolution is contained: any target that resolves outside the
    registered directory is rejected as 404 (no traversal — we don't even
    distinguish forbidden from missing).
    """
    rel = request.url.path[len(rec.prefix):].lstrip("/")
    root = Path(rec.static_dir).resolve()

    if rel == "":
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index)
        if rec.entry:
            return HTMLResponse(_render_shell(rec))
        return PlainTextResponse("not found", status_code=404)

    try:
        target = (root / rel).resolve()
    except OSError:
        return PlainTextResponse("not found", status_code=404)
    if not _is_within(target, root):
        return PlainTextResponse("not found", status_code=404)
    if not target.is_file():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(target)


async def close_ws_unsupported(scope, receive, send) -> None:
    """Accept + immediately close a WebSocket scope with code 1003.

    Static prefixes have no WS semantics; we accept first so the client
    sees a clean close rather than a handshake reject."""
    await send({"type": "websocket.accept"})
    await send({
        "type": "websocket.close",
        "code": 1003,
        "reason": "static prefix does not accept websocket connections",
    })


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _render_shell(rec: ServiceRecord) -> str:
    """Render the opinionated ESM shell for a naked component bundle.

    Fixed shape: an empty mount node, optional CSS links, and a module
    script pointing at ``rec.entry``. Anything fancier (importmaps,
    multiple entries, framework-specific bootstrap) is handled by the
    scope shipping its own ``index.html``.
    """
    prefix = rec.prefix.rstrip("/")
    title = html.escape(rec.name)
    mount = html.escape(rec.mount_id, quote=True)
    entry_url = f"{prefix}/{rec.entry}" if rec.entry else ""
    css_links = "\n".join(
        f'<link rel="stylesheet" href="{prefix}/{html.escape(href, quote=True)}">'
        for href in rec.css
    )
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<title>{title}</title>\n'
        f'{css_links}\n'
        "</head>\n"
        "<body>\n"
        f'<div id="{mount}"></div>\n'
        f'<script type="module" src="{html.escape(entry_url, quote=True)}"></script>\n'
        "</body>\n"
        "</html>\n"
    )
