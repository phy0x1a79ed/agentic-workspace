"""The live archive query: a free-text search over archived transcripts, shaped as search hits."""

from __future__ import annotations

import re

from awm.transcripts import search

MAX_HITS = 20


def hits(query: str, *, project: str | None = None, limit: int = MAX_HITS) -> dict:
    """Archived lines that contain ``query`` as typed, ignoring case, as ``{id,title,snippet}`` hits.

    The query is matched literally: it is words from a person, not a pattern.
    """
    found = search.find("(?i)" + re.escape(query.strip()), project=project, limit=limit)
    return {"hits": [{"id": f"{h['session']}:{h['line']}", "title": h["project"],
                      "snippet": h["text"], "project": h["project"]} for h in found["hits"]],
            "truncated": found["truncated"]}
