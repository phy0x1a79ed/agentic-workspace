"""Notifications / fleet service schema.

Two tables on the service's own SQLite DB
(``AWM_DIR/services/notifications/notifications.db``):

- ``sessions`` — one row per observed agent session (either harness). This is
  the **fleet roster**: every Claude / OpenCode session the harness hooks have
  reported, machine-wide. ``state`` tracks the session's live posture, grounded
  purely in hook signals (no NLP): ``working`` (mid-turn / user just responded),
  ``idle`` (turn ended), ``needs-you`` (permission / waiting-for-input
  Notification), ``error``, ``ended``. ``tmux_session`` (best-effort, captured
  by the hook when ``$TMUX`` is set) is the attach handle the browser terminal
  uses; a row without it is *not attachable*. Cumulative per-category token
  counters + ``context_tokens`` (the live context size) drive the roster's cost
  / context columns; EOOT (Opus-output-equivalent) is computed on read from a
  configurable rate table, so they store raw tokens, not a precomputed cost.
- ``notifications`` — one row per attention item. De-dupe invariant: at most
  one *unresolved* row per ``(session_id, kind)`` — repeat events refresh the
  open row instead of stacking duplicates. ``notify_at`` is the desktop-push
  gate the page honours: ``created_at`` for needs-you/error (push immediately),
  ``created_at + grace`` for idle (an actively-driven session resolves the item
  before the grace elapses, so it never pings the user every turn).

``kind`` / ``state`` vocabulary (v2, hook-grounded):
``needs-you`` (Notification), ``idle`` (Stop/turn_end), ``error`` (error).
The old ``question`` kind (an NLP guess on the last message) is retired and
migrated to ``needs-you``.
"""

from __future__ import annotations

NOTIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    harness           TEXT NOT NULL,
    cwd               TEXT,
    project           TEXT,
    title             TEXT,
    state             TEXT NOT NULL DEFAULT 'working',
    tmux_session      TEXT,
    model             TEXT,
    tok_in            INTEGER NOT NULL DEFAULT 0,
    tok_out           INTEGER NOT NULL DEFAULT 0,
    tok_cache_write_5m INTEGER NOT NULL DEFAULT 0,
    tok_cache_write_1h INTEGER NOT NULL DEFAULT 0,
    tok_cache_read    INTEGER NOT NULL DEFAULT 0,
    context_tokens    INTEGER NOT NULL DEFAULT 0,
    transcript_offset INTEGER NOT NULL DEFAULT 0,
    first_seen        REAL NOT NULL,
    last_seen         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- needs-you | idle | error
    title       TEXT NOT NULL,
    detail      TEXT,
    snippet     TEXT,
    created_at  REAL NOT NULL,
    notify_at   REAL NOT NULL,
    seen_at     REAL,
    resolved_at REAL,
    resolved_by TEXT                    -- user_response | session_end | page | expired | cleared
);

CREATE INDEX IF NOT EXISTS idx_notifications_session
    ON notifications(session_id);
CREATE INDEX IF NOT EXISTS idx_notifications_open
    ON notifications(session_id, kind) WHERE resolved_at IS NULL;
"""

# v1 → v2: add the fleet columns to an existing sessions table and reground the
# retired ``question`` vocabulary onto ``needs-you`` (both the session state and
# any open/closed attention items). ``no such table`` / ``duplicate column``
# are swallowed by the migrator, so a fresh-but-crashed DB self-heals.
MIGRATION_1_2 = """
ALTER TABLE sessions ADD COLUMN tmux_session TEXT;
ALTER TABLE sessions ADD COLUMN model TEXT;
ALTER TABLE sessions ADD COLUMN tok_in INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN tok_out INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN tok_cache_write_5m INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN tok_cache_write_1h INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN tok_cache_read INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN context_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN transcript_offset INTEGER NOT NULL DEFAULT 0;
UPDATE sessions SET state = 'needs-you' WHERE state = 'question';
UPDATE notifications SET kind = 'needs-you' WHERE kind = 'question';
"""

MIGRATIONS: dict[tuple[int, int], str] = {
    (1, 2): MIGRATION_1_2,
}
