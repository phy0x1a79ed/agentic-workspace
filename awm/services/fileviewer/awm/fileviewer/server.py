"""The loopback file-serving HTTP listener (pure stdlib, no pip deps).

Point a browser at ``http://127.0.0.1:<port>/?path=/abs/path/thing.svg`` and it
gets that file's bytes with a correct ``Content-Type``, so the browser picks the
right native renderer: SVG draws, HTML renders, PNG/JPEG display as images,
``.py``/``.md``/``.json``/``.log`` show inline as readable text. A missing,
directory, or unreadable path returns an HTTP 404 with a small styled not-found
page (never a stack trace, never a JSON blob).

Why off-hub: the awm hub function channel is JSON-only — a handler's return
value is always JSON-serialized — so it can't hand a browser raw bytes with a
real ``Content-Type``. So this rides its own listener, exactly as ``mic`` serves
audio off-hub; the hub registration only supervises the process and exposes a
``status`` verb.

Bound ``127.0.0.1:<port>`` only — a local viewer, no remote exposure. Any
absolute path the awm user can read is viewable (no workspace-root restriction,
per design); single-file only, so relative ``src``/``href`` inside an HTML/SVG
document won't resolve.
"""

from __future__ import annotations

import html
import logging
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("awm.fileviewer.server")

# Cap the sniff read when deciding text-vs-binary for an unknown extension.
_SNIFF_BYTES = 8192


class _State:
    """Live listener status, read by the ``fileviewer_status`` verb."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.bind: str | None = None
        self.port: int | None = None
        self.serving = False
        self.requests = 0

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "bind": self.bind,
                "port": self.port,
                "serving": self.serving,
                "requests": self.requests,
                "url_shape": (
                    f"http://{self.bind}:{self.port}/?path=/abs/path/to/file"
                    if self.bind and self.port
                    else None
                ),
            }


STATE = _State()


def status() -> dict:
    """Snapshot of the listener for the status verb (never raises)."""
    return STATE.snapshot()


def _looks_textual(sample: bytes) -> bool:
    """Heuristic: is this byte sample human-readable text? A NUL byte means
    binary; otherwise require it to decode as UTF-8. Used only when the
    extension gives us no MIME type, so a ``.py``/``.md``/``.log``/dotfile
    displays inline instead of prompting a download."""
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _content_type(path: Path, sample: bytes) -> str:
    """Pick a Content-Type. Trust ``mimetypes`` when it knows the extension;
    for text/* ensure a charset so browsers render inline. When it doesn't
    know (many code files), sniff: textual → ``text/plain; charset=utf-8`` so
    it displays; genuine binary → ``application/octet-stream``."""
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        if guessed.startswith("text/") and "charset" not in guessed:
            return f"{guessed}; charset=utf-8"
        return guessed
    if _looks_textual(sample):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


_NOT_FOUND_TMPL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Not found — fileviewer</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:#0d1117; color:#c9d1d9; }}
  @media (prefers-color-scheme: light) {{ body {{ background:#f6f8fa; color:#1f2328; }} }}
  .card {{ max-width:640px; padding:2rem 2.5rem; text-align:center; }}
  h1 {{ font-size:3rem; margin:0 0 .25rem; letter-spacing:-.02em; }}
  p {{ margin:.4rem 0; opacity:.8; }}
  code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
          background:rgba(128,128,128,.18); padding:.15em .4em; border-radius:5px;
          word-break:break-all; }}
</style></head>
<body><div class="card">
  <h1>404</h1>
  <p>{reason}</p>
  <p><code>{path}</code></p>
</div></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence stdlib access logging
        pass

    def do_GET(self) -> None:
        with STATE.lock:
            STATE.requests += 1
        qs = parse_qs(urlsplit(self.path).query)
        raw = (qs.get("path") or [""])[0]
        if not raw:
            return self._not_found(
                "", "No file path given. Use ?path=/absolute/path/to/file."
            )

        try:
            resolved = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return self._not_found(raw, "That path could not be resolved.")

        if not resolved.is_absolute():
            return self._not_found(raw, "The path must be absolute.")
        if resolved.is_dir():
            return self._not_found(
                raw, "That path is a directory — only single files are viewable."
            )
        if not resolved.is_file():
            return self._not_found(raw, "No file exists at that path.")

        try:
            body = resolved.read_bytes()
        except OSError:
            return self._not_found(raw, "That file could not be read.")

        ctype = _content_type(resolved, body[:_SNIFF_BYTES])
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_HEAD = do_GET

    def _not_found(self, attempted: str, reason: str) -> None:
        page = _NOT_FOUND_TMPL.format(
            reason=html.escape(reason),
            path=html.escape(attempted or "(no path)"),
        ).encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(page)


def serve(*, port: int, bind: str = "127.0.0.1") -> None:
    """Bind ``bind:port`` and serve forever (blocks).

    Designed to run in a daemon thread launched from the hub adapter's
    ``on_start``. ``ThreadingHTTPServer`` sets ``allow_reuse_address`` so a
    gateway respawn rebinds the port cleanly. Binds loopback by default — a
    local viewer, no remote reach.
    """
    # Register a few code/text extensions stdlib's mimetypes may not know, so
    # they get a proper text/* type rather than falling to the sniff path.
    for ext, ct in (
        (".md", "text/markdown"),
        (".toml", "text/plain"),
        (".log", "text/plain"),
        (".ts", "text/plain"),
        (".tsx", "text/plain"),
        (".jsx", "text/plain"),
        (".rs", "text/plain"),
        (".yml", "text/plain"),
        (".yaml", "text/plain"),
    ):
        mimetypes.add_type(ct, ext)

    with STATE.lock:
        STATE.bind = bind
        STATE.port = port
        STATE.serving = True

    srv = ThreadingHTTPServer((bind, port), Handler)
    log.info("fileviewer listening on http://%s:%d/?path=…", bind, port)
    try:
        srv.serve_forever()
    finally:
        with STATE.lock:
            STATE.serving = False
