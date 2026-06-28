"""Workspace service data access — the ``units`` table on the workspace
service's OWN SQLite DB (``AWM_DIR/services/workspace/workspace.db``).

A *unit* is the DAG execution sandbox: one directory holding a placed agent's
brief (``CLAUDE.md``), its read-only input pre-readings (``inputs/<name>``),
and its deliverable staging (``deliverable/<contract>/``). The row is pure
bookkeeping — the canonical truth is the directory on disk; this table just
tracks which units exist, where, and whether one is currently in use
(``active``) or freed-but-retained (``idle``, the planner-reuse / audit state).

A unit is keyed on its ``unit_slug`` ALONE (the globally-unique ``orch-<8hex>``
agent slug). A task has no project, so neither does a unit.

Per the modular invariant there is no shared ``state.db``: this service owns
exactly one table and stands it up via ``init_service_db`` at startup.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "workspace"
SCHEMA_VERSION = 2

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS units (
    unit_slug   TEXT PRIMARY KEY,
    state       TEXT NOT NULL DEFAULT 'active',
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT ''
);
"""

# v1→v2: drop the ``project`` column + the composite (project, unit_slug) PK;
# re-key ``units`` on ``unit_slug`` alone (a task has no project). SQLite can't
# drop a PK column in place, so this is the canonical table-rebuild dance:
# create the new-shape table, copy the surviving columns, drop the old, rename.
MIGRATIONS: dict[tuple[int, int], str] = {
    (1, 2): (
        "CREATE TABLE units_new (\n"
        "    unit_slug   TEXT PRIMARY KEY,\n"
        "    state       TEXT NOT NULL DEFAULT 'active',\n"
        "    path        TEXT NOT NULL,\n"
        "    created_at  TEXT NOT NULL DEFAULT '',\n"
        "    updated_at  TEXT NOT NULL DEFAULT ''\n"
        ");\n"
        "INSERT INTO units_new (unit_slug, state, path, created_at, updated_at)\n"
        "    SELECT unit_slug, state, path, created_at, updated_at FROM units;\n"
        "DROP TABLE units;\n"
        "ALTER TABLE units_new RENAME TO units;\n"
    ),
}

_initialized = False


def init() -> None:
    """Idempotently create the workspace service's DB + ``units`` table."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION,
                        migrations=MIGRATIONS)
        _initialized = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkspaceDAO(BaseDAO):
    """CRUD over ``units`` (one row per provisioned workspace unit)."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    def upsert_unit(self, *, unit_slug: str, path: str,
                    state: str = "active") -> dict:
        """Insert (or re-activate) a unit row. Idempotent on unit_slug."""
        now = _now()
        self.execute(
            """\
            INSERT INTO units (unit_slug, state, path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(unit_slug) DO UPDATE SET
                state = excluded.state,
                path = excluded.path,
                updated_at = excluded.updated_at
            """,
            (unit_slug, state, path, now, now),
        )
        return self.get_unit(unit_slug)  # type: ignore[return-value]

    def set_state(self, unit_slug: str, state: str) -> bool:
        """Flip a unit's state (``active`` ⇄ ``idle``). True if a row matched."""
        rows = self.execute(
            "UPDATE units SET state=?, updated_at=? WHERE unit_slug=?",
            (state, _now(), unit_slug),
        )
        return rows > 0

    def get_unit(self, unit_slug: str) -> dict | None:
        return self.query_one(
            "SELECT * FROM units WHERE unit_slug=?",
            (unit_slug,),
        )

    def delete_unit(self, unit_slug: str) -> bool:
        rows = self.execute(
            "DELETE FROM units WHERE unit_slug=?",
            (unit_slug,),
        )
        return rows > 0

    def list_units(self) -> list[dict]:
        return self.query_all("SELECT * FROM units ORDER BY created_at")
