"""Meaning matches for the archive's own documents, from kb's entry index.

kb encodes the passages of journal posts, decisions and notes into its entry index, one zone per kind, and
`kb related` returns the nearest ones with the source, document and span each came from. The archive keeps the
documents themselves, so it resolves those names against its own table (see ``index.resolve``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from awm.search.gateway import Gateway
from awm.search.index import Filters

#: Archive source -> the zone kb's entry index files its passages under.
SOURCE_ZONES = {"journals": "journal", "precedence": "precedent", "notes": "note", "writing": "note"}
TIMEOUT_S = 8.0
#: Entries asked per zone; a filtered query asks for more, because the filters apply after kb answers.
ENTRIES = 60
ENTRIES_FILTERED = 200


def covered(sources: list[str]) -> list[str]:
    """The sources whose passages kb's entry index holds."""
    return [s for s in sources if s in SOURCE_ZONES]


async def ask(gateway: Gateway, text: str, sources: list[str], filters: Filters, limit: int) -> list[dict[str, Any]]:
    """kb's entries for ``text`` among ``sources``, best first. Raises when kb cannot answer."""
    narrowed = bool(filters.project or filters.scope or filters.kinds or filters.after or filters.before)
    k = ENTRIES_FILTERED if narrowed else max(ENTRIES, limit * 6)
    args = {"text": text, "k": min(k, ENTRIES_FILTERED), "sources": sources,
            "zones": sorted({SOURCE_ZONES[s] for s in sources})}
    raw = await asyncio.wait_for(gateway.call("kb", "related", args, TIMEOUT_S), timeout=TIMEOUT_S + 1.0)
    hits = raw["hits"]
    if not isinstance(hits, list):
        raise ValueError("kb related answered without hits")
    return [h for h in hits if isinstance(h, dict) and {"score", "source", "doc", "part", "start", "end"} <= h.keys()]
