"""Attachment replies — file bytes carried inline, and the temp sink for bucket downloads.

``download_attachments`` returns each file as ``{filename, mime, size, b64}``. It
never names a path: ``social`` is a singleton that runs on one node, in a systemd
unit with a private ``/tmp``, so a path it wrote is unreadable to a session on
that node as much as to one on another. The gateway of the caller's node writes the
bytes into the caller's own temp dir and rewrites the reply (``awm-gateway``'s
``files`` module). The total is capped at :data:`MAX_INLINE_BYTES` so a reply stays
a reasonable RPC payload.

``bucket_get`` still writes into :func:`sink_dir`, because a folder is a tree.
"""

from __future__ import annotations

import base64
import os
import re
import tempfile
from urllib.parse import quote

# What one reply carries inline, raw bytes before base64.
MAX_INLINE_BYTES = 24 * 1024 * 1024

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def sink_dir() -> str:
    """A fresh temp dir (outside AWM_DIR) for one call's downloaded files."""
    return tempfile.mkdtemp(prefix="awm-social-")


def safe_name(name: str, idx: int = 0) -> str:
    """A filesystem-safe, non-empty basename for one attachment.

    Strips any directory components (a platform-supplied name is untrusted) and
    collapses unsafe characters; falls back to ``attachment-<idx>`` when nothing
    usable remains.
    """
    base = os.path.basename(name or "").strip()
    base = _SAFE.sub("_", base).strip("._")
    return base or f"attachment-{idx}"


def file_url(path: str) -> str | None:
    """Origin-relative URL at which this node's ``fileviewer`` mount serves ``path``.

    ``None`` when the file lies outside the mount root and therefore is not
    reachable at all — an honest absence beats a URL that will 404. The mount's
    prefix and root are read from the environment rather than imported, because
    ``fileviewer`` is a separate dist; the defaults match its own.
    """
    prefix = (os.environ.get("FILEVIEWER_MOUNT_PREFIX") or "/files").rstrip("/")
    root = os.environ.get("FILEVIEWER_MOUNT_ROOT") or "/"
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except ValueError:  # different drive/anchor — not under the root
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return f"{prefix}/{quote(rel.replace(os.sep, '/'))}"


def inline_attachments(files: list[tuple[str, str, bytes]]) -> list[dict]:
    """``(filename, mime, bytes)`` tuples as reply entries carrying their bytes.

    Raises ``ValueError`` when the files together pass :data:`MAX_INLINE_BYTES`.
    """
    total = sum(len(data) for _, _, data in files)
    if total > MAX_INLINE_BYTES:
        raise ValueError(
            f"the attachments come to {total} bytes, over the {MAX_INLINE_BYTES} "
            "a reply carries; fetch one at a time with idx")
    return [{
        "filename": safe_name(filename, idx),
        "mime": mime or "application/octet-stream",
        "size": len(data),
        "b64": base64.b64encode(data).decode("ascii"),
    } for idx, (filename, mime, data) in enumerate(files)]
