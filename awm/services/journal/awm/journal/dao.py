"""Journal data access: the post log, its tags, links and versions, and the search index.

A scope IS the channel. Everything written to it (messages, journal entries,
briefs, precedents) is a row in ``scope_posts``, told apart by its tags
(``post_tags``, see :mod:`awm.journal.tags`). A comment is a post with
``reply_to``; ``thread_id`` names its thread's first post.
"""

from __future__ import annotations

import sqlite3

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import get_connection, init_service_db
from awm.persistence.embeddings import EMBEDDINGS_DDL

from awm.journal import builtin_tags

SERVICE = "journal"
SCHEMA_VERSION = 5

# Where a scope has read its own channel up to. One row per channel: seats churn,
# the scope persists. See awm.journal.inbox.
_CURSORS_SQL = """\
CREATE TABLE IF NOT EXISTS scope_cursors (
    owner_project  TEXT NOT NULL,
    owner_scope    TEXT NOT NULL,
    through_ts     INTEGER NOT NULL,
    through_id     TEXT NOT NULL,
    acked_at       INTEGER NOT NULL,
    acked_by       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (owner_project, owner_scope)
);
"""

MIGRATIONS = {(1, 2): _CURSORS_SQL}

# The vocabulary every post's tags come from (fleet.md R107 item 6). A post links
# a tag by id, so a rename shows on every post. tag_versions keeps each earlier
# name and description.
TAGS_SQL = """\
CREATE TABLE IF NOT EXISTS tags (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description  TEXT NOT NULL,
    origin       TEXT NOT NULL DEFAULT '',
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tag_versions (
    tag_id       TEXT NOT NULL,
    version      INTEGER NOT NULL,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL,
    edited_at    INTEGER NOT NULL,
    edited_by    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tag_id, version)
);
CREATE TABLE IF NOT EXISTS post_tags (
    post_id      TEXT NOT NULL,
    tag_id       TEXT NOT NULL,
    PRIMARY KEY (post_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_post_tags_tag ON post_tags(tag_id, post_id);
-- Two nodes that added one name before they synced: two concepts. The newer tag
-- carries a provisional name until an agent chooses one (awm.journal.tagsync).
CREATE TABLE IF NOT EXISTS tag_clashes (
    name           TEXT NOT NULL,
    older_id       TEXT NOT NULL,
    newer_id       TEXT NOT NULL,
    older_node     TEXT NOT NULL DEFAULT '',
    newer_node     TEXT NOT NULL DEFAULT '',
    detected_at    INTEGER NOT NULL,
    dispatched_at  INTEGER,
    dispatch       TEXT NOT NULL DEFAULT '',
    resolved_at    INTEGER,
    PRIMARY KEY (older_id, newer_id)
);
""" + builtin_tags.builtins_sql()

SCHEMA_SQL = """\
-- One append-only post log per channel. The owner is addressed by natural key
-- (owner_project, owner_scope). owner_project='' marks a non-literal channel
-- whose ref lives verbatim in owner_scope ('user:alice', 'project:awm',
-- 'workspace').
--   author : 'agent:<project>/<scope>', 'user:<name>', 'peer:<node>' or 'system'.
--   meta      : JSON; journal structure (decisions/issues/next_steps/outcome/...) lives here.
--   reply_to  : the post a comment answers; NULL for a thread's first post.
--   thread_id : the thread's first post; NULL for that post itself.
CREATE TABLE IF NOT EXISTS scope_posts (
    id             TEXT PRIMARY KEY,
    owner_project  TEXT NOT NULL,
    owner_scope    TEXT NOT NULL,
    author         TEXT NOT NULL,
    body           TEXT NOT NULL DEFAULT '',
    meta           TEXT NOT NULL DEFAULT '{}',
    ts             INTEGER NOT NULL,
    reply_to       TEXT,
    thread_id      TEXT,
    version        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_scope_posts_owner_ts ON scope_posts(owner_project, owner_scope, ts);
CREATE INDEX IF NOT EXISTS idx_scope_posts_thread ON scope_posts(thread_id, ts);
CREATE INDEX IF NOT EXISTS idx_scope_posts_reply ON scope_posts(reply_to);
CREATE INDEX IF NOT EXISTS idx_scope_posts_author ON scope_posts(author, ts);
""" + _CURSORS_SQL + TAGS_SQL

