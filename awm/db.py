"""SQLite setup + migrations."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from awm.config import DB_PATH, AWM_DIR

SCHEMA_VERSION = 38

# Sentinel user uuids — fixed namespace so a fresh install lands the same
# 'cli'/'system' ids regardless of when/where it's run. The v37 preflight
# overrides its per-peer derivation with these so the workspace migration
# and a fresh dev install both end up with the same sentinel ids.
_SENTINEL_USERS_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "awm.v37.users.sentinels")
SENTINEL_USER_IDS = {
    "cli": str(uuid.uuid5(_SENTINEL_USERS_NS, "cli")),
    "system": str(uuid.uuid5(_SENTINEL_USERS_NS, "system")),
}


def new_uuid() -> str:
    """Mint a fresh uuid4 hex for use as a row primary key."""
    return uuid.uuid4().hex


SCHEMA_SQL = """\
-- v37 schema (16 tables). The identity layer (projects/users/agents/
-- agent_instances) is the source of truth; every data row points at a
-- uuid agent_id and human-facing names live on the identity tables where
-- a rename is a single UPDATE.
--
-- Conventions
--   * every v37-new ``id`` is a TEXT uuid4 (or uuid5 for deterministic
--     migration backfill). Legacy session_logs/artifacts/messages keep
--     INTEGER AUTOINCREMENT — there's no cross-peer collision risk now
--     that they're not CRRs anymore.
--   * ``*_at`` columns are INTEGER unix-ms.
--   * polymorphic refs (guest_ref / room_transcripts.author /
--     messages.recipient_id / messages.sender_id) are TEXT carrying
--     either an agents.id, a users.id, or the literal sentinel 'system'.
--     No DDL FK because SQLite doesn't do polymorphic FKs; integrity is
--     a preflight + foreign_key_check responsibility during migration
--     and an app-layer responsibility going forward.

-- Identity ------------------------------------------------------------

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
CREATE INDEX IF NOT EXISTS idx_agents_project_status
    ON agents(project_id, status);
CREATE INDEX IF NOT EXISTS idx_agents_parent
    ON agents(parent_id);

