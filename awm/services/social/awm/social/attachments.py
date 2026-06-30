"""Attachment download sink — write fetched file bytes to a temp directory.

Downloaded attachments deliberately land in a **system temp dir** (honoring
``$TMPDIR``), NEVER under ``AWM_DIR``: the social service hoards no state, and a
manuscript pulled from Slack is a transient artifact the caller copies elsewhere
if they want to keep it. Each ``download_attachments`` call gets its own
``mkdtemp`` subdir so concurrent downloads never collide on filenames.
"""

from __future__ import annotations

import os
import re
import tempfile

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


def write_attachments(
    files: list[tuple[str, str, bytes]],
) -> list[dict]:
    """Write ``(filename, mime, bytes)`` tuples to a fresh temp dir.

    Returns one metadata dict per file — ``{filename, mime, size, path}`` — with
    ``path`` the absolute location on disk. De-duplicates colliding basenames by
    suffixing ``-1``, ``-2`` … so two attachments named ``draft.docx`` both land.
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
        })
    return out
