"""Scopes service data access — all 9 tables owned by the scopes service plus
the ``embeddings`` table for semantic search.

Per the modular invariant there is no shared ``state.db``: this service owns
its tables and stands them up via ``init_service_db`` at startup. The
identity layer (projects/users/agents) is relational within this DB; all
other cross-service refs use natural keys.
"""

from __future__ import annotations

import sqlite3

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db
from awm.persistence.embeddings import EMBEDDINGS_DDL

SERVICE = "scopes"
SCHEMA_VERSION = 1

# All 9 tables owned by the scopes service, taken verbatim from
# SCHEMA_HANDOFF.md § scopes v1 schema, plus the per-service embeddings table.
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
    parent_id     TEXT REFERENCES agents(id),
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
CREATE INDEX IF NOT EXISTS idx_agents_parent ON agents(parent_id);

-- Sessions ------------------------------------------------------------
-- agent_id FK dropped; (project, scope) carried inline.
CREATE TABLE IF NOT EXISTS session_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project       TEXT NOT NULL,
    scope         TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    file_path     TEXT NOT NULL DEFAULT '',
    git_commit    TEXT,
    summary       TEXT NOT NULL DEFAULT '',
    metadata      TEXT,
    content       TEXT,
    skill_path    TEXT,
    outcome       TEXT,
    deviations    TEXT,
    suggestions   TEXT,
    skill_version TEXT,
    resolved_at   INTEGER,
    resolution    TEXT,
    title         TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_logs_scope ON session_logs(project, scope, created_at DESC);

-- Messaging -----------------------------------------------------------
-- recipient_id/sender_id were polymorphic refs (agent uuid | user uuid |
-- 'system'). Re-key to natural-key ref strings: store the literal the app
-- layer resolves, e.g. 'agent:<project>/<scope>', 'user:<name>', 'system'.
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_ref TEXT NOT NULL,
    sender_ref    TEXT NOT NULL,
    msg_type      TEXT NOT NULL DEFAULT '',
    subject       TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    metadata      TEXT,
    status        TEXT NOT NULL DEFAULT 'unread',
    created_at    INTEGER NOT NULL,
    read_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient_status
    ON messages(recipient_ref, status, created_at DESC);

-- Rooms (within-service FKs kept) -------------------------------------
-- owner_agent_id was an agents(id) FK; agents is in this same DB, but the
-- room owner is best addressed by natural key going forward → owner_project,
-- owner_scope. (A scope IS the channel; one owner agent.)
CREATE TABLE IF NOT EXISTS rooms (
    id              TEXT PRIMARY KEY,
    owner_project   TEXT NOT NULL,
    owner_scope     TEXT NOT NULL,
    topic           TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    closed_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rooms_owner ON rooms(owner_project, owner_scope);
CREATE INDEX IF NOT EXISTS idx_rooms_status ON rooms(status);

CREATE TABLE IF NOT EXISTS guest_list (
    room_id        TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    guest_kind     TEXT NOT NULL,       -- 'agent' | 'user'
    guest_ref      TEXT NOT NULL,       -- natural key: 'project/scope' or 'user:<name>'
    display_name   TEXT NOT NULL,
    subscriptions  TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (room_id, guest_kind, guest_ref)
);

CREATE TABLE IF NOT EXISTS room_transcripts (
    id       TEXT PRIMARY KEY,
    room_id  TEXT NOT NULL REFERENCES rooms(id),
    author   TEXT NOT NULL,             -- natural-key ref string or 'system'
    kind     TEXT NOT NULL,
    body     TEXT NOT NULL DEFAULT '',
    meta     TEXT NOT NULL DEFAULT '{}',
    ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_room_transcripts_room_ts ON room_transcripts(room_id, ts);
CREATE INDEX IF NOT EXISTS idx_room_transcripts_kind ON room_transcripts(room_id, kind, ts);
"""

_initialized = False


def init() -> None:
    """Idempotently create the scopes service DB + all 9 tables + embeddings."""
    global _initialized
    if not _initialized:
        init_service_db(
            SERVICE,
            SCHEMA_SQL + EMBEDDINGS_DDL,
            schema_version=SCHEMA_VERSION,
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
