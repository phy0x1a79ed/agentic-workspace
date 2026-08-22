"""Social service data access — the social service's OWN SQLite DB
(``AWM_DIR/services/social/social.db``).

Two tables, all metadata only — **never a token** (tokens live solely in
``social.toml``):

- ``social_accounts``  — one row per configured identity (name, platform, kind).
- ``social_operators`` — platform-scoped allowlist: a ``(platform, user id)``
  maps to an ``awm_user``. Generalises the old ``discord_operators`` by adding
  the ``platform`` column.

There is deliberately **no message store**: the external platforms (Slack,
Gmail, Teams, Discord) are the source of truth, so fetch/search/download query
them live rather than mirroring messages here. A schema-v2 migration drops the
former ``social_messages`` table from any existing DB.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "social"
SCHEMA_VERSION = 2

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS social_accounts (
    name         TEXT NOT NULL PRIMARY KEY,
    platform     TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'bot',
    display_name TEXT NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1,
    added_at     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS social_operators (
    platform         TEXT NOT NULL,
    platform_user_id TEXT NOT NULL,
    awm_user         TEXT NOT NULL DEFAULT '',
    added_at         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (platform, platform_user_id)
);
"""

# v1 → v2: the local message mirror is gone (query platforms live). Drop the
# orphan table + its indexes from any existing prod/dev DB. Idempotent.
MIGRATIONS = {
    (1, 2): """\
DROP INDEX IF EXISTS ux_social_messages_dedupe;
DROP INDEX IF EXISTS ix_social_messages_channel;
DROP TABLE IF EXISTS social_messages;
""",
}

_initialized = False


def init() -> None:
    """Idempotently create the social service's DB + its two tables."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION,
                        migrations=MIGRATIONS)
        _initialized = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SocialDAO(BaseDAO):
    """CRUD over the social service's two tables (accounts + operators)."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    # -- accounts ----------------------------------------------------------

    def upsert_account(
        self,
        name: str,
        platform: str,
        *,
        kind: str = "bot",
        display_name: str = "",
        enabled: bool = True,
    ) -> dict:
        """Record an account's metadata (NEVER its token). Idempotent."""
        if not name or not str(name).strip():
            raise ValueError("account name is required")
        name = str(name).strip()
        now = _now()
        self.execute(
            """\
            INSERT INTO social_accounts
                (name, platform, kind, display_name, enabled, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                platform     = excluded.platform,
                kind         = excluded.kind,
                display_name = excluded.display_name,
                enabled      = excluded.enabled
            """,
            (name, platform, kind, display_name, 1 if enabled else 0, now),
        )
        return {"name": name, "platform": platform, "kind": kind}

    def list_accounts(self) -> list[dict]:
        rows = self.query_all(
            "SELECT name, platform, kind, display_name, enabled, added_at "
            "FROM social_accounts ORDER BY name"
        )
        for r in rows:
            r["enabled"] = bool(r["enabled"])
        return rows

    # -- operators (platform-scoped) ---------------------------------------

    def add_operator(
        self, platform: str, platform_user_id: str, awm_user: str
    ) -> dict:
        """Idempotent: re-adding overwrites the ``awm_user`` mapping."""
        if not platform or not str(platform).strip():
            raise ValueError("platform is required")
        if not platform_user_id or not str(platform_user_id).strip():
            raise ValueError("platform_user_id is required")
        if not awm_user or not str(awm_user).strip():
            raise ValueError("awm_user is required")
        platform = str(platform).strip()
        uid = str(platform_user_id).strip()
        user = str(awm_user).strip()
        now = _now()
        self.execute(
            """\
            INSERT INTO social_operators
                (platform, platform_user_id, awm_user, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(platform, platform_user_id) DO UPDATE SET
                awm_user = excluded.awm_user
            """,
            (platform, uid, user, now),
        )
        return {
            "platform": platform, "platform_user_id": uid,
            "awm_user": user, "added_at": now,
        }

    def remove_operator(self, platform: str, platform_user_id: str) -> bool:
        """Return True if a row was deleted."""
        rows = self.execute(
            "DELETE FROM social_operators "
            "WHERE platform = ? AND platform_user_id = ?",
            (str(platform).strip(), str(platform_user_id).strip()),
        )
        return rows > 0

    def list_operators(self, platform: str | None = None) -> list[dict]:
        if platform:
            return self.query_all(
                "SELECT platform, platform_user_id, awm_user, added_at "
                "FROM social_operators WHERE platform = ? ORDER BY added_at",
                (str(platform).strip(),),
            )
        return self.query_all(
            "SELECT platform, platform_user_id, awm_user, added_at "
            "FROM social_operators ORDER BY platform, added_at"
        )

    def lookup(self, platform: str, platform_user_id: str) -> str | None:
        """Return the ``awm_user`` for a (platform, user id), or None."""
        if not platform or not platform_user_id:
            return None
        row = self.query_one(
            "SELECT awm_user FROM social_operators "
            "WHERE platform = ? AND platform_user_id = ?",
            (str(platform).strip(), str(platform_user_id).strip()),
        )
        return row["awm_user"] if row else None

