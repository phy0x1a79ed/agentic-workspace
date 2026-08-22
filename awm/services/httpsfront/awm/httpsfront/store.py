"""httpsfront's own tag/filter store — the ``page_tags`` and ``selected_tags``
tables on the service's OWN SQLite DB (``AWM_DIR/services/httpsfront/httpsfront.db``).

Backs the landing page's tagging + filtering feature: which tags a page
carries, and which tags are currently selected as a filter. Both persist
across reloads and service restarts, per the modular invariant that each
service owns its own DB rather than sharing state.
"""

from __future__ import annotations

import sqlite3

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "httpsfront"
SCHEMA_VERSION = 1

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS page_tags (
    page TEXT NOT NULL,
    tag  TEXT NOT NULL,
    PRIMARY KEY (page, tag)
);
CREATE TABLE IF NOT EXISTS selected_tags (
    tag TEXT PRIMARY KEY
);
"""

_initialized = False


def init() -> None:
    """Idempotently create httpsfront's DB + tag tables."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


def _normalize(tag: str) -> str:
    return " ".join(str(tag or "").split())


class LandingDAO(BaseDAO):
    """CRUD over ``page_tags`` and ``selected_tags``."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    def tags_for_page(self, page: str) -> list[str]:
        rows = self.query_all(
            "SELECT tag FROM page_tags WHERE page = ? ORDER BY tag",
            (page,),
        )
        return [r["tag"] for r in rows]

    def tags_by_page(self, pages: list[str]) -> dict[str, list[str]]:
        """Bulk fetch: one query for every page's tags, for rendering the index."""
        result: dict[str, list[str]] = {p: [] for p in pages}
        if not pages:
            return result
        placeholders = ",".join("?" for _ in pages)
        rows = self.query_all(
            f"SELECT page, tag FROM page_tags WHERE page IN ({placeholders}) "
            "ORDER BY page, tag",
            tuple(pages),
        )
        for r in rows:
            result.setdefault(r["page"], []).append(r["tag"])
        return result

    def all_tag_counts(self) -> dict[str, int]:
        rows = self.query_all(
            "SELECT tag, COUNT(*) AS n FROM page_tags GROUP BY tag ORDER BY tag"
        )
        return {r["tag"]: r["n"] for r in rows}

    def add_tag(self, page: str, tag: str) -> None:
        tag = _normalize(tag)
        if not page or not tag:
            return
        self.execute(
            "INSERT OR IGNORE INTO page_tags (page, tag) VALUES (?, ?)",
            (page, tag),
        )

    def remove_tag(self, page: str, tag: str) -> None:
        tag = _normalize(tag)
        self.execute(
            "DELETE FROM page_tags WHERE page = ? AND tag = ?",
            (page, tag),
        )

    def selected_tags(self) -> list[str]:
        rows = self.query_all("SELECT tag FROM selected_tags ORDER BY tag")
        return [r["tag"] for r in rows]

    def select_tag(self, tag: str) -> None:
        tag = _normalize(tag)
        if not tag:
            return
        self.execute(
            "INSERT OR IGNORE INTO selected_tags (tag) VALUES (?)", (tag,)
        )

    def deselect_tag(self, tag: str) -> None:
        tag = _normalize(tag)
        self.execute("DELETE FROM selected_tags WHERE tag = ?", (tag,))
