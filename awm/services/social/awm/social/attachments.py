"""Attachment download sink — write fetched file bytes to a temp directory.

Downloaded attachments deliberately land in a **system temp dir** (honoring
``$TMPDIR``), NEVER under ``AWM_DIR``: the social service hoards no state, and a
manuscript pulled from Slack is a transient artifact the caller copies elsewhere
if they want to keep it. Each ``download_attachments`` call gets its own
``mkdtemp`` subdir so concurrent downloads never collide on filenames.

``path`` is only meaningful on the node that ran the download, and a borrowed
``social`` runs on its owner — so every file also carries ``url``, the
origin-relative address at which THIS node's ``fileviewer`` mount serves those
same bytes. That is what lets a caller on another node turn the reply into a
local file (``gatewayclient.fetch_peer_file``) instead of a path to nothing.
Bytes still never inline into the RPC payload; only their address does.
"""

from __future__ import annotations

import os
import re
import tempfile
from urllib.parse import quote

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(name: str, idx: int) -> str:
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


def write_attachments(
    files: list[tuple[str, str, bytes]],
) -> list[dict]:
    """Write ``(filename, mime, bytes)`` tuples to a fresh temp dir.

    Returns one metadata dict per file — ``{filename, mime, size, path, url}`` —
    with ``path`` the absolute location on disk *of the node that ran this* and
    ``url`` the same bytes' address on that node's ``fileviewer`` mount (see the
    module docstring). De-duplicates colliding basenames by suffixing ``-1``,
    ``-2`` … so two attachments named ``draft.docx`` both land.
    """
    if not files:
        return []
    out_dir = tempfile.mkdtemp(prefix="awm-social-")
    used: set[str] = set()
    out: list[dict] = []
    for idx, (filename, mime, data) in enumerate(files):
        name = _safe_name(filename, idx)
        candidate = name
        n = 1
        while candidate in used:
            stem, dot, ext = name.partition(".")
            candidate = f"{stem}-{n}{dot}{ext}"
            n += 1
        used.add(candidate)
        path = os.path.join(out_dir, candidate)
        with open(path, "wb") as fh:
            fh.write(data)
        out.append({
            "filename": filename or candidate,
            "mime": mime or "application/octet-stream",
            "size": len(data),
            "path": path,
            "url": file_url(path),
        })
    return out
