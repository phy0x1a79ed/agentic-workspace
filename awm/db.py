"""SQLite setup + migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from awm.config import DB_PATH, AWM_DIR

SCHEMA_VERSION = 35

SCHEMA_SQL = """\
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

CREATE TABLE IF NOT EXISTS shared_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    worktree_path TEXT NOT NULL,
    branch TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
);

-- session_logs: UNIQUE(project, scope, logged_at, agent_id) is gone because
-- cr-sqlite forbids non-PK unique constraints on CRRs. Dedup is now the
-- writer's responsibility at the application layer.
CREATE TABLE IF NOT EXISTS session_logs (
    uuid TEXT NOT NULL PRIMARY KEY,
    legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    git_commit TEXT,
    logged_at TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    agent_id TEXT NOT NULL DEFAULT 'unknown',
    metadata TEXT,
    content TEXT,
    skill_path TEXT,
    outcome TEXT,
    deviations TEXT,
    suggestions TEXT,
    skill_version TEXT,
    resolved_at TEXT,
    resolution TEXT,
    title TEXT
);

CREATE INDEX IF NOT EXISTS idx_session_logs_project_scope
    ON session_logs(project, scope);
CREATE INDEX IF NOT EXISTS idx_session_logs_legacy
    ON session_logs(legacy_id, origin_peer);

-- scopes: cr-sqlite forbids UNIQUE indices beyond the PK, so the
-- previously-partial "one active row per (project, scope)" constraint
-- moves into the application layer (services/scopes.py:create_scope still
-- explicitly rejects a duplicate-active insert).
CREATE TABLE IF NOT EXISTS scopes (
    uuid TEXT NOT NULL PRIMARY KEY,
    legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    branch TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '',
    repo_path TEXT,
    session INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_scopes_status ON scopes(status);
CREATE INDEX IF NOT EXISTS idx_scopes_project ON scopes(project);
CREATE INDEX IF NOT EXISTS idx_scopes_legacy ON scopes(legacy_id, origin_peer);

-- artifacts: UNIQUE(path) drops because cr-sqlite forbids non-PK unique
-- constraints on CRRs. Application-layer upsert in services/artifacts.py
-- already SELECTs by path before INSERT or UPDATE, so behavior is
-- preserved within a single peer. Cross-peer: two peers may now register
-- the same path independently (each row keyed by its own origin_peer +
-- legacy_id) — phase 6 federation handles the content-fetch routing.
CREATE TABLE IF NOT EXISTS artifacts (
    uuid TEXT NOT NULL PRIMARY KEY,
    legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    artifact_type TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    description TEXT,
    format TEXT,
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'current',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project);
CREATE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path);
CREATE INDEX IF NOT EXISTS idx_artifacts_legacy ON artifacts(legacy_id, origin_peer);

CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_type, source_id)
);

