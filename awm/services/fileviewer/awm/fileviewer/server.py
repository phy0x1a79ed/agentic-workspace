"""Register fileviewer's bytes as a gateway ``kind=static`` mount and hold its lease.

fileviewer no longer runs its own HTTP listener. Instead it registers a
``kind=static`` mount at ``/files`` on the gateway (root ``FILEVIEWER_MOUNT_ROOT``,
default ``/``) and holds that mount's WS lease for the life of the process. The
gateway's ``serve_static`` ships each file's bytes with a correct ``Content-Type``,
and ``httpsfront`` fronts the whole gateway — so a figure links as an
origin-relative ``/files/<abs-path>`` that renders for any device that can open
the notes page, not just a browser on the server's own loopback. That is the
whole point of the change: the old ``http://127.0.0.1:12210/?path=…`` links were
loopback-only and never reachable through the HTTPS front.

**Mask.** The mount exposes the entire filesystem under the root, so a denylist
(a per-mount ``deny`` glob list, enforced gateway-side in ``serve_static`` on the
*resolved* path) hides secrets — ssh keys, TLS private keys, the gateway auth
token, ``.env`` files, ``.certs`` — and hides the mask override file itself. A
masked path 404s exactly like a missing one; matching is on the symlink-resolved
path, so a symlink to a secret can't slip past. Globs are gitignore-shaped: a
leading ``!`` re-exposes and the last match wins, so a servable subtree can be
carved out of a broad deny without unmasking its siblings.

Two records, two leases: the ``ServiceAdapter`` (``kind=service`` at
``/svc/fileviewer``) gives supervision + the ``fileviewer_status`` verb; this
module owns the separate ``kind=static`` mount and its lease. The control WS does
not cover the mount, so ``hold_mount`` runs its own register + hold-lease +
reconnect loop — it must survive the gateway bouncing (records are in-memory) or
the mount silently vanishes after a gateway restart.

Pure-ish stdlib plus ``httpx`` / ``websockets`` (already deps of the adapter).
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import threading
from pathlib import Path

import httpx
import websockets

log = logging.getLogger("awm.fileviewer.server")

# Where the mount lives on the origin, and what filesystem root it exposes. The
# root defaults to "/" — literally "all files" — with the mask as the safety
# layer. Narrow it by setting FILEVIEWER_MOUNT_ROOT to bound the blast radius.
SERVICE_NAME = os.environ.get("AWM_SERVICE_NAME", "fileviewer")
MOUNT_PREFIX = os.environ.get("FILEVIEWER_MOUNT_PREFIX", "/files")
MOUNT_ROOT = os.environ.get("FILEVIEWER_MOUNT_ROOT", "/")

# Default mask: gitignore-style globs, matched by the gateway with
# PurePosixPath.full_match against the resolved path relative to MOUNT_ROOT, so
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
        self.service_id: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            prefix, root = self.prefix, self.root
            return {
                "mounted": self.mounted,
                "prefix": prefix,
                "root": root,
                "deny_globs": len(self.deny),
                "service_id": self.service_id,
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


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # loopback, self-signed gateway cert
    return ctx


async def hold_mount() -> None:
    """Register the ``kind=static`` mount and hold its lease forever.

    Registers ``{name, prefix:/files, static:{dir:<root>, deny:<mask>}}`` on the
    hub, then holds the returned ``/hub/lease/{id}`` WS open. On any drop —
    network hiccup or a gateway restart that wipes the in-memory registry — it
    re-registers (idempotent: same name+prefix replaces in place, minting a fresh
    id) and re-holds, with capped exponential backoff. Meant to be launched as a
    background task from the adapter's ``on_start`` (it runs in the adapter's
    event loop and never returns)."""
    hub_url = os.environ.get("AWM_HUB_URL", "").rstrip("/")
    if not hub_url:
        log.error("AWM_HUB_URL not set; fileviewer mount cannot register")
        return
    ws_base = hub_url.replace("https://", "wss://").replace("http://", "ws://")
    ssl_ctx = _ssl_ctx() if ws_base.startswith("wss://") else None

    mask = load_mask()
    with STATE.lock:
        STATE.prefix, STATE.root, STATE.deny = MOUNT_PREFIX, MOUNT_ROOT, mask

    backoff = 1.0
    while True:
        try:
            async with httpx.AsyncClient(verify=False, timeout=15) as cli:
                r = await cli.post(f"{hub_url}/hub/register", json={
                    "name": SERVICE_NAME,
                    "prefix": MOUNT_PREFIX,
                    "static": {"dir": MOUNT_ROOT, "deny": list(mask)},
                })
                r.raise_for_status()
            body = r.json()
            sid = body["service_id"]
            lease_path = body["lease_ws_path"]
            log.info("fileviewer mount up: %s → %s (id=%s, %d deny globs)",
                     MOUNT_PREFIX, MOUNT_ROOT, sid, len(mask))
            with STATE.lock:
                STATE.mounted, STATE.service_id = True, sid
            backoff = 1.0
            async with websockets.connect(
                f"{ws_base}{lease_path}",
                ssl=ssl_ctx, max_size=None, open_timeout=10,
            ) as ws:
                # First frame is {"type":"ready",...}; then just hold the socket
                # open (iterating drains frames and returns when the hub closes).
                async for _ in ws:
                    pass
            log.info("fileviewer mount lease closed; re-registering")
        except Exception as exc:  # noqa: BLE001 — stay up across any transient fault
            log.warning("fileviewer mount lost (%s); re-registering in %.1fs",
                        exc, backoff)
        finally:
            with STATE.lock:
                STATE.mounted = False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)
