"""The shape of a hit and of one source's answer, and the cleaning of answers that come from elsewhere.

A hit is exactly ``{id, title, ts}``; ``search get`` reads the document. A live
source or a peer answers in sections. Whatever arrives is cut down to those
keys, so a source or a peer can neither inject fields nor quote text.
"""

from __future__ import annotations

from typing import Any

#: The sections of one source's answer, in the order they are read.
SECTIONS = ("exact", "related")


def clean(raw: Any) -> dict[str, Any] | None:
    """A hit from elsewhere as ``{id, title, ts}``, or ``None`` when it names no document."""
    if not isinstance(raw, dict) or raw.get("id") in (None, ""):
        return None
    return {"id": str(raw["id"])[:2000], "title": str(raw.get("title") or "")[:2000],
            "ts": str(raw.get("ts") or "")[:64]}


def section(raw: Any, limit: int) -> list[dict[str, Any]]:
    """A list of hits from elsewhere, cleaned and capped at ``limit``. A section sent as one list per source
    counts as their concatenation."""
    if isinstance(raw, dict):
        raw = [h for hits in raw.values() if isinstance(hits, list) for h in hits]
    if not isinstance(raw, list):
        return []
    return [h for h in (clean(x) for x in raw) if h][:limit]
