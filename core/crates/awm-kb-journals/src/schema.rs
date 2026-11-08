//! journal.db at schema v6, as `awm/journal/dao.py` creates and migrates it.
//!
//! The DDL is the Python's text, so a database this crate creates is the one the Python would
//! create, and a rollback reads it unchanged. After the cutover the post tables (`scope_posts`,
//! `post_tags`, `post_cites`) and the `search_feed*` tables are frozen: nothing here writes
//! them but the export-back.

use std::sync::LazyLock;

use awm_store::Migration;

pub const SCHEMA_VERSION: i64 = 6;

pub const MESSAGE: &str = "builtin:message";
pub const JOURNAL: &str = "builtin:journal";
pub const BRIEF: &str = "builtin:brief";
pub const CLOSED: &str = "builtin:closed";
pub const SUPERSEDED: &str = "builtin:superseded";
pub const PRECEDENT: &str = "builtin:precedent";
pub const FOLDED: &str = "builtin:folded";

pub const BUILTINS: [(&str, &str, &str); 7] = [
    (MESSAGE, "message", "A post by a user or by another scope or agent."),
    (JOURNAL, "journal", "A scope's own session log, the debrief entry."),
    (BRIEF, "brief", "What a scope is for within its project."),
    (CLOSED, "closed", "Done with, like a closed issue. A post without it is open."),
    (SUPERSEDED, "superseded", "A later post that replies to this one replaces it."),
    (
        PRECEDENT,
        "precedent",
        "A decision Tony made or overrode that holds beyond the session; its body holds the context, the question and the decision.",
    ),
    (
        FOLDED,
        "folded",
        "An addendum folded into the post it corrects, as a new version of that post. Reads skip it unless the tag filter names it.",
    ),
];

/// The tags that stand for what `kind` said.
pub const KIND_TAGS: [&str; 3] = [MESSAGE, JOURNAL, BRIEF];
/// Set when a post is made, never after.
pub const TYPE_TAGS: [&str; 4] = [MESSAGE, JOURNAL, BRIEF, PRECEDENT];
/// A thread is in the search index when its first post carries one of these.
pub const INDEXED_TAGS: [&str; 3] = [JOURNAL, MESSAGE, PRECEDENT];
pub const MIGRATED_PREFIX: &str = "migrated:";
const MIGRATED_DESCRIPTION: &str = "A post kind migrated from the old kind column.";
const KIND_TO_TAG: [(&str, &str); 7] = [
    ("message", MESSAGE),
    ("journal", JOURNAL),
    ("brief", BRIEF),
    ("precedent", PRECEDENT),
    ("goal", JOURNAL),
    ("debrief", JOURNAL),
    ("note", MESSAGE),
];

fn q(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

/// The fixed tags, inserted on a fresh schema, in the migration and on every start.
pub fn builtins_sql() -> String {
    BUILTINS
        .iter()
        .map(|(i, n, d)| {
            format!(
                "INSERT OR IGNORE INTO tags (id, name, description, origin, version, created_at, updated_at) VALUES ({}, {}, {}, '', 1, 0, 0);\n",
                q(i),
                q(n),
                q(d)
            )
        })
        .collect()
}

fn known_kinds_sql() -> String {
    KIND_TO_TAG.iter().map(|(k, _)| q(k)).collect::<Vec<_>>().join(", ")
}

fn kind_case_sql(column: &str) -> String {
    let whens: Vec<String> = KIND_TO_TAG.iter().map(|(k, t)| format!("WHEN {} THEN {}", q(k), q(t))).collect();
    format!("CASE {column} {} ELSE NULL END", whens.join(" "))
}

pub const CURSORS_SQL: &str = "\
CREATE TABLE IF NOT EXISTS scope_cursors (
    owner_project  TEXT NOT NULL,
    owner_scope    TEXT NOT NULL,
    through_ts     INTEGER NOT NULL,
    through_id     TEXT NOT NULL,
    acked_at       INTEGER NOT NULL,
    acked_by       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (owner_project, owner_scope)
);
";

