"""The tables of `awm.reporting.work`, apart so the store can create them without importing the ledger."""

WORK_SCHEMA = """
CREATE TABLE IF NOT EXISTS work (
    task_id      TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    node         TEXT NOT NULL DEFAULT '',
    created      REAL NOT NULL,
    updated      REAL NOT NULL,
    folded_into  TEXT REFERENCES work (task_id)
);
CREATE TABLE IF NOT EXISTS work_sessions (
    task_id  TEXT NOT NULL REFERENCES work (task_id),
    session  TEXT NOT NULL,
    role     TEXT NOT NULL,
    node     TEXT NOT NULL DEFAULT '',
    joined   REAL NOT NULL,
    ended    REAL,
    name     TEXT,
    rc_url   TEXT,
    PRIMARY KEY (task_id, session)
);
CREATE INDEX IF NOT EXISTS work_sessions_by_session ON work_sessions (session);
-- The hard edges of the lineage graph (sessions.md R13, R16): who started a
-- session, or which session it was before a clear. One parent per session.
CREATE TABLE IF NOT EXISTS session_parents (
    session  TEXT PRIMARY KEY,
    parent   TEXT NOT NULL,
    how      TEXT NOT NULL,
    node     TEXT NOT NULL DEFAULT '',
    at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS session_parents_by_parent ON session_parents (parent);
"""
