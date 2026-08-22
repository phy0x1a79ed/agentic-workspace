"""Scopes service data access — the tables owned by the scopes service plus
the ``embeddings`` table for semantic search.

Per the modular invariant there is no shared ``state.db``: this service owns
its tables and stands them up via ``init_service_db`` at startup. The
identity layer (projects/users/agents) is relational within this DB; all
other cross-service refs use natural keys.

**A scope IS the channel.** There is no separate rooms/messages/session_logs
machinery: a scope owns one channel, and everything written to it — messages,
journal (debrief) entries, system notices — is a row in ``scope_posts``
differentiated by ``kind``. Subscribers (besides the owner scope) live in
``scope_subscribers``. Raw agent acts are NOT here — they belong to the agent
and live in the agents service's own DB; you subscribe to an agent for those.
"""

from __future__ import annotations

import sqlite3

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db
from awm.persistence.embeddings import EMBEDDINGS_DDL

SERVICE = "scopes"
SCHEMA_VERSION = 3

# Tables owned by the scopes service. v2 collapsed the old rooms/messages/
# session_logs trio into one ``scope_posts`` channel + ``scope_subscribers``;
# v3 drops the vestigial ``agents.parent_id`` (rooms-era agent containment —
# agents form no tree, a scope IS the channel).
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

-- Scope channel (a scope IS the channel) ------------------------------
-- One append-only post log per scope. Messages, journal (debrief) entries,
-- and system notices are all rows here, differentiated by ``kind``. The
-- channel owner is a scope, addressed by natural key (owner_project,
-- owner_scope). For legacy non-agent targets (a user/project/workspace
-- inbox) the owner need not be a literal worktree scope: owner_project=''
-- marks a non-literal channel whose ref lives verbatim in owner_scope
-- (e.g. 'user:alice', 'project:awm', 'workspace').
--   author : natural-key ref ('agent:<project>/<scope>', 'user:<name>') or 'system'.
--   kind   : 'message' | 'journal' | 'system' | …
--   meta   : JSON; journal structure (decisions/issues/next_steps/outcome/…) lives here.
CREATE TABLE IF NOT EXISTS scope_posts (
    id             TEXT PRIMARY KEY,
    owner_project  TEXT NOT NULL,
    owner_scope    TEXT NOT NULL,
    author         TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'message',
    body           TEXT NOT NULL DEFAULT '',
    meta           TEXT NOT NULL DEFAULT '{}',
    ts             INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scope_posts_owner_ts ON scope_posts(owner_project, owner_scope, ts);
CREATE INDEX IF NOT EXISTS idx_scope_posts_kind ON scope_posts(owner_project, owner_scope, kind, ts);
CREATE INDEX IF NOT EXISTS idx_scope_posts_author ON scope_posts(author, ts);

-- Channel subscribers (besides the owner scope) -----------------------
CREATE TABLE IF NOT EXISTS scope_subscribers (
    owner_project  TEXT NOT NULL,
    owner_scope    TEXT NOT NULL,
    guest_kind     TEXT NOT NULL,       -- 'agent' | 'user'
    guest_ref      TEXT NOT NULL,       -- natural key: 'project/scope' or 'user:<name>'
    display_name   TEXT NOT NULL,
    joined_at      INTEGER NOT NULL,
    PRIMARY KEY (owner_project, owner_scope, guest_kind, guest_ref)
);
CREATE INDEX IF NOT EXISTS idx_scope_subscribers_ref ON scope_subscribers(guest_kind, guest_ref);
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
}

_initialized = False


def init() -> None:
    """Idempotently create the scopes service DB + all tables + embeddings."""
    global _initialized
    if not _initialized:
        init_service_db(
            SERVICE,
            SCHEMA_SQL + EMBEDDINGS_DDL,
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