CREATE TABLE IF NOT EXISTS messages (
    uuid TEXT NOT NULL PRIMARY KEY,
    legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    msg_type TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    metadata TEXT,
    status TEXT NOT NULL DEFAULT 'unread',
    created_at TEXT NOT NULL DEFAULT '',
    read_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages(scope);
CREATE INDEX IF NOT EXISTS idx_messages_scope_status ON messages(scope, status);
CREATE INDEX IF NOT EXISTS idx_messages_legacy ON messages(legacy_id, origin_peer);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    scope TEXT NOT NULL,
    pid INTEGER NOT NULL,
    status TEXT NOT NULL,
    agent_cli TEXT NOT NULL,
    started_at TEXT NOT NULL,
    exited_at TEXT,
    exit_code INTEGER,
    log_path TEXT NOT NULL,
    claude_session_id TEXT,
    intent TEXT NOT NULL DEFAULT 'live'  -- 'live' | 'stopped' | 'killed' | 'compacted'
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_scope ON agent_sessions(project, scope);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_sessions_active_unique
    ON agent_sessions(project, scope)
    WHERE status IN ('starting', 'running', 'stopping', 'orphaned');

-- agent_events: structured per-session transcript. One row per stream-json
-- event from the CLI (direction='out') and per framed stdin write
-- (direction='in'). Local-only, NOT a CRR — transcripts are large and
-- per-host. Underpins auto-resume replay and synthetic /compact.
CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    project TEXT NOT NULL,
    scope TEXT NOT NULL,
    agent_cli TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    direction TEXT NOT NULL,             -- 'in' | 'out'
    event_type TEXT NOT NULL,            -- 'init'|'assistant'|'user'|'tool_use'|'tool_result'|'result'|'partial'|'raw'
    body TEXT NOT NULL,                  -- JSON: full parsed event, or {"raw": "..."}
    claude_session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_events_session_seq ON agent_events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_agent_events_scope_ts ON agent_events(project, scope, ts);
CREATE INDEX IF NOT EXISTS idx_agent_events_claude_sid ON agent_events(claude_session_id);

-- agent_resume_queue: scheduling table for the post-restart resume driver.
-- One row per prior session that the reconciler wants to resurrect.
-- session_id is the prior incarnation's id; primer_text is populated by
-- /compact orphan recovery.
CREATE TABLE IF NOT EXISTS agent_resume_queue (
    session_id INTEGER PRIMARY KEY,
    scheduled_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    prior_exited_at TEXT,
    primer_text TEXT
);

-- NOT NULL columns have explicit DEFAULTs: cr-sqlite (phase 4) refuses to
-- mark a table as CRR if any non-PK NOT NULL column lacks a default.
CREATE TABLE IF NOT EXISTS peers (
    peer_id TEXT NOT NULL PRIMARY KEY,
    ssh_alias TEXT NOT NULL DEFAULT '',
    remote_port INTEGER NOT NULL DEFAULT 7820,
    friendly_name TEXT,
    last_seen TEXT,
    added_at TEXT NOT NULL DEFAULT '',
    endpoints TEXT,                              -- JSON: ordered list of {kind, ...}
    tls_fingerprint TEXT,                        -- SHA-256 of remote cert, when pinning
    peer_priority INTEGER NOT NULL DEFAULT 100,  -- lower = higher leadership precedence; 0 wins
    origin_peer TEXT
);

CREATE TABLE IF NOT EXISTS rooms (
    id TEXT NOT NULL PRIMARY KEY,
    host_peer_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    closed_at TEXT,
    topic TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    close_on_exit INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT
);

CREATE TABLE IF NOT EXISTS room_participants (
    room_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    identifier TEXT NOT NULL DEFAULT '',
    joined_at TEXT NOT NULL DEFAULT '',
    left_at TEXT,
    origin_peer TEXT,
    PRIMARY KEY (room_id, kind, identifier)
);

-- room_posts: cr-sqlite forbids UNIQUE constraints beyond the PK on CRR
-- tables, so the per-peer (legacy_id, origin_peer) uniqueness is enforced
-- in services/rooms.py via next_legacy_id() inside the write transaction
-- rather than by a DB constraint. The non-unique index supports the
-- read path.
CREATE TABLE IF NOT EXISTS room_posts (
    uuid TEXT NOT NULL PRIMARY KEY,
    legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '',
    room_id TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'text',
    ts TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_room_posts_room_ts ON room_posts(room_id, ts);
CREATE INDEX IF NOT EXISTS idx_room_posts_legacy ON room_posts(legacy_id, origin_peer);
CREATE INDEX IF NOT EXISTS idx_rooms_status ON rooms(status);
CREATE INDEX IF NOT EXISTS idx_room_participants_scope
    ON room_participants(kind, identifier) WHERE left_at IS NULL;

CREATE TABLE IF NOT EXISTS discord_operators (
    discord_user_id TEXT NOT NULL PRIMARY KEY,
    awm_user TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL DEFAULT '',
    origin_peer TEXT
);

-- Local-only, NOT replicated: tracks the cr-sqlite db_version each remote
-- peer last sent us, so the pull loop can ask "anything new since N".
CREATE TABLE IF NOT EXISTS peer_sync_state (
    peer_id TEXT PRIMARY KEY,
    last_db_version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""

# Migrations keyed by (from_version, to_version)
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
    (7, 8): """\
ALTER TABLE tasks ADD COLUMN session INTEGER NOT NULL DEFAULT 1;
""",
    (8, 9): """\
ALTER TABLE session_logs ADD COLUMN content TEXT;
""",
    (9, 10): """\
-- Rename tasks → scopes
ALTER TABLE tasks RENAME TO scopes;
ALTER TABLE scopes RENAME COLUMN task TO scope;

-- Experiences table
CREATE TABLE IF NOT EXISTS experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_path TEXT,
    skill_version TEXT,
    project TEXT NOT NULL,
    scope TEXT NOT NULL,
    agent_id TEXT DEFAULT 'unknown',
    outcome TEXT,
    summary TEXT NOT NULL,
    deviations TEXT,
    suggestions TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_experiences_skill ON experiences(skill_path);
CREATE INDEX IF NOT EXISTS idx_experiences_project ON experiences(project);

-- Artifacts table
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    description TEXT,
    format TEXT,
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'current',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path);
""",
    (10, 11): """\
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_type, source_id)
);
""",
    (5, 6): """\
-- Migrate task worktree paths: main/{project}/{task} → main/{project}/tasks/{task}
UPDATE tasks SET worktree = REPLACE(worktree,
    '/' || project || '/' || task,
    '/' || project || '/tasks/' || task)
WHERE worktree LIKE '%/main/%' AND worktree NOT LIKE '%/tasks/%';

-- Migrate session file paths
UPDATE session_logs SET file_path = REPLACE(file_path,
    'main/' || project || '/' || task || '/',
    'main/' || project || '/tasks/' || task || '/')
WHERE file_path LIKE 'main/%' AND file_path NOT LIKE '%/tasks/%';
""",
    (11, 12): """\
-- Rename session_logs.task → scope
ALTER TABLE session_logs RENAME COLUMN task TO scope;
""",
    (12, 13): """\
ALTER TABLE session_logs ADD COLUMN skill_path TEXT;
""",
    (13, 14): """\
-- Fold experiences into session_logs
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
    (15, 16): """\
ALTER TABLE session_logs ADD COLUMN title TEXT;
""",
    (16, 17): """\
CREATE TABLE IF NOT EXISTS agent_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    scope TEXT NOT NULL,
    pid INTEGER NOT NULL,
    status TEXT NOT NULL,
    agent_cli TEXT NOT NULL,
    started_at TEXT NOT NULL,
    exited_at TEXT,
    exit_code INTEGER,
    log_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_status ON agent_sessions(status);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_scope ON agent_sessions(project, scope);
""",
    (17, 18): """\
CREATE TABLE IF NOT EXISTS peers (
    peer_id TEXT PRIMARY KEY,
    base_url TEXT NOT NULL,
    token_path TEXT NOT NULL,
    friendly_name TEXT,
    last_seen TEXT,
    added_at TEXT NOT NULL
);
""",
    (21, 22): """\
-- Rooms can auto-close when their last agent exits — flag persisted on
-- the room itself so agent_instances can act on _waiter_loop without needing
-- in-memory orchestration state.
ALTER TABLE rooms ADD COLUMN close_on_exit INTEGER NOT NULL DEFAULT 0;
""",
    (22, 23): """\
-- Rooms now support an 'archived' status alongside 'active' and 'closed'.
-- No schema change required (status column is TEXT with no CHECK constraint);
-- this migration is a marker so older code knows the domain has grown.
SELECT 1;
""",
    (23, 24): """\
-- Persist claude's resume id so re-invite after the agent process dies
-- (and is reaped from agent_instances._by_scope) can still pass --resume
-- to the new claude subprocess.
ALTER TABLE agent_sessions ADD COLUMN claude_session_id TEXT;
""",
    (20, 21): """\
-- One running agent process per (project, scope). Partial unique index
-- enforces it for the active statuses; exited/killed rows are unconstrained
-- so the same scope can re-spawn after termination.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_sessions_active_unique
    ON agent_sessions(project, scope)
    WHERE status IN ('starting', 'running', 'stopping', 'orphaned');
""",
    (19, 20): """\
-- Rooms primitive: multi-participant conversations with scope-keyed agents
-- and human/peer subscribers. Tables: rooms, room_participants, room_posts.
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    host_peer_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    topic TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS room_participants (
    room_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    identifier TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    left_at TEXT,
    PRIMARY KEY (room_id, kind, identifier)
);
CREATE TABLE IF NOT EXISTS room_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    ts TEXT NOT NULL,
    FOREIGN KEY (room_id) REFERENCES rooms(id)
);
CREATE INDEX IF NOT EXISTS idx_room_posts_room_ts ON room_posts(room_id, ts);
CREATE INDEX IF NOT EXISTS idx_rooms_status ON rooms(status);
CREATE INDEX IF NOT EXISTS idx_room_participants_scope
    ON room_participants(kind, identifier) WHERE left_at IS NULL;
""",
    (18, 19): """\
-- Peer registry rebuild: SSH-tunneled transport replaces direct-LAN.
-- base_url / token_path are dropped (URL is derived from the tunnel;
-- token is loaded from a canonical file). Existing rows are not
-- migrated — the operator re-runs `awm peer add` with --ssh-alias.
CREATE TABLE peers_new (
    peer_id TEXT PRIMARY KEY,
    ssh_alias TEXT NOT NULL,
    remote_port INTEGER NOT NULL DEFAULT 7820,
    friendly_name TEXT,
    last_seen TEXT,
    added_at TEXT NOT NULL
);
DROP TABLE peers;
ALTER TABLE peers_new RENAME TO peers;
""",
    (24, 25): """\
-- Direct endpoints + TLS pinning for the peer-client.
-- ``endpoints`` is a JSON-encoded ordered list of ``{kind, ...}`` entries
-- (``direct=https://10.x.y.z:7820``, ``ssh=alias:port``). When NULL, the
-- peer_client falls back to the legacy ssh_alias/remote_port pair so
-- existing peer rows continue to work without a re-add.
-- ``tls_fingerprint`` is the SHA-256 of the remote daemon's cert, used
-- by the peer_client for optional pinning. NULL = no pinning, fall back
-- to TLS verify=False (acceptable for zerotier overlays).
ALTER TABLE peers ADD COLUMN endpoints TEXT;
ALTER TABLE peers ADD COLUMN tls_fingerprint TEXT;
""",

    (25, 26): """\
-- v26: discord operators table. Whitelists which Discord users may run
-- the /login slash command and what awm_user identity they map to. The
-- bot consults this table on every /login invocation; absence means
-- "not authorized."
CREATE TABLE IF NOT EXISTS discord_operators (
    discord_user_id TEXT PRIMARY KEY,
    awm_user TEXT NOT NULL,
    added_at TEXT NOT NULL
);
""",

    (26, 27): """\
-- v27: peer_priority for application-layer leader election (phase 3 of
-- the decentralization arc). Lower integer = higher precedence; 0 wins.
-- Default 100 lets pre-existing peers stay non-leader until an operator
-- sets explicit priorities. Self-row is upserted on init_db() when the
-- local PEER_FILE identity is known.
ALTER TABLE peers ADD COLUMN peer_priority INTEGER NOT NULL DEFAULT 100;
""",

    (27, 28): """\
-- v28: Phase 4 (cr-sqlite replication) prep — already-TEXT-PK tables.
-- cr-sqlite requires PRIMARY KEY columns to be NOT NULL; SQLite's
-- "TEXT PRIMARY KEY" alone is nullable. Rebuild rooms, peers, and
-- discord_operators with NOT NULL PKs and an origin_peer audit column.
-- room_participants gets origin_peer too (compound PK already NOT NULL).
--
-- The five INTEGER-PK tables (room_posts, scopes, session_logs, messages,
-- artifacts) need a separate INTEGER→UUID migration per the phase-4 plan,
-- landed one per PR so the ~150 service-callsite updates stay reviewable.
-- After v28 the four already-TEXT-PK tables replicate; the others stay
-- local-only until their migrations land.

CREATE TABLE rooms_new (
    id TEXT NOT NULL PRIMARY KEY,
    host_peer_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    closed_at TEXT,
    topic TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    close_on_exit INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT
);
INSERT INTO rooms_new (id, host_peer_id, created_at, closed_at, topic, status, close_on_exit, origin_peer)
    SELECT id, host_peer_id, created_at, closed_at, topic, status, close_on_exit, host_peer_id
    FROM rooms WHERE id IS NOT NULL;
DROP TABLE rooms;
ALTER TABLE rooms_new RENAME TO rooms;
CREATE INDEX IF NOT EXISTS idx_rooms_status ON rooms(status);

-- room_participants needs DEFAULTs on its compound PK columns + new origin_peer.
CREATE TABLE room_participants_new (
    room_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    identifier TEXT NOT NULL DEFAULT '',
    joined_at TEXT NOT NULL DEFAULT '',
    left_at TEXT,
    origin_peer TEXT,
    PRIMARY KEY (room_id, kind, identifier)
);
INSERT INTO room_participants_new (room_id, kind, identifier, joined_at, left_at, origin_peer)
    SELECT room_id, kind, identifier, joined_at, left_at, NULL FROM room_participants;
DROP TABLE room_participants;
ALTER TABLE room_participants_new RENAME TO room_participants;
CREATE INDEX IF NOT EXISTS idx_room_participants_scope
    ON room_participants(kind, identifier) WHERE left_at IS NULL;

CREATE TABLE peers_new (
    peer_id TEXT NOT NULL PRIMARY KEY,
    ssh_alias TEXT NOT NULL DEFAULT '',
    remote_port INTEGER NOT NULL DEFAULT 7820,
    friendly_name TEXT,
    last_seen TEXT,
    added_at TEXT NOT NULL DEFAULT '',
    endpoints TEXT,
    tls_fingerprint TEXT,
    peer_priority INTEGER NOT NULL DEFAULT 100,
    origin_peer TEXT
);
INSERT INTO peers_new
    SELECT peer_id, ssh_alias, remote_port, friendly_name, last_seen, added_at,
           endpoints, tls_fingerprint, peer_priority, peer_id
    FROM peers WHERE peer_id IS NOT NULL;
DROP TABLE peers;
ALTER TABLE peers_new RENAME TO peers;

CREATE TABLE discord_operators_new (
    discord_user_id TEXT NOT NULL PRIMARY KEY,
    awm_user TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL DEFAULT '',
    origin_peer TEXT
);
INSERT INTO discord_operators_new
    SELECT discord_user_id, awm_user, added_at, NULL
    FROM discord_operators WHERE discord_user_id IS NOT NULL;
DROP TABLE discord_operators;
ALTER TABLE discord_operators_new RENAME TO discord_operators;
""",

    (28, 29): """\
-- v29: peer_sync_state — local-only bookkeeping for the cr-sqlite pull loop.
-- One row per remote peer, recording the last db_version we have received
-- from them. NOT replicated (each peer's view of "who sent me what" is its
-- own state).
CREATE TABLE IF NOT EXISTS peer_sync_state (
    peer_id TEXT PRIMARY KEY,
    last_db_version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);
""",

    (29, 30): """\
-- v30: room_posts INTEGER→UUID, the first of five per-table conversions
-- (room_posts → scopes → session_logs → messages → artifacts) needed
-- for cr-sqlite replication. AUTOINCREMENT INTEGER PKs collide across
-- peers; cr-sqlite requires non-autoincrement unique PKs, so we move to
-- uuid TEXT PK + per-peer monotonic legacy_id INTEGER + origin_peer.
--
-- Post-v30 row identity:
--   - ``uuid``        — cr-sqlite PK, internal only.
--   - ``legacy_id``   — public id, minted by the owning peer in its own
--                       local sequence. Same id may appear on different
--                       peers (different origin_peer disambiguates).
--   - ``origin_peer`` — the peer that minted ``legacy_id`` for this row.
--
-- The FK to rooms(id) is dropped: replicated posts can arrive before
-- their parent room replicates, and cr-sqlite doesn't enforce FKs across
-- CRRs anyway.
--
-- Backfill: existing posts get fresh uuids; legacy_id ← id (preserves
-- public identity); origin_peer ← (the post's room's host_peer_id), so
-- the row's "owning peer" matches the room that hosts it. Pre-v28 rooms
-- without host_peer_id fall back to '' (empty string) — those rows are
-- effectively local-only until reconciled.

CREATE TABLE room_posts_new (
    uuid TEXT NOT NULL PRIMARY KEY,
    legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '',
    room_id TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'text',
    ts TEXT NOT NULL DEFAULT ''
);
INSERT INTO room_posts_new (uuid, legacy_id, origin_peer, room_id, author, body, kind, ts)
    SELECT
        lower(hex(randomblob(16))),
        room_posts.id,
        COALESCE((SELECT host_peer_id FROM rooms WHERE rooms.id = room_posts.room_id), ''),
        room_posts.room_id,
        room_posts.author,
        room_posts.body,
        room_posts.kind,
        room_posts.ts
    FROM room_posts;
DROP TABLE room_posts;
ALTER TABLE room_posts_new RENAME TO room_posts;
CREATE INDEX IF NOT EXISTS idx_room_posts_room_ts ON room_posts(room_id, ts);
CREATE INDEX IF NOT EXISTS idx_room_posts_legacy ON room_posts(legacy_id, origin_peer);
""",

    (31, 32): """\
-- v32: session_logs INTEGER→UUID. Third per-table CRR migration.
-- Public id is still the integer legacy_id (returned via SessionLogEntry.id);
-- uuid is internal-only. UNIQUE(project, scope, logged_at, agent_id) drops
-- because cr-sqlite rejects non-PK unique constraints on CRRs; dedup is
-- now the writer's responsibility at the application layer.

CREATE TABLE session_logs_new (
    uuid TEXT NOT NULL PRIMARY KEY,
    legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    git_commit TEXT,
    logged_at TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    agent_id TEXT NOT NULL DEFAULT 'unknown',
    metadata TEXT,
    content TEXT,
    skill_path TEXT,
    outcome TEXT,
    deviations TEXT,
    suggestions TEXT,
    skill_version TEXT,
    resolved_at TEXT,
    resolution TEXT,
    title TEXT
);
INSERT INTO session_logs_new (uuid, legacy_id, origin_peer, project, scope,
                              file_path, git_commit, logged_at, summary,
                              agent_id, metadata, content, skill_path,
                              outcome, deviations, suggestions, skill_version,
                              resolved_at, resolution, title)
    SELECT
        lower(hex(randomblob(16))),
        id,
        COALESCE((SELECT peer_id FROM peers WHERE ssh_alias = 'self' LIMIT 1), ''),
        project, scope, COALESCE(file_path, ''), git_commit,
        logged_at, summary, COALESCE(agent_id, 'unknown'), metadata, content,
        skill_path, outcome, deviations, suggestions, skill_version,
        resolved_at, resolution, title
    FROM session_logs;
DROP TABLE session_logs;
ALTER TABLE session_logs_new RENAME TO session_logs;
CREATE INDEX IF NOT EXISTS idx_session_logs_project_scope
    ON session_logs(project, scope);
CREATE INDEX IF NOT EXISTS idx_session_logs_legacy
    ON session_logs(legacy_id, origin_peer);
""",

    (32, 33): """\
-- v33: messages INTEGER→UUID. Fourth per-table CRR migration.
-- Public id stays INTEGER (returned as MessageInfo.id = legacy_id). The
-- recipient peer mints the legacy_id at receive time — for cross-peer
-- sends the existing /peer/inbox handler recurses into send_message()
-- locally on the recipient, so the per-peer monotonic sequence runs on
-- the owning peer just like rooms/sessions.

CREATE TABLE messages_new (
    uuid TEXT NOT NULL PRIMARY KEY,
    legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    msg_type TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    metadata TEXT,
    status TEXT NOT NULL DEFAULT 'unread',
    created_at TEXT NOT NULL DEFAULT '',
    read_at TEXT
);
INSERT INTO messages_new (uuid, legacy_id, origin_peer, scope, sender,
                          msg_type, subject, body, metadata, status,
                          created_at, read_at)
    SELECT
        lower(hex(randomblob(16))),
        id,
        COALESCE((SELECT peer_id FROM peers WHERE ssh_alias = 'self' LIMIT 1), ''),
        scope, sender, msg_type, subject, body, metadata, status,
        created_at, read_at
    FROM messages;
DROP TABLE messages;
ALTER TABLE messages_new RENAME TO messages;
CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages(scope);
CREATE INDEX IF NOT EXISTS idx_messages_scope_status ON messages(scope, status);
CREATE INDEX IF NOT EXISTS idx_messages_legacy ON messages(legacy_id, origin_peer);
""",

    (33, 34): """\
-- v34: artifacts INTEGER→UUID. Final per-table CRR migration.
-- Drops UNIQUE(path) (cr-sqlite forbids non-PK unique indices on CRRs).
-- The path-keyed upsert in services/artifacts.py:register_artifact moves
-- the constraint into the app layer (it already SELECTs by path before
-- choosing INSERT vs UPDATE). Adds origin_peer so phase 6 content-fetch
-- federation can decide whether to read locally or proxy to the owning
-- peer.
DROP INDEX IF EXISTS idx_artifacts_path;

CREATE TABLE artifacts_new (
    uuid TEXT NOT NULL PRIMARY KEY,
    legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    artifact_type TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    description TEXT,
    format TEXT,
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'current',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
INSERT INTO artifacts_new (uuid, legacy_id, origin_peer, project, scope, name,
                           artifact_type, path, description, format, tags,
                           status, created_at, updated_at)
    SELECT
        lower(hex(randomblob(16))),
        id,
        COALESCE((SELECT peer_id FROM peers WHERE ssh_alias = 'self' LIMIT 1), ''),
        project, scope, name, artifact_type, path, description, format,
        tags, status, created_at, updated_at
    FROM artifacts;
DROP TABLE artifacts;
ALTER TABLE artifacts_new RENAME TO artifacts;
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project);
CREATE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(path);
CREATE INDEX IF NOT EXISTS idx_artifacts_legacy ON artifacts(legacy_id, origin_peer);
""",

    (30, 31): """\
-- v31: scopes INTEGER→UUID. Second of five per-table CRR migrations.
-- Public identifier is (project, scope) — uuid is purely internal for
-- cr-sqlite. legacy_id is kept for parity with other CRR tables but is
-- not currently exposed to callers. Drop the partial unique index since
-- cr-sqlite forbids UNIQUE constraints beyond the PK on CRRs; the
-- application-level check in create_scope() still rejects duplicate
-- active rows.
DROP INDEX IF EXISTS idx_scopes_active_unique;

CREATE TABLE scopes_new (
    uuid TEXT NOT NULL PRIMARY KEY,
    legacy_id INTEGER NOT NULL DEFAULT 0,
    origin_peer TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    branch TEXT NOT NULL DEFAULT '',
    worktree TEXT NOT NULL DEFAULT '',
    repo_path TEXT,
    session INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
INSERT INTO scopes_new (uuid, legacy_id, origin_peer, project, scope, status,
                        branch, worktree, repo_path, session, created_at, updated_at)
    SELECT
        lower(hex(randomblob(16))),
        id,
        COALESCE((SELECT peer_id FROM peers WHERE ssh_alias = 'self' LIMIT 1), ''),
        project, scope, status, branch, worktree, repo_path,
        session, created_at, updated_at
    FROM scopes;
DROP TABLE scopes;
ALTER TABLE scopes_new RENAME TO scopes;
CREATE INDEX IF NOT EXISTS idx_scopes_status ON scopes(status);
CREATE INDEX IF NOT EXISTS idx_scopes_project ON scopes(project);
CREATE INDEX IF NOT EXISTS idx_scopes_legacy ON scopes(legacy_id, origin_peer);
""",

    (34, 35): """\
-- v35: structured transcript + auto-resume foundations.
--   - agent_sessions.intent: tracks why a session exited so the reconciler
--     knows whether to auto-resume (intent='live' + dead PID = resume
--     candidate; 'stopped'/'killed'/'compacted' = leave alone).
--   - agent_events: structured per-session transcript. One row per
--     stream-json event from the CLI (direction='out') or per framed stdin
--     write (direction='in'). Local-only, not a CRR.
--   - agent_resume_queue: scheduling table the resume driver reads after
--     reconcile_on_startup. session_id is the PRIOR incarnation's id;
--     primer_text supports /compact orphan recovery.
ALTER TABLE agent_sessions ADD COLUMN intent TEXT NOT NULL DEFAULT 'live';

CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    project TEXT NOT NULL,
    scope TEXT NOT NULL,
    agent_cli TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    direction TEXT NOT NULL,
    event_type TEXT NOT NULL,
    body TEXT NOT NULL,
    claude_session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_events_session_seq ON agent_events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_agent_events_scope_ts ON agent_events(project, scope, ts);
CREATE INDEX IF NOT EXISTS idx_agent_events_claude_sid ON agent_events(claude_session_id);

CREATE TABLE IF NOT EXISTS agent_resume_queue (
    session_id INTEGER PRIMARY KEY,
    scheduled_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    prior_exited_at TEXT,
    primer_text TEXT
);
""",
}


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Return a new SQLite connection with WAL mode enabled.

    Loads the cr-sqlite extension when available (vendored at
    ``awm/_native/crsqlite.so``). Missing/unloadable extension is non-fatal
    — the daemon falls back to single-peer mode and replication is a no-op.
    """
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path), timeout=30, factory=_FinalizingConnection)
    # cr-sqlite must be loaded before any DDL/DML for CRR tables; load
    # eagerly on every connection so opt-in replication "just works".
    try:
        from awm.services.replication import schema as _repl_schema
        _repl_schema.load_extension(conn)
    except Exception:
        # Replication is an enhancement; never block core DB access on it.
        pass
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


class _FinalizingConnection(sqlite3.Connection):
    """sqlite3.Connection subclass that runs SELECT crsql_finalize() before
    close. cr-sqlite leaks per-connection state (state.db + state.db-wal
    FDs) unless finalized; this guarantees every get_connection() caller
    releases those handles regardless of whether they call finalize()
    explicitly. crsql_finalize is idempotent, so the few sites that also
    call finalize() themselves (db.init_db, replication.sync) are safe.
    """

    def close(self):
        try:
            from awm.services.replication import schema as _repl_schema
            _repl_schema.finalize(self)
        except Exception:
            pass
        super().close()


def _migrate(conn: sqlite3.Connection, current: int) -> None:
    """Apply migrations from current version up to SCHEMA_VERSION."""
    while current < SCHEMA_VERSION:
        next_ver = current + 1
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
    """Upsert a peers-table row for the local peer so leader election can
    read its own priority from the same source as remote peers.

    No-op when PEER_FILE is missing (the daemon can run pre-federation).
    ``ssh_alias`` is the sentinel ``"self"``, ``remote_port`` is
    ``EXPOSED_PORT``. ``peer_priority`` seeds from ``AWM_PEER_PRIORITY``
    on first insert; subsequent calls preserve any operator-set value
    (so `awm peer set-priority self <n>` survives restarts).
    """
    from awm import config
    import json as _json
    import os as _os

    try:
        from awm.services.network.peers import get_local_identity, LocalIdentityError
        ident = get_local_identity()
    except Exception:
        return
    if ident is None:
        return
    peer_id = ident.get("peer_id")
    if not peer_id:
        return

    seed_priority = int(_os.environ.get("AWM_PEER_PRIORITY", "100"))
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    endpoints_json = _json.dumps([
        {"kind": "direct", "url": f"https://{config.EXPOSED_HOST}:{config.EXPOSED_PORT}"}
    ])
    try:
        conn.execute(
            """
            INSERT INTO peers (peer_id, ssh_alias, remote_port, friendly_name,
                               added_at, endpoints, tls_fingerprint, peer_priority)
            VALUES (?, 'self', ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(peer_id) DO UPDATE SET
                ssh_alias = 'self',
                remote_port = excluded.remote_port,
                friendly_name = COALESCE(peers.friendly_name, excluded.friendly_name),
                endpoints = excluded.endpoints
            """,
            (peer_id, config.EXPOSED_PORT, peer_id, now, endpoints_json, seed_priority),
        )
        conn.commit()
    except sqlite3.OperationalError:
        # peers table missing (partial test fixtures) — non-fatal.
        pass


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
            # Fresh database — create everything
            conn.executescript(SCHEMA_SQL)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value, updated_at) VALUES ('agent_cli', 'opencode', datetime('now'))"
            )
            conn.commit()
        else:
            current = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            if current < SCHEMA_VERSION:
                _migrate(conn, current)

        _ensure_self_row(conn)
        # CRR registration runs after migrations land all the v32 tables.
        # Idempotent — safe to call on every init.
        try:
            from awm.services.replication import schema as _repl_schema
            _repl_schema.register_all_crrs(conn)
        except Exception:
            pass
    finally:
        try:
            from awm.services.replication import schema as _repl_schema
            _repl_schema.finalize(conn)
        except Exception:
            pass
        conn.close()
