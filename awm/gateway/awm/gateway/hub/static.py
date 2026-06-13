"""Static-directory serving for ``kind="static"`` registrations.

Mirrors ``proxy.py`` in role: where ``proxy_http`` forwards a request to a
registered URL, ``serve_static`` answers it from a registered directory.

The hub uses ``starlette.responses.FileResponse`` for the actual byte
shipping — we don't mount a ``StaticFiles`` sub-app because the prefix is
chosen per record at registration time, not at app-construction time.

Canonical paths only: each registered URL maps to a deterministic file
on disk — the file at that exact path, or the directory's
``index.html``. No SPA fallback, no ``Accept``-conditional synthesis on
miss. A registered prefix that wants deep-link refresh must prerender a
real ``index.html`` at every URL its UI exposes. For SPA-shaped routing
without per-route prerendering, use ``kind=url`` against a real
upstream.

Auto-shell exception: if the registered dir has no ``index.html`` and the
record carries an ``entry``, ``serve_static`` synthesises a minimal ESM
page **at the prefix root only** so a "naked bundle" (just ``main.js`` ±
css) is viewable without the scope hand-authoring HTML. This is still
canonical — the synthesized HTML answers exactly the prefix-root URL.
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

from awm.gateway.hub.registry import ServiceRecord

log = logging.getLogger("awm.hub.static")


async def serve_static(request: Request, rec: ServiceRecord) -> Response:
    """Resolve the request path against ``rec.static_dir`` and return a Response.

    Path resolution is contained: any target that resolves outside the
    registered directory is rejected as 404 (no traversal — we don't even
    distinguish forbidden from missing).

    Canonical-paths contract: the resolved URL maps to one specific file
    on disk, or 404. The mapping is:

    * URL has a file at that exact path → serve that file.
    * URL points at a directory that contains ``index.html`` → serve
      that ``index.html``. This is the universal static-server default
      ("directory URLs are served by their index file") used by nginx,
      Apache, GitHub Pages, Netlify, etc.; SvelteKit's adapter-static
      with ``trailingSlash: 'always'`` emits exactly this layout
      (``<route>/index.html``) so a deep link to ``/focus`` is answered
      by ``focus/index.html`` without any ``Accept`` sniffing or shell
      fallback.
    * URL points at a directory with no ``index.html``, or at no file
      at all → 404.

    No ``Accept``-conditional behaviour, no extension sniffing, no
    fallback to the prefix root's ``index.html`` on miss. The only
    response that isn't bytes-from-disk is the naked-bundle auto-shell
    at the prefix root (records carrying an ``entry``).
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
    if target.is_file():
        return FileResponse(target)
    if target.is_dir():
        index = target / "index.html"
        if index.is_file():
            return FileResponse(index)
    return PlainTextResponse("not found", status_code=404)


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
