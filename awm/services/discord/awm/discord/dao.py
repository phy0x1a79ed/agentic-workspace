"""Discord service data access — the ``discord_operators`` table on the
discord service's OWN SQLite DB (``AWM_DIR/services/discord/discord.db``).

Per the modular invariant there is no shared ``state.db``: this service owns
exactly one table and stands it up via ``init_service_db`` at startup. A row
whitelists a Discord user to run ``/login`` and maps them to the ``awm_user``
identity that ``X-Awm-As`` reports.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "discord"
SCHEMA_VERSION = 1

# This service's own tables — only one. ``origin_peer`` from the legacy v37
# schema is dropped (federation retired).
SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS discord_operators (
    discord_user_id TEXT NOT NULL PRIMARY KEY,
    awm_user        TEXT NOT NULL DEFAULT '',
    added_at        TEXT NOT NULL DEFAULT ''
);
"""

_initialized = False


def init() -> None:
    """Idempotently create the discord service's DB + ``discord_operators``."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


class DiscordDAO(BaseDAO):
    """CRUD over ``discord_operators`` (one row per whitelisted Discord user)."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    def add_operator(self, discord_user_id: str, awm_user: str) -> dict:
        """Idempotent: re-adding overwrites the ``awm_user`` mapping."""
        if not discord_user_id or not str(discord_user_id).strip():
            raise ValueError("discord_user_id is required")
        if not awm_user or not str(awm_user).strip():
            raise ValueError("awm_user is required")
        did = str(discord_user_id).strip()
        user = str(awm_user).strip()
        now = datetime.now(timezone.utc).isoformat()
        self.execute(
            """\
            INSERT INTO discord_operators (discord_user_id, awm_user, added_at)
            VALUES (?, ?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                awm_user = excluded.awm_user
            """,
            (did, user, now),
        )
        return {"discord_user_id": did, "awm_user": user, "added_at": now}

    def remove_operator(self, discord_user_id: str) -> bool:
        """Return True if a row was deleted."""
        rows = self.execute(
            "DELETE FROM discord_operators WHERE discord_user_id = ?",
            (str(discord_user_id).strip(),),
        )
        return rows > 0

    def list_operators(self) -> list[dict]:
        return self.query_all(
            "SELECT discord_user_id, awm_user, added_at "
            "FROM discord_operators ORDER BY added_at"
        )

    def lookup(self, discord_user_id: str) -> str | None:
        """Return the ``awm_user`` for a Discord ID, or None if not whitelisted."""
        if not discord_user_id:
            return None
        row = self.query_one(
            "SELECT awm_user FROM discord_operators WHERE discord_user_id = ?",
            (str(discord_user_id).strip(),),
        )
        return row["awm_user"] if row else None
