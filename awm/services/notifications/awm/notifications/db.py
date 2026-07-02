"""Notifications service schema.

Two tables on the service's own SQLite DB
(``AWM_DIR/services/notifications/notifications.db``):

- ``sessions`` — one row per observed agent session (either harness). ``state``
  tracks the session's live posture: ``working`` (user responded / mid-turn),
  ``idle`` / ``question`` / ``error`` (needs attention), ``ended``.
- ``notifications`` — one row per attention item. De-dupe invariant: at most
  one *unresolved* row per ``(session_id, kind)`` — repeat events refresh the
  open row instead of stacking duplicates. ``notify_at`` is the desktop-push
  gate the page honours: ``created_at`` for question/error (push immediately),
  ``created_at + grace`` for idle (an actively-driven session resolves the item
  before the grace elapses, so it never pings the user every turn).
"""

from __future__ import annotations

NOTIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    harness     TEXT NOT NULL,
    cwd         TEXT,
    project     TEXT,
    title       TEXT,
    state       TEXT NOT NULL DEFAULT 'working',
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- question | idle | error
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