const TAGS_DDL: &str = "\
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
";

fn tags_sql() -> String {
    format!("{TAGS_DDL}{}", builtins_sql())
}

const POSTS_DDL: &str = "\
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
";

/// The v6 schema before the reads, links, versions and feed tables: `dao.SCHEMA_SQL`.
pub fn schema_sql() -> String {
    format!("{POSTS_DDL}{CURSORS_SQL}{}", tags_sql())
}

pub const READS_SQL: &str = "\
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
";

const LINKS_SQL: &str = "\
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
";

const VERSIONS_SQL: &str = "\
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
";

const NEXT: &str = "(SELECT COALESCE(MAX(seq), 0) + 1 FROM search_feed)";

fn bump(root_sql: &str) -> String {
    format!(
        "INSERT INTO search_feed (root_id, seq) SELECT r, {NEXT} FROM ({root_sql}) WHERE r IS NOT NULL ON CONFLICT(root_id) DO UPDATE SET seq = excluded.seq;"
    )
}

fn root_of(post: &str) -> String {
    format!("SELECT COALESCE(thread_id, id) AS r FROM scope_posts WHERE id = {post}")
}

/// The search feed tables and their triggers. Nothing in this crate reads them; they stay so
/// that the export-back reaches the Python search after a rollback.
fn feed_sql() -> String {
    format!(
        "\
CREATE TABLE IF NOT EXISTS search_feed (
    root_id  TEXT PRIMARY KEY,
    seq      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_feed_seq ON search_feed(seq, root_id);
CREATE TABLE IF NOT EXISTS search_feed_epoch (epoch TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS search_feed_post_ai AFTER INSERT ON scope_posts BEGIN
    {}
END;
CREATE TRIGGER IF NOT EXISTS search_feed_post_au AFTER UPDATE ON scope_posts BEGIN
    {}
END;
CREATE TRIGGER IF NOT EXISTS search_feed_post_ad AFTER DELETE ON scope_posts BEGIN
    {}
END;
CREATE TRIGGER IF NOT EXISTS search_feed_tag_ai AFTER INSERT ON post_tags BEGIN
    {}
END;
CREATE TRIGGER IF NOT EXISTS search_feed_tag_ad AFTER DELETE ON post_tags BEGIN
    {}
END;
CREATE TRIGGER IF NOT EXISTS search_feed_cite_ai AFTER INSERT ON post_cites BEGIN
    {}
END;
CREATE TRIGGER IF NOT EXISTS search_feed_cite_ad AFTER DELETE ON post_cites BEGIN
    {}
END;
CREATE TRIGGER IF NOT EXISTS search_feed_tag_rename AFTER UPDATE OF name ON tags BEGIN
    {}
END;
",
        bump("SELECT COALESCE(NEW.thread_id, NEW.id) AS r"),
        bump("SELECT COALESCE(NEW.thread_id, NEW.id) AS r UNION SELECT COALESCE(OLD.thread_id, OLD.id)"),
        bump("SELECT COALESCE(OLD.thread_id, OLD.id) AS r"),
        bump(&root_of("NEW.post_id")),
        bump(&root_of("OLD.post_id")),
        bump(&root_of("NEW.post_id")),
        bump(&root_of("OLD.post_id")),
        bump("SELECT DISTINCT COALESCE(p.thread_id, p.id) AS r FROM post_tags pt JOIN scope_posts p ON p.id = pt.post_id WHERE pt.tag_id = NEW.id"),
    )
}

fn indexed_sql_literal() -> String {
    let marks: Vec<String> = INDEXED_TAGS.iter().map(|t| q(t)).collect();
    format!(
        " AND thread_id IS NULL AND EXISTS (SELECT 1 FROM post_tags it WHERE it.post_id = scope_posts.id AND it.tag_id IN ({})) AND NOT EXISTS (SELECT 1 FROM post_tags ft WHERE ft.post_id = scope_posts.id AND ft.tag_id = {})",
        marks.join(","),
        q(FOLDED)
    )
}

fn feed_seed_sql() -> String {
    format!(
        "INSERT OR IGNORE INTO search_feed (root_id, seq) SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM scope_posts WHERE body != ''{};\n",
        indexed_sql_literal()
    )
}

fn migration_4_5() -> String {
    let supersedes = "json_extract(meta, '$.supersedes')";
    let goal_test = "kind = 'goal' AND (owner_project = '_goaltest' OR owner_scope = 'project:_goaltest' OR body LIKE 'TEST-%' OR lower(body) LIKE 'test cleanup%')";
    let cites = "json_valid(meta) AND json_type(meta, '$.cites') = 'array'";
    let known = known_kinds_sql();
    format!(
        "{tags}{LINKS_SQL}\
ALTER TABLE scope_posts ADD COLUMN reply_to TEXT;
ALTER TABLE scope_posts ADD COLUMN thread_id TEXT;
DELETE FROM scope_posts WHERE {goal_test};
INSERT OR IGNORE INTO tags (id, name, description, origin, version, created_at, updated_at)
    SELECT DISTINCT '{MIGRATED_PREFIX}' || lower(trim(kind)), lower(trim(kind)),
           '{MIGRATED_DESCRIPTION}', '', 1, 0, 0
    FROM scope_posts WHERE trim(kind) != '' AND lower(trim(kind)) NOT IN ({known});
INSERT OR IGNORE INTO post_tags (post_id, tag_id)
    SELECT id, {case} FROM scope_posts
    WHERE lower(trim(kind)) IN ({known});
INSERT OR IGNORE INTO post_tags (post_id, tag_id)
    SELECT p.id, t.id FROM scope_posts p JOIN tags t ON t.name = lower(trim(p.kind))
    WHERE lower(trim(p.kind)) NOT IN ({known});
INSERT OR IGNORE INTO post_tags (post_id, tag_id)
    SELECT id, '{MESSAGE}' FROM scope_posts WHERE trim(kind) = '';
UPDATE scope_posts SET reply_to = {supersedes}
    WHERE kind = 'brief' AND {supersedes} IN (SELECT id FROM scope_posts WHERE kind = 'brief');
INSERT OR IGNORE INTO post_tags (post_id, tag_id)
    SELECT reply_to, '{SUPERSEDED}' FROM scope_posts WHERE reply_to IS NOT NULL;
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
    WHERE {cites} AND EXISTS (SELECT 1 FROM json_each(scope_posts.meta, '$.cites') c
                              WHERE json_extract(c.value, '$.reason') IS NOT NULL);
UPDATE scope_posts SET meta = json_remove(meta, '$.cites') WHERE {cites};
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
",
        tags = tags_sql(),
        case = kind_case_sql("lower(trim(kind))"),
    )
}

/// The whole fresh schema, as `dao.init` passes it.
pub static FRESH: LazyLock<String> =
    LazyLock::new(|| format!("{}{READS_SQL}{LINKS_SQL}{VERSIONS_SQL}{}", schema_sql(), feed_sql()));

fn leak(s: String) -> &'static str {
    Box::leak(s.into_boxed_str())
}

pub static MIGRATIONS: LazyLock<Vec<Migration>> = LazyLock::new(|| {
    vec![
        Migration { from: 1, sql: CURSORS_SQL },
        Migration { from: 2, sql: READS_SQL },
        Migration {
            from: 3,
            sql: leak(format!("ALTER TABLE scope_posts ADD COLUMN version INTEGER NOT NULL DEFAULT 1;\n{VERSIONS_SQL}")),
        },
        Migration { from: 4, sql: leak(migration_4_5()) },
        Migration { from: 5, sql: leak(format!("{}{}", feed_sql(), feed_seed_sql())) },
    ]
});