READS_SQL = """\
-- The journals each session read through fetch: its citation candidates
-- (sessions.md R15). session is the reader's R9 id.
CREATE TABLE IF NOT EXISTS session_reads (
    session     TEXT NOT NULL,
    post_id     TEXT NOT NULL,
    first_read  INTEGER NOT NULL,
    last_read   INTEGER NOT NULL,
    times       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session, post_id)
);
"""

MIGRATIONS[(2, 3)] = READS_SQL

# A debrief journal's citations, its reply_to list (sessions.md R14), and the
# parent links reflection records (R13). Both point one way: no reverse lookup.
LINKS_SQL = """\
CREATE TABLE IF NOT EXISTS post_cites (
    post_id   TEXT NOT NULL,
    cited_id  TEXT NOT NULL,
    ord       INTEGER NOT NULL,
    PRIMARY KEY (post_id, cited_id)
);
CREATE TABLE IF NOT EXISTS session_parents (
    session  TEXT PRIMARY KEY,
    parent   TEXT NOT NULL,
    how      TEXT NOT NULL,
    node     TEXT NOT NULL DEFAULT '',
    at       REAL NOT NULL
);
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

MIGRATIONS[(3, 4)] = "ALTER TABLE scope_posts ADD COLUMN version INTEGER NOT NULL DEFAULT 1;\n" + VERSIONS_SQL

# Kinds become tags (fleet.md R107 item 6); a kind with no fixed tag becomes a
# migrated tag. The goal log went (item 10): a real goal becomes a journal post in
# its channel with its body and meta, and the goal test rows go. A brief's
# meta.supersedes becomes a reply plus the superseded tag (item 8). A journal's
# meta.cites becomes its citations, and each reason stays as meta.cite_reasons
# (sessions.md R14, R15). The subscriber table, which nothing read, goes. The legacy channel spellings ``task:<p>`` and
# ``('', 'user:proj:<p>')`` become ``<p>`` and ``('', 'project:<p>')``.
_SUPERSEDES = "json_extract(meta, '$.supersedes')"
_GOAL_TEST = ("kind = 'goal' AND (owner_project = '_goaltest' OR owner_scope = 'project:_goaltest'"
              " OR body LIKE 'TEST-%' OR lower(body) LIKE 'test cleanup%')")
_CITES = "json_valid(meta) AND json_type(meta, '$.cites') = 'array'"
MIGRATIONS[(4, 5)] = TAGS_SQL + LINKS_SQL + f"""\
ALTER TABLE scope_posts ADD COLUMN reply_to TEXT;
ALTER TABLE scope_posts ADD COLUMN thread_id TEXT;
DELETE FROM scope_posts WHERE {_GOAL_TEST};
INSERT OR IGNORE INTO tags (id, name, description, origin, version, created_at, updated_at)
    SELECT DISTINCT '{builtin_tags.MIGRATED_PREFIX}' || lower(trim(kind)), lower(trim(kind)),
           '{builtin_tags.MIGRATED_DESCRIPTION}', '', 1, 0, 0
    FROM scope_posts WHERE trim(kind) != '' AND lower(trim(kind)) NOT IN ({builtin_tags.known_kinds_sql()});
INSERT OR IGNORE INTO post_tags (post_id, tag_id)
    SELECT id, {builtin_tags.kind_case_sql('lower(trim(kind))')} FROM scope_posts
    WHERE lower(trim(kind)) IN ({builtin_tags.known_kinds_sql()});
INSERT OR IGNORE INTO post_tags (post_id, tag_id)
    SELECT p.id, t.id FROM scope_posts p JOIN tags t ON t.name = lower(trim(p.kind))
    WHERE lower(trim(p.kind)) NOT IN ({builtin_tags.known_kinds_sql()});
