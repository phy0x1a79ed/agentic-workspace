"""Pages the Zotero library mirror as search documents, for the archive's `kb` source.

The mirror is the same `library.json` the kb server falls back to when no node holds a
Zotero key, so the documents are metadata and abstracts and need neither the server nor a
network call. Paging is keyset: a cursor is the last key sent, so edits to the library
between pages never repeat or skip a surviving paper.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PAGE_DEFAULT = 200
PAGE_MAX = 1000


def _name(creator: Any) -> str:
    if isinstance(creator, dict):
        return str(creator.get("lastName") or creator.get("name") or "")
    return str(creator)


def _timestamp(item: dict, pulled: str) -> str:
    year = str(item.get("year") or "")
    return f"{year}-01-01T00:00:00Z" if re.fullmatch(r"\d{4}", year) else pulled


def _url(item: dict) -> str | None:
    if item.get("url"):
        return str(item["url"])
    return f"https://doi.org/{item['doi']}" if item.get("doi") else None


def to_document(item: dict, pulled: str = "") -> dict[str, Any]:
    """One library item as a search document; the text matches what the kb server indexes."""
    authors = ", ".join(_name(c) for c in (item.get("creators") or [])[:6])
    title = item.get("title") or ""
    text = (f"{title}\n{authors} ({item.get('year') or 'n.d.'}). {item.get('publication') or ''}. "
            f"doi:{item.get('doi') or '-'}\n\n{item.get('abstract') or ''}")
    doc: dict[str, Any] = {"id": item["key"], "title": title, "text": text,
                           "ts": _timestamp(item, pulled), "kind": "paper"}
    url = _url(item)
    if url:
        doc["url"] = url
    return doc


def export_page(library: Path, cursor: str | None, limit: int | None) -> dict[str, Any]:
    """The next page of papers after `cursor`; the last page also carries every id in the library."""
    raw = json.loads(library.read_text())
    pulled = raw.get("pulled") or ""
    items = sorted((i for i in raw.get("items") or [] if i.get("key")), key=lambda i: i["key"])
    size = min(max(int(limit or PAGE_DEFAULT), 1), PAGE_MAX)
    rest = [i for i in items if not cursor or i["key"] > cursor]
    page, more = rest[:size], len(rest) > size
    out: dict[str, Any] = {"documents": [to_document(i, pulled) for i in page],
                           "next_cursor": page[-1]["key"] if more else None, "done": not more}
    if not more:
        out["ids"] = [i["key"] for i in items]
    return out
