"""rlm-browser service data access — the ``rlm_sessions`` table on the service's
OWN SQLite DB (``AWM_DIR/services/rlm-browser/rlm-browser.db``).

Per the modular invariant there is no shared ``state.db``: this service owns its
tables and stands them up via ``init_service_db`` at startup. Each row is one
realm session — a (future) Chrome/CDP browser bound to a single game.

Skeleton note: the table is real so stub state survives across calls, but the
browser behind a session is not launched yet. ``acquire`` mints a session row;
the act/perceive verbs are placeholders until real CDP lands.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "rlm-browser"
SCHEMA_VERSION = 1

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS rlm_sessions (
    session_id  TEXT NOT NULL PRIMARY KEY,
    game        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT ''
);
"""

_initialized = False


def init() -> None:
    """Idempotently create the rlm-browser service's DB + ``rlm_sessions``."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BrowserDAO(BaseDAO):
    """CRUD over ``rlm_sessions`` (one row per acquired realm session)."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    def create_session(self, game: str) -> dict:
        """Mint a new session row and return it. ``game`` is optional metadata."""
        session_id = f"rlm-browser-{uuid.uuid4().hex[:12]}"
        now = _now()
        self.execute(
            """\
            INSERT INTO rlm_sessions (session_id, game, status, created_at, updated_at)
            VALUES (?, ?, 'active', ?, ?)
            """,
            (session_id, str(game or "").strip(), now, now),
        )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict | None:
        if not session_id:
            return None
        return self.query_one(
            "SELECT session_id, game, status, created_at, updated_at "
            "FROM rlm_sessions WHERE session_id = ?",
            (str(session_id).strip(),),
        )

    def list_sessions(self) -> list[dict]:
        return self.query_all(
            "SELECT session_id, game, status, created_at, updated_at "
            "FROM rlm_sessions ORDER BY created_at"
        )

    def set_status(self, session_id: str, status: str) -> dict | None:
        """Update a session's status (e.g. 'active' → 'reset'); return the row."""
        sid = str(session_id).strip()
        self.execute(
            "UPDATE rlm_sessions SET status = ?, updated_at = ? WHERE session_id = ?",
            (str(status).strip(), _now(), sid),
        )
        return self.get_session(sid)

    def delete_session(self, session_id: str) -> bool:
        """Return True if a row was deleted."""
        rows = self.execute(
            "DELETE FROM rlm_sessions WHERE session_id = ?",
            (str(session_id).strip(),),
        )
        return rows > 0
