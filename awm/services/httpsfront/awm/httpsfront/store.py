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
SCHEMA_VERSION = 2

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS page_tags (
    page TEXT NOT NULL,
    tag  TEXT NOT NULL,
    PRIMARY KEY (page, tag)
);
CREATE TABLE IF NOT EXISTS selected_tags (
    tag TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS page_names (
    page         TEXT PRIMARY KEY,
    display_name TEXT NOT NULL
);
"""

#: Upgrade path for a DB that already exists at an older ``SCHEMA_VERSION``.
#: ``init_service_db`` only runs ``SCHEMA_SQL`` in full on a brand-new DB; an
#: existing one is advanced step by step through this map instead.
MIGRATIONS: dict[tuple[int, int], str] = {
    (1, 2): """\
CREATE TABLE IF NOT EXISTS page_names (
    page         TEXT PRIMARY KEY,
    display_name TEXT NOT NULL
);
""",
}

_initialized = False


def init() -> None:
    """Idempotently create httpsfront's DB + tag tables."""
    global _initialized
    if not _initialized:
        init_service_db(
            SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION,
            migrations=MIGRATIONS,
        )
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

    def display_name(self, page: str) -> str | None:
        row = self.query_one(
            "SELECT display_name FROM page_names WHERE page = ?", (page,)
        )
        return row["display_name"] if row else None

    def display_names(self, pages: list[str]) -> dict[str, str]:
        """Bulk fetch: one query for every page's display-name override, for rendering the index."""
        if not pages:
            return {}
        placeholders = ",".join("?" for _ in pages)
        rows = self.query_all(
            f"SELECT page, display_name FROM page_names WHERE page IN ({placeholders})",
            tuple(pages),
        )
        return {r["page"]: r["display_name"] for r in rows}

    def set_display_name(self, page: str, name: str) -> None:
        name = _normalize(name)
        if not page or not name:
            return
        self.execute(
            "INSERT INTO page_names (page, display_name) VALUES (?, ?) "
            "ON CONFLICT(page) DO UPDATE SET display_name = excluded.display_name",
            (page, name),
        )

    def clear_display_name(self, page: str) -> None:
        self.execute("DELETE FROM page_names WHERE page = ?", (page,))
