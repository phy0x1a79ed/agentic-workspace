"""Artifacts service data access — ``artifacts`` + per-service ``embeddings`` tables
on the artifacts service's OWN SQLite DB (``AWM_DIR/services/artifacts/artifacts.db``).

Per the modular invariant there is no shared ``state.db``: this service owns its
own tables and stands them up via ``init_service_db`` at startup. The
``agent_id`` FK is gone; ``project`` / ``scope`` natural keys are carried inline.
The cross-service identity link is validated at the app layer by calling
``scopes`` over gateway RPC (cached via ``RefCache``).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db
from awm.persistence.embeddings import EMBEDDINGS_DDL

SERVICE = "artifacts"
SCHEMA_VERSION = 1

# VERBATIM from SCHEMA_HANDOFF.md § artifacts —
# agent_id FK dropped; (project, scope) carried inline; embeddings appended.
SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS artifacts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project        TEXT NOT NULL,
    scope          TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    artifact_type  TEXT NOT NULL DEFAULT '',
    path           TEXT NOT NULL DEFAULT '',
    description    TEXT,
    format         TEXT,
    tags           TEXT,
    status         TEXT NOT NULL DEFAULT 'current',
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_scope ON artifacts(project, scope, created_at DESC);
""" + EMBEDDINGS_DDL

_initialized = False


