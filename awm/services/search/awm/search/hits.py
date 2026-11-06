"""The shape of a hit and of one source's answer, and the cleaning of answers that come from elsewhere.

A hit is its ``id`` and the fields its source's declaration marks as ``hit``;
``search get`` reads the document. A live source or a peer answers in sections.
Whatever arrives is cut down to those keys, so a source or a peer can neither
inject fields nor quote text.
"""

from __future__ import annotations

from typing import Any, Sequence

#: The sections of one source's answer, in the order they are read.
SECTIONS = ("exact", "related")
_CHARS = 2000
_VALUES = 50


def _value(raw: Any) -> Any:
    if isinstance(raw, list):
        return [str(x)[:_CHARS] for x in raw[:_VALUES] if isinstance(x, (str, int, float))]
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return raw
    return str(raw if raw is not None else "")[:_CHARS]


def clean(raw: Any, fields: Sequence[str]) -> dict[str, Any] | None:
    """A hit from elsewhere as ``{id, <fields>}``, or ``None`` when it names no document."""
    if not isinstance(raw, dict) or raw.get("id") in (None, ""):
        return None
    return {"id": str(raw["id"])[:_CHARS], **{f: _value(raw.get(f)) for f in fields}}


def section(raw: Any, limit: int, fields: Sequence[str]) -> list[dict[str, Any]]:
    """A list of hits from elsewhere, cleaned and capped at ``limit``. A section sent as one list per source
    counts as their concatenation."""
    if isinstance(raw, dict):
        raw = [h for hits in raw.values() if isinstance(hits, list) for h in hits]
    if not isinstance(raw, list):
        return []
    return [h for h in (clean(x, fields) for x in raw) if h][:limit]
