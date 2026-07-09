"""rlm-factorio service data access — the ``rlm_sessions`` table on the service's
OWN SQLite DB (``AWM_DIR/services/rlm-factorio/rlm-factorio.db``).

Per the modular invariant there is no shared ``state.db``: this service owns its
tables and stands them up via ``init_service_db`` at startup. Each row is one
realm session — a Factorio appliance (Docker container + supervisor) bound to a
single game, addressed by the container/ports it was brought up on.

The row carries the runtime coordinates the handlers need to reach the live
appliance: ``container_name`` / ``compose_project`` (so ``release`` tears down
exactly what ``acquire`` brought up and a respawn can re-adopt it), the
``control_port`` (supervisor HTTP), ``game_port`` (Factorio UDP), and
``rcon_port`` (always 0 — RCON is container-internal, never published).
``current_world`` mirrors the
supervisor's notion of which named save the live ``_active`` was derived from.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "rlm-factorio"
SCHEMA_VERSION = 1

# status: acquiring -> ready -> (paused) -> stopped ; error on any failed bring-up.
SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS rlm_sessions (
    session_id      TEXT NOT NULL PRIMARY KEY,
    game            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'acquiring',
    container_name  TEXT NOT NULL DEFAULT '',
    compose_project TEXT NOT NULL DEFAULT '',
    control_port    INTEGER NOT NULL DEFAULT 0,
    game_port       INTEGER NOT NULL DEFAULT 0,
    rcon_port       INTEGER NOT NULL DEFAULT 0,
    current_world   TEXT,
    created_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT ''
);
"""

# Column list shared by every read so the row shape is stable across methods.
_COLS = (
    "session_id, game, status, container_name, compose_project, "
    "control_port, game_port, rcon_port, current_world, created_at, updated_at"
)

_initialized = False


def init() -> None:
    """Idempotently create the rlm-factorio service's DB + ``rlm_sessions``."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Fields a caller may patch through ``set_runtime`` (a whitelist guards the SQL).
_PATCHABLE = {
    "status", "container_name", "compose_project",
    "control_port", "game_port", "rcon_port", "current_world",
}


class FactorioDAO(BaseDAO):
    """CRUD over ``rlm_sessions`` (one row per acquired Factorio appliance)."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    def create_session(
        self,
        game: str,
        *,
        container_name: str = "",
        compose_project: str = "",
        control_port: int = 0,
        game_port: int = 0,
        rcon_port: int = 0,
    ) -> dict:
        """Mint a new session row (status 'acquiring') and return it."""
        session_id = f"rlm-factorio-{uuid.uuid4().hex[:12]}"
        now = _now()
        self.execute(
            """\
            INSERT INTO rlm_sessions (
                session_id, game, status, container_name, compose_project,
                control_port, game_port, rcon_port, created_at, updated_at
            ) VALUES (?, ?, 'acquiring', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, str(game or "").strip(), container_name,
                compose_project, int(control_port), int(game_port),
                int(rcon_port), now, now,
            ),
        )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict | None:
        if not session_id:
            return None
        return self.query_one(
            f"SELECT {_COLS} FROM rlm_sessions WHERE session_id = ?",
            (str(session_id).strip(),),
        )

    def list_sessions(self) -> list[dict]:
        return self.query_all(
            f"SELECT {_COLS} FROM rlm_sessions ORDER BY created_at"
        )

    def live_sessions(self) -> list[dict]:
        """Sessions not yet released/stopped — the pool of candidate appliances."""
        return self.query_all(
            f"SELECT {_COLS} FROM rlm_sessions "
            "WHERE status NOT IN ('stopped', 'error') ORDER BY created_at"
        )

    def set_status(self, session_id: str, status: str) -> dict | None:
        return self.set_runtime(session_id, status=status)

    def set_runtime(self, session_id: str, **fields) -> dict | None:
        """Patch any subset of the runtime columns on a session; return the row.

        Only whitelisted columns (``_PATCHABLE``) are accepted — anything else
        raises, so a typo can't silently no-op or inject SQL.
        """
        sid = str(session_id).strip()
        bad = set(fields) - _PATCHABLE
        if bad:
            raise ValueError(f"non-patchable fields: {sorted(bad)}")
        if not fields:
            return self.get_session(sid)
        cols = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [_now(), sid]
        self.execute(
            f"UPDATE rlm_sessions SET {cols}, updated_at = ? WHERE session_id = ?",
            params,
        )
        return self.get_session(sid)

    def delete_session(self, session_id: str) -> bool:
        """Return True if a row was deleted."""
        rows = self.execute(
            "DELETE FROM rlm_sessions WHERE session_id = ?",
            (str(session_id).strip(),),
        )
        return rows > 0