INSERT OR IGNORE INTO post_tags (post_id, tag_id)
    SELECT id, '{builtin_tags.MESSAGE}' FROM scope_posts WHERE trim(kind) = '';
UPDATE scope_posts SET reply_to = {_SUPERSEDES}
    WHERE kind = 'brief' AND {_SUPERSEDES} IN (SELECT id FROM scope_posts WHERE kind = 'brief');
INSERT OR IGNORE INTO post_tags (post_id, tag_id)
    SELECT reply_to, '{builtin_tags.SUPERSEDED}' FROM scope_posts WHERE reply_to IS NOT NULL;
UPDATE scope_posts SET meta = json_remove(meta, '$.supersedes')
    WHERE reply_to IS NOT NULL AND json_valid(meta);
UPDATE scope_posts SET thread_id = (
    WITH RECURSIVE up(id, parent, depth) AS (
        SELECT s.id, s.reply_to, 0 FROM scope_posts s WHERE s.id = scope_posts.reply_to
        UNION ALL
        SELECT s.id, s.reply_to, up.depth + 1 FROM scope_posts s JOIN up ON s.id = up.parent
        WHERE up.depth < 10000)
    SELECT id FROM up WHERE parent IS NULL)
    WHERE reply_to IS NOT NULL;
INSERT OR IGNORE INTO post_cites (post_id, cited_id, ord)
    SELECT p.id, json_extract(c.value, '$.journal'), c.key
    FROM scope_posts p, json_each(p.meta, '$.cites') c
    WHERE p.kind = 'journal' AND json_valid(p.meta) AND json_type(p.meta, '$.cites') = 'array'
      AND json_extract(c.value, '$.journal') IN (SELECT id FROM scope_posts);
UPDATE scope_posts SET meta = json_set(meta, '$.cite_reasons', json((
    SELECT json_group_object(json_extract(c.value, '$.journal'), json_extract(c.value, '$.reason'))
    FROM json_each(scope_posts.meta, '$.cites') c
    WHERE json_extract(c.value, '$.reason') IS NOT NULL)))
    WHERE {_CITES} AND EXISTS (SELECT 1 FROM json_each(scope_posts.meta, '$.cites') c
                              WHERE json_extract(c.value, '$.reason') IS NOT NULL);
UPDATE scope_posts SET meta = json_remove(meta, '$.cites') WHERE {_CITES};
DROP INDEX IF EXISTS idx_scope_posts_kind;
ALTER TABLE scope_posts DROP COLUMN kind;
CREATE INDEX IF NOT EXISTS idx_scope_posts_thread ON scope_posts(thread_id, ts);
CREATE INDEX IF NOT EXISTS idx_scope_posts_reply ON scope_posts(reply_to);
UPDATE OR IGNORE scope_posts SET owner_project = substr(owner_project, 6) WHERE owner_project LIKE 'task:%';
UPDATE OR IGNORE scope_posts SET owner_scope = 'project:' || substr(owner_scope, 11)
    WHERE owner_project = '' AND owner_scope LIKE 'user:proj:%';
UPDATE OR IGNORE scope_cursors SET owner_project = substr(owner_project, 6) WHERE owner_project LIKE 'task:%';
UPDATE OR IGNORE scope_cursors SET owner_scope = 'project:' || substr(owner_scope, 11)
    WHERE owner_project = '' AND owner_scope LIKE 'user:proj:%';
DELETE FROM scope_cursors WHERE owner_project LIKE 'task:%' OR owner_scope LIKE 'user:proj:%';
DROP TABLE IF EXISTS scope_subscribers;
"""


_initialized = False


def init() -> None:
    """Idempotently create the journal DB, its tables and the embeddings store."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL + READS_SQL + LINKS_SQL + VERSIONS_SQL + EMBEDDINGS_DDL,
                        schema_version=SCHEMA_VERSION, migrations=MIGRATIONS)
        conn = get_connection(SERVICE)
        try:
            conn.executescript(builtin_tags.builtins_sql())
        finally:
            conn.close()
        _initialized = True


class JournalDAO(BaseDAO):
    """CRUD over the journal service's own SQLite DB."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)
