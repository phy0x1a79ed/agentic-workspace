"""Journal data access: the post log, the subscriber table and the search index.

A scope IS the channel. Everything written to it (messages, journal entries,
system notices, goals) is a row in ``scope_posts`` differentiated by ``kind``.
The table shapes match the scopes service's, so the first-start migration is a
row copy.
"""

from __future__ import annotations

import sqlite3

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db
from awm.persistence.embeddings import EMBEDDINGS_DDL

SERVICE = "journal"
SCHEMA_VERSION = 2

SCHEMA_SQL = """\
-- One append-only post log per channel. The owner is addressed by natural key
-- (owner_project, owner_scope). owner_project='' marks a non-literal channel
-- whose ref lives verbatim in owner_scope ('user:alice', 'project:awm',
-- 'workspace').
--   author : 'agent:<project>/<scope>', 'user:<name>', 'peer:<node>' or 'system'.
--   kind   : 'message' | 'journal' | 'system' | 'goal' | ...
--   meta   : JSON; journal structure (decisions/issues/next_steps/outcome/...) lives here.
CREATE TABLE IF NOT EXISTS scope_posts (
    id             TEXT PRIMARY KEY,
    owner_project  TEXT NOT NULL,
    owner_scope    TEXT NOT NULL,
    author         TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'message',
    body           TEXT NOT NULL DEFAULT '',
    meta           TEXT NOT NULL DEFAULT '{}',
    ts             INTEGER NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_scope_posts_owner_ts ON scope_posts(owner_project, owner_scope, ts);
CREATE INDEX IF NOT EXISTS idx_scope_posts_kind ON scope_posts(owner_project, owner_scope, kind, ts);
CREATE INDEX IF NOT EXISTS idx_scope_posts_author ON scope_posts(author, ts);

CREATE TABLE IF NOT EXISTS scope_subscribers (
    owner_project  TEXT NOT NULL,
    owner_scope    TEXT NOT NULL,
    guest_kind     TEXT NOT NULL,       -- 'agent' | 'user'
    guest_ref      TEXT NOT NULL,       -- 'project/scope' or 'user:<name>'
    display_name   TEXT NOT NULL,
    joined_at      INTEGER NOT NULL,
    PRIMARY KEY (owner_project, owner_scope, guest_kind, guest_ref)
);
CREATE INDEX IF NOT EXISTS idx_scope_subscribers_ref ON scope_subscribers(guest_kind, guest_ref);
"""

# Every version of an edited post (fleet.md R107 item 1). scope_posts.body holds
# the latest; a post with no rows here was never edited and is version 1.
VERSIONS_SQL = """\
CREATE TABLE IF NOT EXISTS post_versions (
    post_id   TEXT NOT NULL,
    version   INTEGER NOT NULL,
    body      TEXT NOT NULL,
    diff      TEXT NOT NULL DEFAULT '',
    editor    TEXT NOT NULL,
    reason    TEXT NOT NULL DEFAULT '',
    ts        INTEGER NOT NULL,
    PRIMARY KEY (post_id, version)
);
"""

MIGRATIONS = {
    (1, 2): "ALTER TABLE scope_posts ADD COLUMN version INTEGER NOT NULL DEFAULT 1;\n" + VERSIONS_SQL,
}

_initialized = False


def init() -> None:
    """Idempotently create the journal DB, its tables and the embeddings store."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL + VERSIONS_SQL + EMBEDDINGS_DDL,
                        schema_version=SCHEMA_VERSION, migrations=MIGRATIONS)
        _initialized = True


class JournalDAO(BaseDAO):
    """CRUD over the journal service's own SQLite DB."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)
