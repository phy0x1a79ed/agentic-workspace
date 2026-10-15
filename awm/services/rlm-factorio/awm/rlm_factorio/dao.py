"""rlm-factorio service data access — the ``rlm_sessions`` and ``rlm_seats``
tables on the service's OWN SQLite DB (``AWM_DIR/services/rlm-factorio/rlm-factorio.db``).

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

``rlm_seats`` is one row per seat: a real Factorio client container joined to a
session's world as a real player. A seat binds three things that only this table
witnesses — an agent (``owner``), a container (``container_name``), and an
in-game player (``player_name`` / ``player_index``) — which is why seats are
persisted rather than enumerated from the world the way the browser realm
enumerates tabs from Chrome. ``player_name`` is assigned by us before the client
starts (the seat asserts it in its own ``player-data.json``), so the binding is
made by lookup rather than by guessing which new player appeared.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "rlm-factorio"
SCHEMA_VERSION = 2

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
CREATE TABLE IF NOT EXISTS rlm_seats (
    seat_id         TEXT NOT NULL PRIMARY KEY,
    session_id      TEXT NOT NULL,
    player_name     TEXT NOT NULL DEFAULT '',
    player_index    INTEGER NOT NULL DEFAULT 0,
    container_name  TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'joining',
    owner           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    last_seen_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rlm_seats_session ON rlm_seats (session_id);
"""

# v1 predates seats; an existing DB gets the table without losing its sessions.
MIGRATIONS = {
    (1, 2): """\
CREATE TABLE IF NOT EXISTS rlm_seats (
    seat_id         TEXT NOT NULL PRIMARY KEY,
    session_id      TEXT NOT NULL,
    player_name     TEXT NOT NULL DEFAULT '',
    player_index    INTEGER NOT NULL DEFAULT 0,
    container_name  TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'joining',
    owner           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    last_seen_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rlm_seats_session ON rlm_seats (session_id);
""",
}

# Column list shared by every read so the row shape is stable across methods.
_COLS = (
    "session_id, game, status, container_name, compose_project, "
    "control_port, game_port, rcon_port, current_world, created_at, updated_at"
)

_initialized = False


def init() -> None:
    """Idempotently create the service's DB + ``rlm_sessions`` + ``rlm_seats``."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION,
                        migrations=MIGRATIONS)
        _initialized = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Fields a caller may patch through ``set_runtime`` (a whitelist guards the SQL).
_PATCHABLE = {
    "status", "container_name", "compose_project",
    "control_port", "game_port", "rcon_port", "current_world",
}

_SEAT_COLS = (
    "seat_id, session_id, player_name, player_index, container_name, "
    "status, owner, created_at, updated_at, last_seen_at"
)

_SEAT_PATCHABLE = {"status", "player_index", "container_name", "owner"}


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


    # -- seats --------------------------------------------------------------

    def create_seat(
        self,
        session_id: str,
        *,
        player_name: str = "",
        container_name: str = "",
        owner: str = "",
    ) -> dict:
        """Mint a seat row (status 'joining') and return it.

        The row exists before the client does, so there is no window in which a
        connected player has no row to bind to. ``player_name`` defaults to the
        seat id: the in-game name IS the seat's identity, which is what lets the
        service resolve ``game.players[name]`` instead of guessing which new
        player appeared.
        """
        seat_id = f"seat-{uuid.uuid4().hex[:8]}"
        player_name = player_name or seat_id
        now = _now()
        self.execute(
            """\
            INSERT INTO rlm_seats (
                seat_id, session_id, player_name, player_index, container_name,
                status, owner, created_at, updated_at, last_seen_at
            ) VALUES (?, ?, ?, 0, ?, 'joining', ?, ?, ?, ?)
            """,
            (seat_id, str(session_id).strip(), player_name, container_name,
             owner, now, now, now),
        )
        return self.get_seat(seat_id)

    def get_seat(self, seat_id: str) -> dict | None:
        if not seat_id:
            return None
        return self.query_one(
            f"SELECT {_SEAT_COLS} FROM rlm_seats WHERE seat_id = ?",
            (str(seat_id).strip(),),
        )

    def list_seats(self, session_id: str | None = None) -> list[dict]:
        if session_id:
            return self.query_all(
                f"SELECT {_SEAT_COLS} FROM rlm_seats WHERE session_id = ? "
                "ORDER BY created_at",
                (str(session_id).strip(),),
            )
        return self.query_all(
            f"SELECT {_SEAT_COLS} FROM rlm_seats ORDER BY created_at")

    def live_seats(self, session_id: str | None = None) -> list[dict]:
        """Seats not yet released -- the ones that should own a container."""
        return [r for r in self.list_seats(session_id)
                if r["status"] not in ("stopped", "error")]

    def set_seat(self, seat_id: str, **fields) -> dict | None:
        """Patch whitelisted seat columns; also refreshes ``updated_at``."""
        sid = str(seat_id).strip()
        bad = set(fields) - _SEAT_PATCHABLE
        if bad:
            raise ValueError(f"non-patchable seat fields: {sorted(bad)}")
        if not fields:
            return self.get_seat(sid)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(
            f"UPDATE rlm_seats SET {cols}, updated_at = ? WHERE seat_id = ?",
            list(fields.values()) + [_now(), sid],
        )
        return self.get_seat(sid)

    def touch_seat(self, seat_id: str) -> None:
        """Refresh a seat's last-seen stamp. Any verb naming the seat is proof
        its owner is alive, which is what holds the seat against the reaper."""
        self.execute(
            "UPDATE rlm_seats SET last_seen_at = ? WHERE seat_id = ?",
            (_now(), str(seat_id).strip()),
        )

    def delete_seat(self, seat_id: str) -> bool:
        rows = self.execute(
            "DELETE FROM rlm_seats WHERE seat_id = ?", (str(seat_id).strip(),))
        return rows > 0

    def delete_session(self, session_id: str) -> bool:
        """Return True if a row was deleted."""
        rows = self.execute(
            "DELETE FROM rlm_sessions WHERE session_id = ?",
            (str(session_id).strip(),),
        )
        return rows > 0