def init() -> None:
    """Idempotently create the artifacts service's DB + tables."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


class ArtifactsDAO(BaseDAO):
    """CRUD + search over the ``artifacts`` table (re-keyed to natural keys)."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    # -----------------------------------------------------------------------
    # Base SELECT — no JOINs needed: project/scope are native columns.
    # -----------------------------------------------------------------------

    _BASE_SELECT = (
        "SELECT id, project, scope, name, artifact_type, path, "
        "description, format, tags, status, created_at, updated_at "
        "FROM artifacts"
    )

    # -----------------------------------------------------------------------
    # Register (upsert on path+project)
    # -----------------------------------------------------------------------

    def get_by_path_and_project(
        self, path: str, project: str, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        """Return an existing artifact row by (path, project), or None."""
        return self.query_one(
            "SELECT id FROM artifacts WHERE path = ? AND project = ? LIMIT 1",
            (path, project),
            conn=conn,
        )

    def update_artifact(
        self,
        artifact_id: int,
        *,
        project: str,
        scope: str,
        name: str,
        artifact_type: str,
        description: str | None,
        format: str | None,
        tags: str | None,
        updated_at: int,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self.execute(
            "UPDATE artifacts SET "
            " project=?, scope=?, name=?, artifact_type=?, description=?, "
            " format=?, tags=?, status='current', updated_at=? "
            "WHERE id=?",
            (project, scope, name, artifact_type, description,
             format, tags, updated_at, artifact_id),
            conn=conn,
        )

    def insert_artifact(
        self,
        *,
        project: str,
        scope: str,
        name: str,
        artifact_type: str,
        path: str,
        description: str | None,
        format: str | None,
        tags: str | None,
        created_at: int,
        updated_at: int,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Insert a new artifact row and return its ``id``."""
        return self.execute(
            "INSERT INTO artifacts "
            "(project, scope, name, artifact_type, path, description, "
            " format, tags, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'current', ?, ?)",
            (project, scope, name, artifact_type, path,
             description, format, tags, created_at, updated_at),
            conn=conn,
        )

    # -----------------------------------------------------------------------
    # Fetch single
    # -----------------------------------------------------------------------

    def get_by_id(
        self, artifact_id: int, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        return self.query_one(
            f"{self._BASE_SELECT} WHERE id = ?",
            (artifact_id,),
            conn=conn,
        )

    def get_path_by_id(
        self, artifact_id: int, *, conn: sqlite3.Connection | None = None
    ) -> str | None:
        row = self.query_one(
            "SELECT path FROM artifacts WHERE id = ?",
            (artifact_id,),
            conn=conn,
        )
        return row["path"] if row else None

    # -----------------------------------------------------------------------
    # Delete
    # -----------------------------------------------------------------------

    def delete_artifact(
        self, artifact_id: int, *, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        row = self.query_one(
            "SELECT id, name, path FROM artifacts WHERE id = ?",
            (artifact_id,),
            conn=conn,
        )
        if not row:
            return None
        self.execute(
            "DELETE FROM artifacts WHERE id = ?", (artifact_id,), conn=conn
        )
        return row

    # -----------------------------------------------------------------------
    # Search / filter
    # -----------------------------------------------------------------------

    def search(
        self,
        *,
        project: str | None = None,
        scope: str | None = None,
        artifact_type: str | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Keyword filter over artifacts (status='current').

        project / scope / artifact_type / query are all optional.
        Returns plain dicts with the _BASE_SELECT columns.
        """
        sql = f"{self._BASE_SELECT} WHERE status='current'"
        params: list = []
        if project:
            sql += " AND project = ?"
            params.append(project)
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        if artifact_type:
            sql += " AND artifact_type = ?"
            params.append(artifact_type)
        if query:
            sql += " AND (name LIKE ? OR description LIKE ? OR tags LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like, like])
        sql += " ORDER BY project, artifact_type, name LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self.query_all(sql, params, conn=conn)

    # -----------------------------------------------------------------------
    # Sync helpers
    # -----------------------------------------------------------------------

    def get_all_for_sync(
        self, *, conn: sqlite3.Connection | None = None
    ) -> list[dict[str, Any]]:
        return self.query_all(
            "SELECT id, path, status FROM artifacts", conn=conn
        )

    def mark_stale(
        self, artifact_ids: list[int], updated_at: int,
        *, conn: sqlite3.Connection | None = None
    ) -> None:
        self.executemany(
            "UPDATE artifacts SET status='stale', updated_at=? WHERE id=?",
            [(updated_at, aid) for aid in artifact_ids],
            conn=conn,
        )

    def restore_current(
        self, artifact_ids: list[int], updated_at: int,
        *, conn: sqlite3.Connection | None = None
    ) -> None:
        self.executemany(
            "UPDATE artifacts SET status='current', updated_at=? WHERE id=?",
            [(updated_at, aid) for aid in artifact_ids],
            conn=conn,
        )

    def get_current_ids(
        self, *, conn: sqlite3.Connection | None = None
    ) -> set[str]:
        rows = self.query_all(
            "SELECT id FROM artifacts WHERE status='current'", conn=conn
        )
        return {str(r["id"]) for r in rows}

    def get_fingerprint(
        self, *, conn: sqlite3.Connection | None = None
    ) -> str:
        row = self.query_one(
            "SELECT COALESCE(MAX(updated_at), 0) AS mx, COUNT(*) AS cnt FROM artifacts",
            conn=conn,
        )
        return f"{row['mx']}|{row['cnt']}" if row else "0|0"

    # -----------------------------------------------------------------------
    # Embeddings helpers (operate on this service's own embeddings table)
    # -----------------------------------------------------------------------

    def get_embedding_source_ids(
        self, *, conn: sqlite3.Connection | None = None
    ) -> list[str]:
        rows = self.query_all(
            "SELECT source_id FROM embeddings WHERE source_type='artifact'",
            conn=conn,
        )
        return [r["source_id"] for r in rows]

    def delete_embedding_by_source_id(
        self, source_id: str, *, conn: sqlite3.Connection | None = None
    ) -> None:
        self.execute(
            "DELETE FROM embeddings WHERE source_type='artifact' AND source_id=?",
            (source_id,),
            conn=conn,
        )

    def delete_stale_embeddings(
        self, stale_source_ids: list[str], *, conn: sqlite3.Connection | None = None
    ) -> None:
        self.executemany(
            "DELETE FROM embeddings WHERE source_type='artifact' AND source_id=?",
            [(sid,) for sid in stale_source_ids],
            conn=conn,
        )

    # -----------------------------------------------------------------------
    # Seed helper: bulk insert for legacy migration
    # -----------------------------------------------------------------------

    def seed_insert(
        self,
        *,
        project: str,
        scope: str,
        name: str,
        artifact_type: str,
        path: str,
        description: str | None,
        format: str | None,
        tags: str | None,
        status: str,
        created_at: int,
        updated_at: int,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        """Idempotent upsert on (path, project, scope) for legacy seeding."""
        self.execute(
            "INSERT INTO artifacts "
            "(project, scope, name, artifact_type, path, description, "
            " format, tags, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (project, scope, name, artifact_type, path,
             description, format, tags, status, created_at, updated_at),
            conn=conn,
        )
