"""The shape of a hit and of the two sections, and the cleaning of hits that come from elsewhere.

A hit names one document: its source, id, title, date and scope, and no text;
``search get`` reads the document. A live source or a peer answers in sections,
or, on an older release, with one ``hits`` list that counts as ``related``.
Whatever arrives is cut down to the known keys, so a source or a peer can
neither inject fields, quote text nor claim another origin.
"""

from __future__ import annotations

from typing import Any

#: The answer's sections, in the order they are read: literal and keyword, then meaning.
SECTIONS = ("exact", "related")
#: A section a peer on an older release may still send. It is dropped, never read.
RETIRED_SECTIONS = ("graph",)
_TEXT_KEYS = ("source", "id", "title", "ts", "project", "scope", "kind", "url", "match")


def clean(raw: Any, origin: dict[str, str], source: str | None = None) -> dict[str, Any] | None:
    """A peer's hit as ours: known keys only, ``origin`` stamped, any passages or snippet dropped."""
    if not isinstance(raw, dict) or not raw.get("id") or not (source or raw.get("source")):
        return None
    hit = {k: str(raw[k])[:2000] for k in _TEXT_KEYS if raw.get(k) not in (None, "")}
    if source:
        hit["source"] = source
    return {"title": "", "ts": "", **hit, "score": 0.0, "origin": origin}


def bare(hit: dict[str, Any]) -> dict[str, Any]:
    """``hit`` without its passages."""
    return {k: v for k, v in hit.items() if k != "passages"}


def _flat(section: Any) -> list[Any]:
    """A section's hits as one list: a flat list as sent, or the per-source lists of a current answer in turn."""
    if isinstance(section, dict):
        return [h for hits in section.values() if isinstance(hits, list) for h in hits]
    return section if isinstance(section, list) else []


def sections(raw: Any, origin: dict[str, str], limit: int,
             source: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Each section of an answer, cleaned and capped at ``limit`` hits per source; a bare ``hits`` list is
    ``related``. A section arrives as one flat list or as one list per source, and leaves as one flat list.
    A retired section is dropped."""
    if not isinstance(raw, dict):
        raise ValueError("the answer is not an object")
    if any(isinstance(raw.get(s), (list, dict)) for s in (*SECTIONS, *RETIRED_SECTIONS)):
        lists = {s: _flat(raw.get(s)) for s in SECTIONS}
    elif isinstance(raw.get("hits"), list):
        lists = {"exact": [], "related": raw["hits"]}
    else:
        raise ValueError("the answer has no sections and no hits")
    return {s: _cap([h for h in (clean(x, origin, source) for x in items) if h], limit)
            for s, items in lists.items()}


def _cap(hits: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The first ``limit`` hits of each source."""
    taken: dict[str, int] = {}
    out = []
    for hit in hits:
        taken[hit["source"]] = taken.get(hit["source"], 0) + 1
        if taken[hit["source"]] <= limit:
            out.append(hit)
    return out


def count(found: dict[str, list[dict[str, Any]]]) -> int:
    return sum(len(v) for v in found.values())
