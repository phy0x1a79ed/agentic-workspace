"""Events service data access — the ``event_schedules`` table on the events
service's OWN SQLite DB (``AWM_DIR/services/events/events.db``).

Per the modular invariant there is no shared ``state.db``: this service owns its
tables and stands them up via ``init_service_db`` at startup. One row per
scheduled game: the cron/interval spec that drives its ``schedule.tick``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

from awm.events import cron

SERVICE = "events"
SCHEMA_VERSION = 1

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS event_schedules (
    game       TEXT NOT NULL PRIMARY KEY,
    cron       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""

_initialized = False


def init() -> None:
    """Idempotently create the events service's DB + ``event_schedules``."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


class EventsDAO(BaseDAO):
    """CRUD over ``event_schedules`` (one row per scheduled game)."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    def schedule(self, game: str, cron_spec: str) -> dict:
        """Upsert a schedule. Idempotent: re-scheduling overwrites the cron.

        The cron is validated before it is stored so a bad spec is rejected at
        the call site (surfaced to the caller as the handler's error) rather
        than silently breaking the scheduler loop.
        """
        if not game or not str(game).strip():
            raise ValueError("game is required")
        if not cron_spec or not str(cron_spec).strip():
            raise ValueError("cron is required")
        g = str(game).strip()
        spec = str(cron_spec).strip()
        cron.validate(spec)  # raises CronError (a ValueError) on a bad spec
        now = datetime.now(timezone.utc).isoformat()
        self.execute(
            """\
            INSERT INTO event_schedules (game, cron, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(game) DO UPDATE SET
                cron = excluded.cron,
                updated_at = excluded.updated_at
            """,
            (g, spec, now, now),
        )
        return {"game": g, "cron": spec, "updated_at": now}

    def unschedule(self, game: str) -> bool:
        """Return True if a schedule row was deleted."""
        rows = self.execute(
            "DELETE FROM event_schedules WHERE game = ?",
            (str(game).strip(),),
        )
        return rows > 0

    def list_schedules(self) -> list[dict]:
        return self.query_all(
            "SELECT game, cron, created_at, updated_at "
            "FROM event_schedules ORDER BY game"
        )
