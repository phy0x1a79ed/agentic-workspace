"""The tables of the task store reporting kept before operations took the task ids (sessions.md R58). The store's 9->10 step still creates them."""

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
"""
