"""Serve files by absolute path on a loopback HTTP listener, behind the gateway.

fileviewer runs a small HTTP server on ``127.0.0.1`` and registers its URL as
the service's ``mount``. The gateway proxies ``/s/fileviewer/<rest>`` to it, and
``httpsfront`` fronts the gateway, so a figure links as an origin-relative
``/s/fileviewer/<abs-path>`` that renders on any device that can open the notes
page. Each file ships with a ``Content-Type`` from ``mimetypes``.

**Mask.** The listener exposes the entire filesystem under the root, so a
denylist (``deny`` globs matched on the *resolved* path) hides secrets — ssh
keys, TLS private keys, the gateway auth token, ``.env`` files, ``.certs`` — and
hides the mask override file itself. A masked path 404s exactly like a missing
one; matching is on the symlink-resolved path, so a symlink to a secret can't
slip past. Globs are gitignore-shaped: a leading ``!`` re-exposes and the last
match wins, so a servable subtree can be carved out of a broad deny without
unmasking its siblings.

Peers never reach this listener: the edge carries only tool calls.
"""

from __future__ import annotations

import functools
import logging
import mimetypes
import os
import re
import shutil
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

log = logging.getLogger("awm.fileviewer.server")

# What filesystem root the listener exposes, and the origin-relative prefix the
# gateway serves it at. The root defaults to "/" — literally "all files" — with
# the mask as the safety layer. Narrow it by setting FILEVIEWER_MOUNT_ROOT to
# bound the blast radius.
SERVICE_NAME = os.environ.get("AWM_SERVICE_NAME", "fileviewer")
MOUNT_PREFIX = f"/s/{SERVICE_NAME}"
MOUNT_ROOT = os.environ.get("FILEVIEWER_MOUNT_ROOT", "/")

# Default mask: gitignore-style globs, matched whole against the resolved path relative to MOUNT_ROOT, so
# ``**`` spans directories. A leading ``!`` negates and the LAST matching glob
# wins, so any negation must be listed *before* the secret patterns or it
# re-exposes them. Denylist-shaped and best-effort — a new secret type in an
# unlisted location is exposed until added here (or via FILEVIEWER_MASK_FILE).
DEFAULT_MASK: tuple[str, ...] = (
    # git internals (may hold remote tokens in config). No escape hatch is
    # needed: a DVC-materialised file is an ordinary hardlink in the worktree,
    # so it is served under its own name and never resolves into .git.
    "**/.git/**",
    # ssh + gpg private material
    "**/.ssh/**", "**/.ssh",
    "**/.gnupg/**", "**/.gnupg",
    "**/id_rsa", "**/id_dsa", "**/id_ecdsa", "**/id_ed25519",
    "**/*_rsa", "**/*_dsa", "**/*_ecdsa", "**/*_ed25519",
    # TLS / crypto keys + certs-with-keys
    "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx",
    "**/.certs/**", "**/.certs",
    # tokens + credentials
    "**/*.token", "**/auth.token",
    "**/.aws/**", "**/.aws",
    "**/.netrc", "**/.pgpass",
    "**/credentials", "**/credentials.json",
    "**/secrets/**", "**/*.secret",
    # dotenv
    "**/.env", "**/.env.*",
)


