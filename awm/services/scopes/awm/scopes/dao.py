"""Scopes service data access — the tables owned by the scopes service plus
the ``embeddings`` table for semantic search.

Per the modular invariant there is no shared ``state.db``: this service owns
its tables and stands them up via ``init_service_db`` at startup. The
identity layer (projects/users/agents) is relational within this DB; all
other cross-service refs use natural keys.

A scope's posts, brief and subscribers live in the journal service.
"""

from __future__ import annotations

import sqlite3

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db
from awm.persistence.embeddings import EMBEDDINGS_DDL

SERVICE = "scopes"
SCHEMA_VERSION = 5

# Tables owned by the scopes service. v2 collapsed the old rooms/messages/
# session_logs trio into one ``scope_posts`` channel + ``scope_subscribers``;
# v3 drops the vestigial ``agents.parent_id`` (rooms-era agent containment —
# agents form no tree, a scope IS the channel); v4 adds ``tags``; v5 adds
# the scope claims, their queues, last use and counters (`awm.scopes.claims`).
SCHEMA_SQL = """\
-- Identity (kept relational — all in the scopes DB) -------------------
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT,
    repo_path   TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id        TEXT PRIMARY KEY,
    username  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS agents (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects(id),
    scope         TEXT NOT NULL,
    status        TEXT NOT NULL,
    agent_cli     TEXT NOT NULL,
    branch        TEXT NOT NULL,
    worktree      TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    is_vagrant    INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    retired_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agents_project_status ON agents(project_id, status);

-- Tags per project (scope '') or per scope or scope group path ----------
-- Keyed by name, not agents row: a project, a main checkout or a group such
-- as 'metasmith/lab' can carry tags with no agents row behind it.
--   tags : JSON list of normalised tags, never empty (an empty set deletes the row).
CREATE TABLE IF NOT EXISTS tags (
    project     TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (project, scope)
);
"""

# Upgrades for an existing DB. v3 drops the rooms-era ``agents.parent_id``:
# its dependent index goes first (SQLite refuses DROP COLUMN while an index
# references it), then the column. Runs with FKs off (the migration loop sets
# that), so the self-FK in the old column definition is dropped cleanly.
MIGRATIONS: dict[tuple[int, int], str] = {
    (2, 3): (
        "DROP INDEX IF EXISTS idx_agents_parent;\n"
        "ALTER TABLE agents DROP COLUMN parent_id;\n"
    ),
    (3, 4): (
        "CREATE TABLE IF NOT EXISTS tags (\n"
        "    project TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',\n"
        "    tags TEXT NOT NULL, updated_at INTEGER NOT NULL,\n"
        "    PRIMARY KEY (project, scope));\n"
    ),
}

# Scope claims (scopes.md R3, R42, R46) and the hygiene check's inputs (R45).
CLAIMS_SQL = """\
CREATE TABLE IF NOT EXISTS scope_claims (
    project     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    session     TEXT NOT NULL,
    pid         INTEGER,
    proc_start  TEXT,
    job         TEXT,
    claimed_at  INTEGER NOT NULL,
    PRIMARY KEY (project, scope)
);
CREATE TABLE IF NOT EXISTS scope_waiters (
    project     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    session     TEXT NOT NULL,
    pid         INTEGER,
    proc_start  TEXT,
    job         TEXT,
    asked_at    INTEGER NOT NULL,
    notified_at INTEGER,
    PRIMARY KEY (project, scope, session)
);
CREATE TABLE IF NOT EXISTS scope_use (
    project     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    last_used   INTEGER NOT NULL,
    PRIMARY KEY (project, scope)
);
CREATE TABLE IF NOT EXISTS scope_counters (
    name   TEXT PRIMARY KEY,
    value  INTEGER NOT NULL
);
"""

MIGRATIONS[(4, 5)] = CLAIMS_SQL

_initialized = False


def init() -> None:
    """Idempotently create the scopes service DB + all tables + embeddings."""
    global _initialized
    if not _initialized:
        init_service_db(
            SERVICE,
            SCHEMA_SQL + CLAIMS_SQL + EMBEDDINGS_DDL,
            schema_version=SCHEMA_VERSION,
            migrations=MIGRATIONS,
        )
        _initialized = True


class ScopesDAO(BaseDAO):
    """CRUD over the scopes service's own SQLite DB.

    Surfaces the full ``query_one`` / ``query_all`` / ``execute`` /
    ``executemany`` / ``transaction`` interface from :class:`BaseDAO`. Feature
    methods that work across multiple tables open a
    ``with self.transaction() as conn:`` block and pass ``conn`` to each helper
    call so the whole unit of work is atomic.
    """

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)