CREATE TABLE IF NOT EXISTS agent_instances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT NOT NULL REFERENCES agents(id),
    cli_session_id  TEXT,
    log_path        TEXT,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    data            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_agent_instances_agent_started
    ON agent_instances(agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_instances_open
    ON agent_instances(agent_id) WHERE ended_at IS NULL;

-- Rooms ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS rooms (
    id              TEXT PRIMARY KEY,
    owner_agent_id  TEXT NOT NULL REFERENCES agents(id),
    topic           TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    closed_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rooms_owner
    ON rooms(owner_agent_id);
CREATE INDEX IF NOT EXISTS idx_rooms_status
    ON rooms(status);

CREATE TABLE IF NOT EXISTS guest_list (
    room_id        TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    guest_kind     TEXT NOT NULL,
    guest_ref      TEXT NOT NULL,
    display_name   TEXT NOT NULL,
    subscriptions  TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (room_id, guest_kind, guest_ref)
);

CREATE TABLE IF NOT EXISTS room_transcripts (
    id       TEXT PRIMARY KEY,
    room_id  TEXT NOT NULL REFERENCES rooms(id),
    author   TEXT NOT NULL,
    kind     TEXT NOT NULL,
    body     TEXT NOT NULL DEFAULT '',
    meta     TEXT NOT NULL DEFAULT '{}',
    ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_room_transcripts_room_ts
    ON room_transcripts(room_id, ts);
CREATE INDEX IF NOT EXISTS idx_room_transcripts_kind
    ON room_transcripts(room_id, kind, ts);
-- kind ∈ { message, tool_use, tool_result, system, notification, join,
--          leave, slash, session_start, session_end, compact, system_prompt }

-- Data tables (rewritten in v37 — CRR plumbing dropped, uuid agent_id FK)

CREATE TABLE IF NOT EXISTS session_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id      TEXT NOT NULL REFERENCES agents(id),
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
CREATE INDEX IF NOT EXISTS idx_session_logs_agent
    ON session_logs(agent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS artifacts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id       TEXT NOT NULL REFERENCES agents(id),
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
CREATE INDEX IF NOT EXISTS idx_artifacts_agent
    ON artifacts(agent_id, created_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id  TEXT NOT NULL,
    sender_id     TEXT NOT NULL,
    msg_type      TEXT NOT NULL DEFAULT '',
    subject       TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    metadata      TEXT,
    status        TEXT NOT NULL DEFAULT 'unread',
    created_at    INTEGER NOT NULL,
    read_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient_status
    ON messages(recipient_id, status, created_at DESC);

-- Unchanged (structure carries over from v36) -------------------------

-- locks: process-coordination primitive used by awm-internal services
-- (shared_resources, tool_dispatch, federation). v36 schema preserved
-- verbatim — TEXT timestamps stay because every existing caller reads
-- them as ISO strings.
CREATE TABLE IF NOT EXISTS locks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_path TEXT NOT NULL,
    holder_id TEXT NOT NULL,
    holder_pid INTEGER,
    lock_type TEXT NOT NULL DEFAULT 'exclusive',
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    metadata TEXT,
    UNIQUE(resource_path, holder_id)
);

CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_type, source_id)
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discord_operators (
    discord_user_id TEXT NOT NULL PRIMARY KEY,
    awm_user TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL DEFAULT '',
    origin_peer TEXT
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""

# Legacy migrations keyed by (from_version, to_version) — preserved for
# any DB still on a pre-v36 schema. The v37 step (36, 37) is dispatched
# to _migrate_v37 directly; legacy entries below run via the generic
# executescript path.
MIGRATIONS = {
    (1, 2): """\
CREATE TABLE IF NOT EXISTS session_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    task TEXT NOT NULL,
    file_path TEXT NOT NULL,
    git_commit TEXT,
    logged_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    agent_id TEXT DEFAULT 'unknown',
    metadata TEXT,
    UNIQUE(project, task, logged_at, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_session_logs_project_task
    ON session_logs(project, task);
""",
    (2, 3): """\
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    branch TEXT NOT NULL,
    worktree TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project, task)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
""",
    (3, 4): """\
CREATE TABLE IF NOT EXISTS tasks_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    branch TEXT NOT NULL,
    worktree TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO tasks_new SELECT * FROM tasks;
DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;
CREATE UNIQUE INDEX idx_tasks_active_unique
    ON tasks(project, task) WHERE status = 'active';
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_project ON tasks(project);
""",
    (4, 5): """\
ALTER TABLE tasks ADD COLUMN repo_path TEXT;
""",
    (5, 6): """\
UPDATE tasks SET worktree = REPLACE(worktree,
    '/' || project || '/' || task,
    '/' || project || '/tasks/' || task)
WHERE worktree LIKE '%/main/%' AND worktree NOT LIKE '%/tasks/%';
UPDATE session_logs SET file_path = REPLACE(file_path,
    'main/' || project || '/' || task || '/',
    'main/' || project || '/tasks/' || task || '/')
WHERE file_path LIKE 'main/%' AND file_path NOT LIKE '%/tasks/%';
""",
    (6, 7): """\
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    sender TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata TEXT,
    status TEXT NOT NULL DEFAULT 'unread',
    created_at TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages(scope);
CREATE INDEX IF NOT EXISTS idx_messages_scope_status ON messages(scope, status);
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO config (key, value, updated_at)
    VALUES ('agent_cli', 'opencode', datetime('now'));
""",
    (7, 8): "ALTER TABLE tasks ADD COLUMN session INTEGER NOT NULL DEFAULT 1;",
    (8, 9): "ALTER TABLE session_logs ADD COLUMN content TEXT;",
    (9, 10): """\
ALTER TABLE tasks RENAME TO scopes;
ALTER TABLE scopes RENAME COLUMN task TO scope;
CREATE TABLE IF NOT EXISTS experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_path TEXT, skill_version TEXT,
    project TEXT NOT NULL, scope TEXT NOT NULL,
    agent_id TEXT DEFAULT 'unknown', outcome TEXT,
    summary TEXT NOT NULL, deviations TEXT, suggestions TEXT,
    metadata TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiences_skill ON experiences(skill_path);
CREATE INDEX IF NOT EXISTS idx_experiences_project ON experiences(project);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL, scope TEXT NOT NULL, name TEXT NOT NULL,
    artifact_type TEXT NOT NULL, path TEXT NOT NULL,
    description TEXT, format TEXT, tags TEXT,
    status TEXT NOT NULL DEFAULT 'current',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path);
""",
    (10, 11): """\
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL, source_id TEXT NOT NULL,
    chunk_text TEXT NOT NULL, embedding BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_type, source_id)
);
""",
    (11, 12): "ALTER TABLE session_logs RENAME COLUMN task TO scope;",
    (12, 13): "ALTER TABLE session_logs ADD COLUMN skill_path TEXT;",
    (13, 14): """\
ALTER TABLE session_logs ADD COLUMN outcome TEXT;
ALTER TABLE session_logs ADD COLUMN deviations TEXT;
ALTER TABLE session_logs ADD COLUMN suggestions TEXT;
ALTER TABLE session_logs ADD COLUMN skill_version TEXT;
INSERT INTO session_logs
    (project, scope, file_path, git_commit, logged_at, summary, agent_id,
     metadata, content, skill_path, outcome, deviations, suggestions, skill_version)
SELECT project, scope, '', NULL, created_at, summary, agent_id,
       metadata, NULL, skill_path, outcome, deviations, suggestions, skill_version
FROM experiences;
DELETE FROM embeddings WHERE source_type = 'experience';
DROP TABLE IF EXISTS experiences;
""",
    (14, 15): """\
ALTER TABLE session_logs ADD COLUMN resolved_at TEXT;
ALTER TABLE session_logs ADD COLUMN resolution TEXT;
""",
    (15, 16): "ALTER TABLE session_logs ADD COLUMN title TEXT;",
    (16, 17): """\
CREATE TABLE IF NOT EXISTS agent_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL, scope TEXT NOT NULL,
    pid INTEGER NOT NULL, status TEXT NOT NULL,
    agent_cli TEXT NOT NULL, started_at TEXT NOT NULL,
    exited_at TEXT, exit_code INTEGER, log_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_scope ON agent_sessions(project, scope);
""",
    (17, 18): """\
CREATE TABLE IF NOT EXISTS peers (
    peer_id TEXT PRIMARY KEY, base_url TEXT NOT NULL, token_path TEXT NOT NULL,
    friendly_name TEXT, last_seen TEXT, added_at TEXT NOT NULL
);
""",
    (18, 19): """\
CREATE TABLE peers_new (
    peer_id TEXT PRIMARY KEY, ssh_alias TEXT NOT NULL,
    remote_port INTEGER NOT NULL DEFAULT 7820,
    friendly_name TEXT, last_seen TEXT, added_at TEXT NOT NULL
);
DROP TABLE peers;
ALTER TABLE peers_new RENAME TO peers;
""",
    (19, 20): """\
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY, host_peer_id TEXT NOT NULL,
    created_at TEXT NOT NULL, closed_at TEXT, topic TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS room_participants (
    room_id TEXT NOT NULL, kind TEXT NOT NULL, identifier TEXT NOT NULL,
    joined_at TEXT NOT NULL, left_at TEXT,
    PRIMARY KEY (room_id, kind, identifier)
);
CREATE TABLE IF NOT EXISTS room_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL, author TEXT NOT NULL,
    body TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'text',
    ts TEXT NOT NULL,
    FOREIGN KEY (room_id) REFERENCES rooms(id)
);
CREATE INDEX IF NOT EXISTS idx_room_posts_room_ts ON room_posts(room_id, ts);
CREATE INDEX IF NOT EXISTS idx_rooms_status ON rooms(status);
CREATE INDEX IF NOT EXISTS idx_room_participants_scope
    ON room_participants(kind, identifier) WHERE left_at IS NULL;
""",
    (20, 21): """\
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_sessions_active_unique
    ON agent_sessions(project, scope)
    WHERE status IN ('starting', 'running', 'stopping', 'orphaned');
""",
    (21, 22): "ALTER TABLE rooms ADD COLUMN close_on_exit INTEGER NOT NULL DEFAULT 0;",
    (22, 23): "SELECT 1;",
    (23, 24): "ALTER TABLE agent_sessions ADD COLUMN claude_session_id TEXT;",
    (24, 25): """\
ALTER TABLE peers ADD COLUMN endpoints TEXT;
ALTER TABLE peers ADD COLUMN tls_fingerprint TEXT;
""",
    (25, 26): """\
CREATE TABLE IF NOT EXISTS discord_operators (
    discord_user_id TEXT PRIMARY KEY,
    awm_user TEXT NOT NULL, added_at TEXT NOT NULL
);
""",
    (26, 27): "ALTER TABLE peers ADD COLUMN peer_priority INTEGER NOT NULL DEFAULT 100;",
    (27, 28): """\
CREATE TABLE rooms_new (
    id TEXT NOT NULL PRIMARY KEY, host_peer_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '', closed_at TEXT, topic TEXT,
    status TEXT NOT NULL DEFAULT 'active', close_on_exit INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT
);
INSERT INTO rooms_new (id, host_peer_id, created_at, closed_at, topic, status, close_on_exit, origin_peer)
    SELECT id, host_peer_id, created_at, closed_at, topic, status, close_on_exit, host_peer_id
    FROM rooms WHERE id IS NOT NULL;
DROP TABLE rooms;
ALTER TABLE rooms_new RENAME TO rooms;
CREATE INDEX IF NOT EXISTS idx_rooms_status ON rooms(status);
CREATE TABLE room_participants_new (
    room_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
    identifier TEXT NOT NULL DEFAULT '', joined_at TEXT NOT NULL DEFAULT '',
    left_at TEXT, origin_peer TEXT,
    PRIMARY KEY (room_id, kind, identifier)
);
INSERT INTO room_participants_new (room_id, kind, identifier, joined_at, left_at, origin_peer)
    SELECT room_id, kind, identifier, joined_at, left_at, NULL FROM room_participants;
DROP TABLE room_participants;
ALTER TABLE room_participants_new RENAME TO room_participants;
CREATE INDEX IF NOT EXISTS idx_room_participants_scope
    ON room_participants(kind, identifier) WHERE left_at IS NULL;
CREATE TABLE peers_new (
    peer_id TEXT NOT NULL PRIMARY KEY, ssh_alias TEXT NOT NULL DEFAULT '',
    remote_port INTEGER NOT NULL DEFAULT 7820, friendly_name TEXT,
    last_seen TEXT, added_at TEXT NOT NULL DEFAULT '',
    endpoints TEXT, tls_fingerprint TEXT,
    peer_priority INTEGER NOT NULL DEFAULT 100, origin_peer TEXT
);
INSERT INTO peers_new
    SELECT peer_id, ssh_alias, remote_port, friendly_name, last_seen, added_at,
           endpoints, tls_fingerprint, peer_priority, peer_id
    FROM peers WHERE peer_id IS NOT NULL;
DROP TABLE peers;
ALTER TABLE peers_new RENAME TO peers;
CREATE TABLE discord_operators_new (
    discord_user_id TEXT NOT NULL PRIMARY KEY, awm_user TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL DEFAULT '', origin_peer TEXT
);
INSERT INTO discord_operators_new
    SELECT discord_user_id, awm_user, added_at, NULL
    FROM discord_operators WHERE discord_user_id IS NOT NULL;
DROP TABLE discord_operators;
ALTER TABLE discord_operators_new RENAME TO discord_operators;
""",
    (28, 29): """\
CREATE TABLE IF NOT EXISTS peer_sync_state (
    peer_id TEXT PRIMARY KEY, last_db_version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);
""",
    (29, 30): """\
CREATE TABLE room_posts_new (
    uuid TEXT NOT NULL PRIMARY KEY, legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '', room_id TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'text', ts TEXT NOT NULL DEFAULT ''
);
INSERT INTO room_posts_new (uuid, legacy_id, origin_peer, room_id, author, body, kind, ts)
    SELECT lower(hex(randomblob(16))), room_posts.id,
           COALESCE((SELECT host_peer_id FROM rooms WHERE rooms.id = room_posts.room_id), ''),
           room_posts.room_id, room_posts.author, room_posts.body, room_posts.kind, room_posts.ts
    FROM room_posts;
DROP TABLE room_posts;
ALTER TABLE room_posts_new RENAME TO room_posts;
CREATE INDEX IF NOT EXISTS idx_room_posts_room_ts ON room_posts(room_id, ts);
CREATE INDEX IF NOT EXISTS idx_room_posts_legacy ON room_posts(legacy_id, origin_peer);
""",
    (30, 31): """\
DROP INDEX IF EXISTS idx_scopes_active_unique;
CREATE TABLE scopes_new (
    uuid TEXT NOT NULL PRIMARY KEY, legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
    branch TEXT NOT NULL DEFAULT '', worktree TEXT NOT NULL DEFAULT '',
    repo_path TEXT, session INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
);
INSERT INTO scopes_new (uuid, legacy_id, origin_peer, project, scope, status,
                        branch, worktree, repo_path, session, created_at, updated_at)
    SELECT lower(hex(randomblob(16))), id,
           COALESCE((SELECT peer_id FROM peers WHERE ssh_alias = 'self' LIMIT 1), ''),
           project, scope, status, branch, worktree, repo_path,
           session, created_at, updated_at FROM scopes;
DROP TABLE scopes;
ALTER TABLE scopes_new RENAME TO scopes;
CREATE INDEX IF NOT EXISTS idx_scopes_status ON scopes(status);
CREATE INDEX IF NOT EXISTS idx_scopes_project ON scopes(project);
CREATE INDEX IF NOT EXISTS idx_scopes_legacy ON scopes(legacy_id, origin_peer);
""",
    (31, 32): """\
CREATE TABLE session_logs_new (
    uuid TEXT NOT NULL PRIMARY KEY, legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '', file_path TEXT NOT NULL DEFAULT '',
    git_commit TEXT, logged_at TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '', agent_id TEXT NOT NULL DEFAULT 'unknown',
    metadata TEXT, content TEXT, skill_path TEXT, outcome TEXT,
    deviations TEXT, suggestions TEXT, skill_version TEXT,
    resolved_at TEXT, resolution TEXT, title TEXT
);
INSERT INTO session_logs_new (uuid, legacy_id, origin_peer, project, scope,
                              file_path, git_commit, logged_at, summary,
                              agent_id, metadata, content, skill_path,
                              outcome, deviations, suggestions, skill_version,
                              resolved_at, resolution, title)
    SELECT lower(hex(randomblob(16))), id,
           COALESCE((SELECT peer_id FROM peers WHERE ssh_alias = 'self' LIMIT 1), ''),
           project, scope, COALESCE(file_path, ''), git_commit, logged_at, summary,
           COALESCE(agent_id, 'unknown'), metadata, content, skill_path,
           outcome, deviations, suggestions, skill_version,
           resolved_at, resolution, title FROM session_logs;
DROP TABLE session_logs;
ALTER TABLE session_logs_new RENAME TO session_logs;
CREATE INDEX IF NOT EXISTS idx_session_logs_project_scope ON session_logs(project, scope);
CREATE INDEX IF NOT EXISTS idx_session_logs_legacy ON session_logs(legacy_id, origin_peer);
""",
    (32, 33): """\
CREATE TABLE messages_new (
    uuid TEXT NOT NULL PRIMARY KEY, legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '', msg_type TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
    metadata TEXT, status TEXT NOT NULL DEFAULT 'unread',
    created_at TEXT NOT NULL DEFAULT '', read_at TEXT
);
INSERT INTO messages_new (uuid, legacy_id, origin_peer, scope, sender,
                          msg_type, subject, body, metadata, status,
                          created_at, read_at)
    SELECT lower(hex(randomblob(16))), id,
           COALESCE((SELECT peer_id FROM peers WHERE ssh_alias = 'self' LIMIT 1), ''),
           scope, sender, msg_type, subject, body, metadata, status,
           created_at, read_at FROM messages;
DROP TABLE messages;
ALTER TABLE messages_new RENAME TO messages;
CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages(scope);
CREATE INDEX IF NOT EXISTS idx_messages_scope_status ON messages(scope, status);
CREATE INDEX IF NOT EXISTS idx_messages_legacy ON messages(legacy_id, origin_peer);
""",
    (33, 34): """\
DROP INDEX IF EXISTS idx_artifacts_path;
CREATE TABLE artifacts_new (
    uuid TEXT NOT NULL PRIMARY KEY, legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
    artifact_type TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
    description TEXT, format TEXT, tags TEXT,
    status TEXT NOT NULL DEFAULT 'current',
    created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
);
INSERT INTO artifacts_new (uuid, legacy_id, origin_peer, project, scope, name,
                           artifact_type, path, description, format, tags,
                           status, created_at, updated_at)
    SELECT lower(hex(randomblob(16))), id,
           COALESCE((SELECT peer_id FROM peers WHERE ssh_alias = 'self' LIMIT 1), ''),
           project, scope, name, artifact_type, path, description, format,
           tags, status, created_at, updated_at FROM artifacts;
DROP TABLE artifacts;
ALTER TABLE artifacts_new RENAME TO artifacts;
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project);
CREATE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path);
CREATE INDEX IF NOT EXISTS idx_artifacts_legacy ON artifacts(legacy_id, origin_peer);
""",
    (34, 35): """\
ALTER TABLE agent_sessions ADD COLUMN intent TEXT NOT NULL DEFAULT 'live';
CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
    project TEXT NOT NULL, scope TEXT NOT NULL, agent_cli TEXT NOT NULL,
    seq INTEGER NOT NULL, ts TEXT NOT NULL, direction TEXT NOT NULL,
    event_type TEXT NOT NULL, body TEXT NOT NULL, claude_session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_events_session_seq ON agent_events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_agent_events_scope_ts ON agent_events(project, scope, ts);
CREATE INDEX IF NOT EXISTS idx_agent_events_claude_sid ON agent_events(claude_session_id);
CREATE TABLE IF NOT EXISTS agent_resume_queue (
    session_id INTEGER PRIMARY KEY, scheduled_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, prior_exited_at TEXT, primer_text TEXT
);
""",
    (35, 36): """\
CREATE TABLE rooms_new (
    id TEXT NOT NULL PRIMARY KEY, host_peer_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '', closed_at TEXT, topic TEXT,
    status TEXT NOT NULL DEFAULT 'active', close_on_exit INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT, owner_scope_key TEXT
);
INSERT INTO rooms_new (id, host_peer_id, created_at, closed_at, topic,
                       status, close_on_exit, origin_peer, owner_scope_key)
    SELECT id, host_peer_id, created_at, closed_at, topic, status, close_on_exit, origin_peer,
        CASE WHEN (SELECT COUNT(*) FROM room_participants rp
                    WHERE rp.room_id = rooms.id AND rp.kind = 'scope'
                      AND rp.left_at IS NULL) = 1
             THEN (SELECT rp.identifier FROM room_participants rp
                    WHERE rp.room_id = rooms.id AND rp.kind = 'scope'
                      AND rp.left_at IS NULL LIMIT 1)
             ELSE NULL END
    FROM rooms;
DROP TABLE rooms;
ALTER TABLE rooms_new RENAME TO rooms;
CREATE INDEX IF NOT EXISTS idx_rooms_status ON rooms(status);
CREATE INDEX IF NOT EXISTS idx_rooms_owner_scope ON rooms(owner_scope_key);
CREATE TABLE room_participants_new (
    room_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
    identifier TEXT NOT NULL DEFAULT '', joined_at TEXT NOT NULL DEFAULT '',
    left_at TEXT, origin_peer TEXT, guest_name TEXT, subscriptions TEXT,
    PRIMARY KEY (room_id, kind, identifier)
);
INSERT INTO room_participants_new (room_id, kind, identifier, joined_at,
                                    left_at, origin_peer, guest_name, subscriptions)
    SELECT room_id, kind, identifier, joined_at, left_at, origin_peer, identifier, NULL
    FROM room_participants;
DROP TABLE room_participants;
ALTER TABLE room_participants_new RENAME TO room_participants;
CREATE INDEX IF NOT EXISTS idx_room_participants_scope
    ON room_participants(kind, identifier) WHERE left_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_room_participants_guest_name
    ON room_participants(room_id, guest_name) WHERE left_at IS NULL;
CREATE TABLE room_posts_new (
    uuid TEXT NOT NULL PRIMARY KEY, legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '', room_id TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'text', ts TEXT NOT NULL DEFAULT '', meta TEXT
);
INSERT INTO room_posts_new (uuid, legacy_id, origin_peer, room_id, author,
                             body, kind, ts, meta)
    SELECT uuid, legacy_id, origin_peer, room_id, author, body, kind, ts, NULL FROM room_posts;
DROP TABLE room_posts;
ALTER TABLE room_posts_new RENAME TO room_posts;
CREATE INDEX IF NOT EXISTS idx_room_posts_room_ts ON room_posts(room_id, ts);
CREATE INDEX IF NOT EXISTS idx_room_posts_legacy ON room_posts(legacy_id, origin_peer);
CREATE TABLE IF NOT EXISTS agent_relationships (
    child_project TEXT NOT NULL, child_scope TEXT NOT NULL,
    parent_project TEXT NOT NULL, parent_scope TEXT NOT NULL,
    is_vagrant INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (child_project, child_scope)
);
CREATE INDEX IF NOT EXISTS idx_agent_relationships_parent
    ON agent_relationships(parent_project, parent_scope);
""",
    (37, 38): """\
-- Federation retirement: single-host now. Collapse all origin_peer values
-- to '' so post-v38 queries (e.g. embedding indexers) can filter
-- local-origin rows with `origin_peer = ''` without consulting the
-- about-to-be-dropped peers table.
UPDATE scopes SET origin_peer = '';
UPDATE session_logs SET origin_peer = '';
UPDATE messages SET origin_peer = '';
UPDATE artifacts SET origin_peer = '';
UPDATE room_posts SET origin_peer = '';
DROP TABLE IF EXISTS peer_sync_state;
DROP TABLE IF EXISTS peers;
""",
}


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Return a new SQLite connection with WAL mode enabled."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _iso_to_ms_or_zero(iso_str: str | None) -> int:
    """ISO-8601 → unix-ms, returning 0 for empty/invalid. Used during the
    v37 backfill where created_at INTEGER is NOT NULL."""
    if not iso_str:
        return 0
    s = iso_str.strip()
    if not s:
        return 0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _iso_to_ms_or_null(iso_str: str | None) -> int | None:
    """ISO-8601 → unix-ms, returning None for empty/invalid."""
    ms = _iso_to_ms_or_zero(iso_str)
    return ms or None


def _migrate_v37(conn: sqlite3.Connection) -> None:
    """v37 transactional rebuild: identity layer + room transcripts + drop
    CRR plumbing on the data tables. Reads the seed manifest at
    ``<db_dir>/migrations/v37-seed.json`` (generated by
    ``awm.services.migration_v37:preflight``); regenerates it on the fly if
    absent. Bracketed in an explicit BEGIN IMMEDIATE so a mid-flight
    exception rolls back cleanly to v36.
    """
    from awm.services.migration_v37 import preflight, load_manifest

    # Resolve the open DB's filesystem path so we know where to write/read
    # the seed manifest.
    db_path_row = conn.execute(
        "SELECT file FROM pragma_database_list WHERE name='main'"
    ).fetchone()
    if not db_path_row or not db_path_row[0]:
        raise RuntimeError("cannot determine source DB path for v37 migration")
    db_path = Path(db_path_row[0])
    seed_path = db_path.parent / "migrations" / "v37-seed.json"
    if not seed_path.is_file():
        seed_path = preflight(db_path)
    seed = load_manifest(seed_path)

    # ``cli`` / ``system`` users always land on the fixed sentinel uuids so
    # any subsequent fresh install lines up with the migrated workspace.
    for name, sentinel_id in SENTINEL_USER_IDS.items():
        seed.users[name] = sentinel_id
        # If the ref_resolution had already pointed 'user:<name>' at a
        # per-peer uuid for cli/system, retarget it too.
        seed.ref_resolution[f"user:{name}"] = sentinel_id
    # Bare 'system' literal always resolves to the sentinel string.
    seed.ref_resolution["system"] = "system"

    # Foreign keys OFF for the DROP/RENAME dance; foreign_key_check at the
    # end of the transaction verifies declared FKs after backfill.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Step 2: rename the only colliding legacy table. ALTER … RENAME TO
        # keeps indices attached to the renamed table but does NOT rename
        # them, so we drop the legacy index names that v37 reuses on the
        # new ``rooms``.
        conn.execute("ALTER TABLE rooms RENAME TO rooms_legacy_v36")
        for legacy_idx in (
            "idx_rooms_status", "idx_rooms_owner_scope",
            "idx_room_participants_scope", "idx_room_participants_guest_name",
            "idx_room_posts_room_ts", "idx_room_posts_legacy",
            "idx_session_logs_project_scope", "idx_session_logs_legacy",
            "idx_artifacts_project", "idx_artifacts_path", "idx_artifacts_legacy",
            "idx_messages_scope", "idx_messages_scope_status", "idx_messages_legacy",
            "idx_agent_sessions_status", "idx_agent_sessions_scope",
            "idx_agent_sessions_active_unique",
            "idx_agent_events_session_seq", "idx_agent_events_scope_ts",
            "idx_agent_events_claude_sid",
            "idx_agent_relationships_parent",
            "idx_scopes_status", "idx_scopes_project", "idx_scopes_legacy",
        ):
            conn.execute(f"DROP INDEX IF EXISTS {legacy_idx}")

        # Step 3: create the v37 tables (identity + rooms shape).
        _create_v37_tables(conn)

        # Step 4: backfill identity + rooms from the seed manifest.
        _backfill_projects(conn, seed)
        _backfill_users(conn, seed)
        _backfill_agents(conn, seed)
        _backfill_agent_instances(conn, seed)
        _backfill_rooms(conn, seed)
        _backfill_guest_list(conn, seed)
        _backfill_room_transcripts(conn, seed)

        # Step 5: rebuild dance for the data tables.
        _rebuild_session_logs(conn, seed)
        _rebuild_artifacts(conn, seed)
        _rebuild_messages(conn, seed)

        # Step 6: drop retiring tables (and the renamed legacy rooms).
        # Note: ``locks`` survives v37 unchanged — it's a process-
        # coordination primitive with several live readers (shared_resources,
        # tool_dispatch, federation locks endpoints). The v36 row shape is
        # carried over verbatim.
        for table in (
            "shared_edits", "room_posts", "room_participants",
            "rooms_legacy_v36", "agent_relationships",
            "agent_resume_queue", "agent_events", "agent_sessions",
            "scopes",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")

        # Step 7: declared-FK integrity check.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            sample = violations[:10]
            raise RuntimeError(
                f"v37 migration left {len(violations)} FK violations: "
                f"first 10 = {sample}"
            )

        # Step 8: bump schema version.
        conn.execute("UPDATE schema_version SET version = 37")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _create_v37_tables(conn: sqlite3.Connection) -> None:
    """CREATE statements for the v37 new tables only — invoked from inside
    the BEGIN IMMEDIATE in _migrate_v37 (executescript() can't be used
    inside an active transaction; SQLite issues an implicit COMMIT first).
    """
    statements = [
        """CREATE TABLE projects (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            url         TEXT,
            repo_path   TEXT NOT NULL,
            created_at  INTEGER NOT NULL
        )""",
        """CREATE TABLE users (
            id        TEXT PRIMARY KEY,
            username  TEXT NOT NULL UNIQUE
        )""",
        """CREATE TABLE agents (
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
        )""",
        "CREATE INDEX idx_agents_project_status ON agents(project_id, status)",
        "CREATE INDEX idx_agents_parent ON agents(parent_id)",
        """CREATE TABLE agent_instances (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id        TEXT NOT NULL REFERENCES agents(id),
            cli_session_id  TEXT,
            log_path        TEXT,
            started_at      INTEGER NOT NULL,
            ended_at        INTEGER,
            data            TEXT NOT NULL DEFAULT '{}'
        )""",
        "CREATE INDEX idx_agent_instances_agent_started ON agent_instances(agent_id, started_at DESC)",
        "CREATE INDEX idx_agent_instances_open ON agent_instances(agent_id) WHERE ended_at IS NULL",
        """CREATE TABLE rooms (
            id              TEXT PRIMARY KEY,
            owner_agent_id  TEXT NOT NULL REFERENCES agents(id),
            topic           TEXT NOT NULL,
            status          TEXT NOT NULL,
            created_at      INTEGER NOT NULL,
            closed_at       INTEGER
        )""",
        "CREATE INDEX idx_rooms_owner ON rooms(owner_agent_id)",
        "CREATE INDEX idx_rooms_status ON rooms(status)",
        """CREATE TABLE guest_list (
            room_id        TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            guest_kind     TEXT NOT NULL,
            guest_ref      TEXT NOT NULL,
            display_name   TEXT NOT NULL,
            subscriptions  TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (room_id, guest_kind, guest_ref)
        )""",
        """CREATE TABLE room_transcripts (
            id       TEXT PRIMARY KEY,
            room_id  TEXT NOT NULL REFERENCES rooms(id),
            author   TEXT NOT NULL,
            kind     TEXT NOT NULL,
            body     TEXT NOT NULL DEFAULT '',
            meta     TEXT NOT NULL DEFAULT '{}',
            ts       INTEGER NOT NULL
        )""",
        "CREATE INDEX idx_room_transcripts_room_ts ON room_transcripts(room_id, ts)",
        "CREATE INDEX idx_room_transcripts_kind ON room_transcripts(room_id, kind, ts)",
    ]
    for stmt in statements:
        conn.execute(stmt)


def _backfill_projects(conn: sqlite3.Connection, seed) -> None:
    rows = [
        (entry["id"], name, entry.get("url"), entry["repo_path"], int(entry["created_at"] or 0))
        for name, entry in sorted(seed.projects.items())
    ]
    conn.executemany(
        "INSERT INTO projects (id, name, url, repo_path, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def _backfill_users(conn: sqlite3.Connection, seed) -> None:
    rows = [(uid, name) for name, uid in sorted(seed.users.items())]
    conn.executemany("INSERT INTO users (id, username) VALUES (?, ?)", rows)


def _backfill_agents(conn: sqlite3.Connection, seed) -> None:
    """Insert real agents + tombstones in two passes so the FK to projects
    is satisfied before parent_id lookups (currently all NULL anyway)."""
    rows: list[tuple] = []
    sources = [seed.agents, seed.tombstone_agents]
    for src in sources:
        for _key, e in sorted(src.items()):
            rows.append((
                e["id"], e["project_id"], e["scope"], e.get("parent_id"),
                e["status"], e["agent_cli"], e["branch"], e["worktree"],
                e["display_name"], int(e["is_vagrant"]),
                int(e["created_at"] or 0),
                e.get("retired_at"),
            ))
    conn.executemany(
        "INSERT OR IGNORE INTO agents "
        "(id, project_id, scope, parent_id, status, agent_cli, branch, "
        " worktree, display_name, is_vagrant, created_at, retired_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _backfill_agent_instances(conn: sqlite3.Connection, seed) -> None:
    rows = [
        (
            e["agent_id"], e.get("cli_session_id"), e.get("log_path"),
            int(e["started_at"] or 0), e.get("ended_at"),
            e.get("data") or "{}",
        )
        for e in seed.agent_instances
    ]
    conn.executemany(
        "INSERT INTO agent_instances "
        "(agent_id, cli_session_id, log_path, started_at, ended_at, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def _backfill_rooms(conn: sqlite3.Connection, seed) -> None:
    """Read from rooms_legacy_v36 and emit v37 rooms with owner_agent_id
    resolved via seed.room_owner_map."""
    legacy = conn.execute(
        "SELECT id, topic, status, created_at, closed_at FROM rooms_legacy_v36"
    ).fetchall()
    rows = []
    for r in legacy:
        owner = seed.room_owner_map.get(r["id"])
        if not owner:
            # Should never happen — preflight tombstones every room. Skip
            # rather than corrupt; FK check would fail anyway.
            continue
        rows.append((
            r["id"], owner, r["topic"] or "",
            r["status"] or "closed",
            _iso_to_ms_or_zero(r["created_at"]),
            _iso_to_ms_or_null(r["closed_at"]),
        ))
    conn.executemany(
        "INSERT INTO rooms (id, owner_agent_id, topic, status, created_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def _backfill_guest_list(conn: sqlite3.Connection, seed) -> None:
    """Translate room_participants → guest_list. Skip the owner's own
    participant row (a room owner isn't a guest of its own room)."""
    legacy = conn.execute(
        "SELECT room_id, kind, identifier, guest_name, subscriptions "
        "FROM room_participants WHERE left_at IS NULL"
    ).fetchall()
    rows = []
    for r in legacy:
        room_id = r["room_id"]
        kind = r["kind"]
        ident = r["identifier"] or ""
        if not ident:
            continue

        if kind == "scope":
            guest_kind = "agent"
            guest_ref = seed.ref_resolution.get(ident)
        elif kind == "user":
            guest_kind = "user"
            guest_ref = seed.ref_resolution.get(f"user:{ident}") or seed.users.get(ident)
        elif kind == "peer":
            guest_kind = "user"
            guest_ref = seed.ref_resolution.get(f"user:peer-{ident}") or seed.users.get(f"peer-{ident}")
        elif kind == "subscriber":
            # v36 WebSocket-attached human subscribers used identifiers of
            # the form ``ws:<conn>:user:<name>`` (and occasionally just a
            # bare ``user:<name>``). Peel out the user form so guest_list
            # carries persistent user membership; per-connection state is
            # rebuilt at attach time and doesn't belong on disk.
            user_part = ident
            if ":user:" in user_part:
                user_part = "user:" + user_part.split(":user:", 1)[1]
            elif user_part.startswith("ws:"):
                # Anonymous ws subscriber — no persistent identity, skip.
                continue
            if user_part.startswith("user:"):
                name = user_part[len("user:"):]
                guest_kind = "user"
                guest_ref = seed.users.get(name)
            else:
                continue
        else:
            continue
        if not guest_ref:
            continue
        # Skip the owner — they're not a guest of their own room.
        if seed.room_owner_map.get(room_id) == guest_ref:
            continue
        display = (r["guest_name"] or ident)[:200]
        subs = r["subscriptions"] or "{}"
        rows.append((room_id, guest_kind, guest_ref, display, subs))
    conn.executemany(
        "INSERT OR IGNORE INTO guest_list "
        "(room_id, guest_kind, guest_ref, display_name, subscriptions) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def _backfill_room_transcripts(conn: sqlite3.Connection, seed) -> None:
    """Translate room_posts → room_transcripts. Resolve author via
    ref_resolution; normalize kind='text' to 'message'."""
    legacy = conn.execute(
        "SELECT uuid, room_id, author, body, kind, ts, meta FROM room_posts"
    ).fetchall()
    # Use the set of v37 room ids so we don't FK-violate against rooms we
    # skipped during _backfill_rooms.
    valid_rooms = {
        r[0] for r in conn.execute("SELECT id FROM rooms").fetchall()
    }
    rows = []
    for r in legacy:
        if r["room_id"] not in valid_rooms:
            continue
        author = seed.ref_resolution.get(r["author"] or "")
        if not author:
            # Fall back to system sentinel rather than dropping the post.
            author = "system"
        kind = r["kind"] or "message"
        if kind == "text":
            kind = "message"
        ts = _iso_to_ms_or_zero(r["ts"])
        rows.append((
            r["uuid"], r["room_id"], author, kind,
            r["body"] or "", r["meta"] or "{}", ts,
        ))
    conn.executemany(
        "INSERT INTO room_transcripts (id, room_id, author, kind, body, meta, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _resolve_scope_agent_id(seed, project: str, scope: str) -> str | None:
    key = f"{project}/{scope}"
    entry = seed.agents.get(key) or seed.tombstone_agents.get(key)
    return entry["id"] if entry else None


def _rebuild_session_logs(conn: sqlite3.Connection, seed) -> None:
    conn.execute(
        """CREATE TABLE session_logs_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id      TEXT NOT NULL REFERENCES agents(id),
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
        )"""
    )
    legacy = conn.execute(
        "SELECT project, scope, file_path, git_commit, logged_at, summary, "
        "metadata, content, skill_path, outcome, deviations, suggestions, "
        "skill_version, resolved_at, resolution, title FROM session_logs"
    ).fetchall()
    rows = []
    for r in legacy:
        agent_id = _resolve_scope_agent_id(seed, r["project"], r["scope"])
        if not agent_id:
            continue
        rows.append((
            agent_id, _iso_to_ms_or_zero(r["logged_at"]),
            r["file_path"] or "", r["git_commit"], r["summary"] or "",
            r["metadata"], r["content"], r["skill_path"], r["outcome"],
            r["deviations"], r["suggestions"], r["skill_version"],
            _iso_to_ms_or_null(r["resolved_at"]), r["resolution"], r["title"],
        ))
    conn.executemany(
        "INSERT INTO session_logs_new "
        "(agent_id, created_at, file_path, git_commit, summary, metadata, "
        " content, skill_path, outcome, deviations, suggestions, "
        " skill_version, resolved_at, resolution, title) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.execute("DROP TABLE session_logs")
    conn.execute("ALTER TABLE session_logs_new RENAME TO session_logs")
    conn.execute(
        "CREATE INDEX idx_session_logs_agent "
        "ON session_logs(agent_id, created_at DESC)"
    )


def _rebuild_artifacts(conn: sqlite3.Connection, seed) -> None:
    conn.execute(
        """CREATE TABLE artifacts_new (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id       TEXT NOT NULL REFERENCES agents(id),
            name           TEXT NOT NULL DEFAULT '',
            artifact_type  TEXT NOT NULL DEFAULT '',
            path           TEXT NOT NULL DEFAULT '',
            description    TEXT,
            format         TEXT,
            tags           TEXT,
            status         TEXT NOT NULL DEFAULT 'current',
            created_at     INTEGER NOT NULL,
            updated_at     INTEGER NOT NULL
        )"""
    )
    legacy = conn.execute(
        "SELECT project, scope, name, artifact_type, path, description, "
        "format, tags, status, created_at, updated_at FROM artifacts"
    ).fetchall()
    rows = []
    for r in legacy:
        agent_id = _resolve_scope_agent_id(seed, r["project"], r["scope"])
        if not agent_id:
            continue
        rows.append((
            agent_id, r["name"] or "", r["artifact_type"] or "",
            r["path"] or "", r["description"], r["format"], r["tags"],
            r["status"] or "current",
            _iso_to_ms_or_zero(r["created_at"]),
            _iso_to_ms_or_zero(r["updated_at"]),
        ))
    conn.executemany(
        "INSERT INTO artifacts_new "
        "(agent_id, name, artifact_type, path, description, format, tags, "
        " status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.execute("DROP TABLE artifacts")
    conn.execute("ALTER TABLE artifacts_new RENAME TO artifacts")
    conn.execute(
        "CREATE INDEX idx_artifacts_agent "
        "ON artifacts(agent_id, created_at DESC)"
    )


def _rebuild_messages(conn: sqlite3.Connection, seed) -> None:
    conn.execute(
        """CREATE TABLE messages_new (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_id  TEXT NOT NULL,
            sender_id     TEXT NOT NULL,
            msg_type      TEXT NOT NULL DEFAULT '',
            subject       TEXT NOT NULL DEFAULT '',
            body          TEXT NOT NULL DEFAULT '',
            metadata      TEXT,
            status        TEXT NOT NULL DEFAULT 'unread',
            created_at    INTEGER NOT NULL,
            read_at       INTEGER
        )"""
    )
    legacy = conn.execute(
        "SELECT scope, sender, msg_type, subject, body, metadata, status, "
        "created_at, read_at FROM messages"
    ).fetchall()
    rows = []
    for r in legacy:
        recipient = seed.ref_resolution.get(r["scope"] or "") or "system"
        sender = seed.ref_resolution.get(r["sender"] or "") or "system"
        rows.append((
            recipient, sender,
            r["msg_type"] or "", r["subject"] or "", r["body"] or "",
            r["metadata"], r["status"] or "unread",
            _iso_to_ms_or_zero(r["created_at"]),
            _iso_to_ms_or_null(r["read_at"]),
        ))
    conn.executemany(
        "INSERT INTO messages_new "
        "(recipient_id, sender_id, msg_type, subject, body, metadata, "
        " status, created_at, read_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.execute("DROP TABLE messages")
    conn.execute("ALTER TABLE messages_new RENAME TO messages")
    conn.execute(
        "CREATE INDEX idx_messages_recipient_status "
        "ON messages(recipient_id, status, created_at DESC)"
    )


def _migrate(conn: sqlite3.Connection, current: int) -> None:
    """Apply migrations from current version up to SCHEMA_VERSION.

    Finalizes cr-sqlite before any DDL — _FinalizingConnection only finalizes
    on close, which is too late once we're rewriting CRR shadow tables.
    """
    try:
        conn.execute("SELECT crsql_finalize()")
    except sqlite3.OperationalError:
        pass

    while current < SCHEMA_VERSION:
        next_ver = current + 1
        # v37 is a full identity-layer rewrite — needs an explicit
        # BEGIN IMMEDIATE and Python-side backfill, so dispatch out of the
        # generic executescript path.
        if (current, next_ver) == (36, 37):
            _migrate_v37(conn)
            current = next_ver
            continue
        sql = MIGRATIONS.get((current, next_ver))
        if sql is None:
            raise RuntimeError(f"No migration path from v{current} to v{next_ver}")
        try:
            # SQLite's recommended table-rebuild pattern requires FKs off
            # during DROP/RENAME — see https://sqlite.org/lang_altertable.html
            # §7. Re-enable at the end, with foreign_key_check raising if the
            # rebuild left dangling references.
            conn.executescript(
                "PRAGMA foreign_keys=OFF;\n"
                + sql
                + f"\nUPDATE schema_version SET version = {next_ver};\n"
                + "PRAGMA foreign_key_check;\n"
                + "PRAGMA foreign_keys=ON;"
            )
        except sqlite3.OperationalError as exc:
            # Handle partial prior migration (e.g. column already added but
            # version not bumped due to crash). Bump version and continue.
            # Also handle the case where a migration tries to ALTER a table
            # the source DB never had — happens in contrived test fixtures
            # that hand-build a partial schema then ask init_db to migrate it.
            msg = str(exc)
            if "duplicate column name" in msg or "no such table" in msg:
                conn.execute(
                    "UPDATE schema_version SET version = ?", (next_ver,)
                )
                conn.commit()
            else:
                raise
        current = next_ver


def _ensure_self_row(conn: sqlite3.Connection) -> None:
    """Legacy stub. Peers table is being dropped in v38 alongside the
    federation retirement; no per-host bookkeeping required."""
    return


def _seed_sentinel_users(conn: sqlite3.Connection) -> None:
    """Idempotently seed the 'cli' + 'system' users on fresh installs."""
    rows = [(uid, name) for name, uid in SENTINEL_USER_IDS.items()]
    conn.executemany(
        "INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)", rows,
    )
    conn.commit()


def init_db(db_path: Path | None = None) -> None:
    """Create tables if they don't exist, running migrations as needed."""
    path = db_path or DB_PATH
    AWM_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    try:
        row = None
        try:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
        except sqlite3.OperationalError:
            pass  # table doesn't exist yet

        if row is None:
            # Fresh database — create everything at the v37 shape.
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value, updated_at) "
                "VALUES ('agent_cli', 'opencode', datetime('now'))"
            )
            _seed_sentinel_users(conn)
            conn.commit()
        else:
            current = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            if current < SCHEMA_VERSION:
                _migrate(conn, current)
            _seed_sentinel_users(conn)

        _ensure_self_row(conn)
    finally:
        conn.close()
