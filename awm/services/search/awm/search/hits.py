"""The shape of a hit and of the three sections, and the cleaning of hits that come from elsewhere.

A hit is one document with up to a few passages, each a quote with its span in
the document. A live source or a peer answers in sections, or, on an older
release, with one ``hits`` list that counts as ``related``. Whatever arrives is
cut down to the known keys, so a source or a peer can neither inject fields
nor claim another origin.
"""

from __future__ import annotations

from typing import Any

#: The answer's sections, in the order they are read: literal and keyword, meaning, graph.
SECTIONS = ("exact", "related", "graph")
_TEXT_KEYS = ("source", "id", "title", "ts", "project", "scope", "kind", "url", "match")
_QUOTE_CHARS = 2000
MAX_PASSAGES = 10


def _span(value: Any) -> list[int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(x, int) for x in value):
        return [int(value[0]), int(value[1])]
    return None


def _passage(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str) or not raw["text"].strip():
        return None
    out: dict[str, Any] = {"text": raw["text"][:_QUOTE_CHARS]}
    for key in ("start", "end"):
        if isinstance(raw.get(key), int):
            out[key] = raw[key]
    for key in ("section", "field"):
        if isinstance(raw.get(key), str) and raw[key]:
            out[key] = raw[key][:200]
    listed = raw.get("marks") if isinstance(raw.get("marks"), list) else []
    marks = [m for m in map(_span, listed) if m]
    if marks:
        out["marks"] = marks[:20]
    return out


def clean(raw: Any, origin: dict[str, str], source: str | None = None) -> dict[str, Any] | None:
    """A foreign hit as ours: known keys only, ``origin`` stamped, a bare snippet turned into a passage."""
    if not isinstance(raw, dict) or not raw.get("id") or not (source or raw.get("source")):
        return None
    hit = {k: str(raw[k])[:2000] for k in _TEXT_KEYS if raw.get(k) not in (None, "")}
    if source:
        hit["source"] = source
    quotes = raw.get("passages")
    passages = [p for p in (_passage(x) for x in quotes) if p] if isinstance(quotes, list) else []
    snippet = " ".join(str(raw.get("snippet") or "").split())
    if not passages and snippet:
        passages = [{"text": snippet[:_QUOTE_CHARS]}]
    return {"title": "", "ts": "", **hit, "passages": passages[:MAX_PASSAGES], "score": 0.0, "origin": origin}


def sections(raw: Any, origin: dict[str, str], limit: int,
             source: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Each section of an answer, cleaned and capped at ``limit``; a bare ``hits`` list is ``related``."""
    if not isinstance(raw, dict):
        raise ValueError("the answer is not an object")
    if any(isinstance(raw.get(s), list) for s in SECTIONS):
        lists = {s: raw.get(s) if isinstance(raw.get(s), list) else [] for s in SECTIONS}
    elif isinstance(raw.get("hits"), list):
        lists = {"exact": [], "related": raw["hits"], "graph": []}
    else:
        raise ValueError("the answer has no sections and no hits")
    return {s: [h for h in (clean(x, origin, source) for x in items) if h][:limit]
            for s, items in lists.items()}


def count(found: dict[str, list[dict[str, Any]]]) -> int:
    return sum(len(v) for v in found.values())
