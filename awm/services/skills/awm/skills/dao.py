"""Skills service data access — the ``embeddings`` table on the skills service's
own SQLite DB (``AWM_DIR/services/skills/skills.db``).

The skills catalog is file-based; skills owns NO relational state in the legacy
DB beyond its embeddings rows. v1 schema is just the ``embeddings`` table
(``source_type='skill'`` rows). The embeddings engine lives in
``awm.persistence.embeddings`` and is connection-level: callers pass the
service's own connection.
"""

from __future__ import annotations

import sqlite3

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import get_connection, init_service_db
from awm.persistence.embeddings import EMBEDDINGS_DDL

SERVICE = "skills"
SCHEMA_VERSION = 1

# Skills owns ONLY the embeddings table.  The DDL is imported from the shared
# engine so seed-from-legacy is a straight copy (identical shape).
SCHEMA_SQL = EMBEDDINGS_DDL

_initialized = False


def init() -> None:
    """Idempotently create the skills service's DB + ``embeddings`` table."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


class SkillsDAO(BaseDAO):
    """Data access over the ``embeddings`` table in the skills service DB.

    Skills' embeddings ops all go through the shared engine (``upsert_embedding``,
    ``semantic_search``, ``hybrid_augment``) — this DAO provides only the two
    reconciliation queries that ``sync_skills`` needs:

    * :meth:`list_skill_ids` — all ``source_id`` values for ``source_type='skill'``
    * :meth:`delete_stale` — bulk-delete stale embedding rows by source_id
    """

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    def list_skill_ids(self) -> list[str]:
        """Return all source_ids in the embeddings table with source_type='skill'."""
        rows = self.query_all(
            "SELECT source_id FROM embeddings WHERE source_type='skill'"
        )
        return [r["source_id"] for r in rows]

    def delete_stale(self, stale_ids: list[str]) -> int:
        """Delete embeddings rows whose skill files no longer exist.

        Returns the number of rows deleted.
        """
        if not stale_ids:
            return 0
        return self.executemany(
            "DELETE FROM embeddings WHERE source_type='skill' AND source_id=?",
            [(sid,) for sid in stale_ids],
        )

    def open_connection(self) -> sqlite3.Connection:
        """Open (and return) a raw connection to the skills DB.

        Used by callers that need to hand the connection to the embeddings
        engine (``upsert_embedding``, ``hybrid_augment``, etc.) which expect
        a raw ``sqlite3.Connection``. The caller owns the lifecycle
        (must ``.close()`` when done).
        """
        return get_connection(SERVICE)