class _State:
    """Live mount status, read by the ``fileviewer_status`` verb (never raises)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.prefix: str | None = None
        self.root: str | None = None
        self.deny: tuple[str, ...] = ()
        self.mounted = False
        self.url: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            prefix, root = self.prefix, self.root
            return {
                "mounted": self.mounted,
                "prefix": prefix,
                "root": root,
                "deny_globs": len(self.deny),
                "listen": self.url,
                # Origin-relative shape — no host/port. The notes page resolves
                # it against whatever host is serving the UI.
                "url_shape": (
                    f"{prefix}/<absolute-path-under-{root}>" if prefix else None
                ),
            }


STATE = _State()


def status() -> dict:
    """Snapshot of the mount for the status verb (never raises)."""
    return STATE.snapshot()


def load_mask() -> tuple[str, ...]:
    """DEFAULT_MASK plus any globs from FILEVIEWER_MASK_FILE (gitignore-style:
    one glob per line, ``#`` comments and blanks ignored, leading ``!`` negates).
    Override globs land after the defaults, and the last match wins — so a file
    line can both add a mask and punch a hole in one. If that file sits under the
    mount root, its own path is appended so the mask hides itself."""
    globs = list(DEFAULT_MASK)
    mf = os.environ.get("FILEVIEWER_MASK_FILE")
    if mf:
        p = Path(mf).expanduser()
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    globs.append(line)
        except OSError:
            log.warning("mask file %s unreadable; using default mask only", mf)
        # Self-hide: never serve the mask file through the mount it configures.
        globs.append(f"**/{p.name}")
        try:
            rel = p.resolve().relative_to(Path(MOUNT_ROOT).resolve()).as_posix()
            globs.append(rel)
        except (ValueError, OSError):
            pass
    # De-dupe, preserve order.
    return tuple(dict.fromkeys(globs))


class _Handler(BaseHTTPRequestHandler):
    """GET/HEAD by path under ``server.root``; anything unservable is 404."""

    server: "FileServer"

    def do_GET(self) -> None:  # noqa: N802
        self._serve(body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(body=False)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    def _serve(self, *, body: bool) -> None:
        url = urlsplit(self.path)
        target = resolve(unquote(url.path), self.server.root, self.server.deny)
        if target is None:
            self._not_found()
            return
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        try:
            f = target.open("rb")
        except OSError:
            self._not_found()
            return
        with f:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
            self.end_headers()
            if body:
                shutil.copyfileobj(f, self.wfile)

    def _not_found(self) -> None:
        data = b"not found"
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


class FileServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root: str, deny: tuple[str, ...], port: int = 0) -> None:
        self.root = Path(root).resolve()
        self.deny = deny
        super().__init__(("127.0.0.1", port), _Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


def resolve(rel: str, root: Path, deny: tuple[str, ...]) -> Path | None:
    """The file a request path names, or None when it must 404.

    The path resolves against ``root``. A target outside the root, a masked
    one, or one that is neither a file nor a directory with ``index.html``
    yields None. Missing, masked and forbidden all look alike.
    """
    rel = rel.lstrip("/")
    if "\x00" in rel:
        return None
    try:
        target = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    if not _is_within(target, root) or _is_masked(target, root, deny):
        return None
    if target.is_dir():
        target = target / "index.html"
        if _is_masked(target, root, deny):
            return None
    return target if target.is_file() else None


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _glob_segment(seg: str) -> str:
    out, i = "", 0
    while i < len(seg):
        c = seg[i]
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[" and (end := seg.find("]", i + 2)) != -1:
            body = seg[i + 1:end]
            out += "[" + ("^" + body[1:] if body.startswith("!") else body).replace("\\", "\\\\") + "]"
            i = end
        else:
            out += re.escape(c)
        i += 1
    return out


@functools.lru_cache(maxsize=1024)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """``pattern`` as a regex for a whole relative path, as Python 3.13's
    ``PurePath.full_match``: ``*`` stays in one segment, ``**`` spans zero or more."""
    segs = pattern.split("/")
    out = ""
    for i, seg in enumerate(segs):
        last = i == len(segs) - 1
        if seg == "**" and last:
            out = out[:-1] + "(?:/[^/]+)*" if out.endswith("/") else out + ".*"
        elif seg == "**":
            out += "(?:[^/]+/)*"
        else:
            out += _glob_segment(seg) + ("" if last else "/")
    return re.compile(out)


def _is_masked(target: Path, root: Path, deny: tuple[str, ...]) -> bool:
    """True if ``target`` (already resolved) is hidden by the ``deny`` globs,
    taken relative to ``root``. Matching the *resolved* path is deliberate: a
    symlink pointing at a masked file resolves to it and is caught here.
    Semantics are gitignore's: a leading ``!`` negates and the last matching
    glob wins, so keep secret patterns last."""
    if not deny:
        return False
    try:
        rel = target.relative_to(root).as_posix()
    except ValueError:
        return True
    masked = False
    for glob in deny:
        negated = glob.startswith("!")
        pattern = glob[1:] if negated else glob
        if pattern and _glob_regex(pattern).fullmatch(rel):
            masked = not negated
    return masked


def start(port: int | None = None) -> FileServer:
    """Bind the listener, serve it on a daemon thread, and record it in STATE.

    ``port`` defaults to ``FILEVIEWER_PORT``, else an ephemeral port: the
    gateway learns the URL from the registration, so nothing else needs it.
    """
    if port is None:
        port = int(os.environ.get("FILEVIEWER_PORT") or 0)
    mask = load_mask()
    srv = FileServer(MOUNT_ROOT, mask, port)
    threading.Thread(target=srv.serve_forever, name="fileviewer-http", daemon=True).start()
    with STATE.lock:
        STATE.prefix, STATE.root, STATE.deny = MOUNT_PREFIX, MOUNT_ROOT, mask
        STATE.mounted, STATE.url = True, srv.url
    log.info("fileviewer listening on %s: %s → %s (%d deny globs)",
             srv.url, MOUNT_PREFIX, MOUNT_ROOT, len(mask))
    return srv
