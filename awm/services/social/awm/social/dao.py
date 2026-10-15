"""Social service data access — the social service's OWN SQLite DB
(``AWM_DIR/services/social/social.db``).

Never a token (tokens live solely in ``social.toml``):

- ``social_accounts``  — one row per configured identity (name, platform, kind).
- ``social_operators`` — platform-scoped allowlist: a ``(platform, user id)``
  maps to an ``awm_user``. Generalises the old ``discord_operators`` by adding
  the ``platform`` column.
- ``social_history`` / ``social_sync`` — the synced message history of a bot
  account (every chat the bot is a member of) and its per-chat sync cursor.
- ``social_mounts`` — which Claude session holds which seat (a chat a bot
  account is in). Written only on the node that owns the account.

Accounts that act as a person (Slack/Teams via mira, Gmail) keep no local copy:
the platform is the source of truth and fetch/search query it live. A bot's
history is kept because the bot is the only reader of its own chats and the
store is what a listener catches up from after missing pushes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "social"
SCHEMA_VERSION = 4

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

CREATE TABLE IF NOT EXISTS social_history (
    account     TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    thread_id   TEXT NOT NULL DEFAULT '',
    sender_id   TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    attachments TEXT NOT NULL DEFAULT '[]',
    ts          TEXT NOT NULL DEFAULT '',
    own         INTEGER NOT NULL DEFAULT 0,
    edited      INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account, channel_id, message_id)
);
CREATE INDEX IF NOT EXISTS ix_social_history_ts ON social_history (account, ts);

CREATE TABLE IF NOT EXISTS social_sync (
    account      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    channel_name TEXT NOT NULL DEFAULT '',
    synced_ts    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account, channel_id)
);

CREATE TABLE IF NOT EXISTS social_mounts (
    account      TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    channel_name TEXT NOT NULL DEFAULT '',
    node         TEXT NOT NULL,
    job          TEXT NOT NULL,
    session_name TEXT NOT NULL DEFAULT '',
    mounted_at   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (account, channel_id)
);
"""

_MOUNTS_AT = SCHEMA_SQL.index("CREATE TABLE IF NOT EXISTS social_mounts")
HISTORY_DDL = SCHEMA_SQL[SCHEMA_SQL.index("CREATE TABLE IF NOT EXISTS social_history"):_MOUNTS_AT]
MOUNTS_DDL = SCHEMA_SQL[_MOUNTS_AT:]

# v1 → v2: the local message mirror is gone (query platforms live). Drop the
# orphan table + its indexes from any existing prod/dev DB. Idempotent.
MIGRATIONS = {
    (1, 2): """\
DROP INDEX IF EXISTS ux_social_messages_dedupe;
DROP INDEX IF EXISTS ix_social_messages_channel;
DROP TABLE IF EXISTS social_messages;
""",
    (2, 3): HISTORY_DDL,
    (3, 4): MOUNTS_DDL,
}

_initialized = False


def init() -> None:
    """Idempotently create the social service's DB + its tables."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION,
                        migrations=MIGRATIONS)
        _initialized = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SocialDAO(BaseDAO):
    """CRUD over the social service's tables."""

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


    # -- bot history ---------------------------------------------------------

    def upsert_message(self, account: str, row: dict, *, edited: bool = False) -> None:
        """Insert or replace one message. An edit keeps the deleted flag clear and
        marks the row edited; a redelivery of the same message is a no-op rewrite."""
        self.execute(
            "INSERT INTO social_history (account, channel_id, message_id, "
            "thread_id, sender_id, sender_name, text, attachments, ts, own, edited) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (account, channel_id, message_id) DO UPDATE SET "
            "thread_id = excluded.thread_id, sender_id = excluded.sender_id, "
            "sender_name = excluded.sender_name, text = excluded.text, "
            "attachments = excluded.attachments, own = excluded.own, "
            "edited = MAX(social_history.edited, excluded.edited)",
            (account, row["channel_id"], row["message_id"], row.get("thread_id", ""),
             row.get("sender_id", ""), row.get("sender_name", ""), row.get("text", ""),
             json.dumps(row.get("attachments", [])), row.get("ts", ""),
             int(bool(row.get("own"))), int(edited)),
        )

    def mark_deleted(self, account: str, channel_id: str, message_id: str) -> None:
        self.execute(
            "UPDATE social_history SET deleted = 1 "
            "WHERE account = ? AND channel_id = ? AND message_id = ?",
            (account, channel_id, message_id),
        )

    def history(self, account: str, *, channel_id: str | None = None,
                since: str | None = None, limit: int = 100,
                include_deleted: bool = False) -> list[dict]:
        """Stored messages oldest→newest. With ``since``, the ``limit`` messages
        right after it; without, the newest ``limit``."""
        where, params = ["account = ?"], [account]
        if channel_id:
            where.append("channel_id = ?")
            params.append(channel_id)
        if since:
            where.append("CAST(ts AS REAL) > CAST(? AS REAL)")
            params.append(since)
        if not include_deleted:
            where.append("deleted = 0")
        order = "ASC" if since else "DESC"
        rows = self.query_all(
            "SELECT channel_id, message_id, thread_id, sender_id, sender_name, "
            "text, attachments, ts, own, edited, deleted FROM social_history "
            f"WHERE {' AND '.join(where)} ORDER BY CAST(ts AS REAL) {order} LIMIT ?",
            (*params, int(limit)),
        )
        if not since:
            rows.reverse()
        for r in rows:
            r["attachments"] = json.loads(r["attachments"] or "[]")
            for flag in ("own", "edited", "deleted"):
                r[flag] = bool(r[flag])
        return rows

    def active_threads(self, account: str, channel_id: str,
                       since_ts: str) -> list[str]:
        """Threads in a chat with a stored message newer than ``since_ts``."""
        rows = self.query_all(
            "SELECT DISTINCT thread_id FROM social_history WHERE account = ? "
            "AND channel_id = ? AND thread_id != '' "
            "AND CAST(ts AS REAL) > CAST(? AS REAL)",
            (account, channel_id, since_ts),
        )
        return [r["thread_id"] for r in rows]

    def synced_ts(self, account: str, channel_id: str) -> str | None:
        row = self.query_one(
            "SELECT synced_ts FROM social_sync WHERE account = ? AND channel_id = ?",
            (account, channel_id),
        )
        return row["synced_ts"] if row else None

    def set_synced(self, account: str, channel_id: str, ts: str,
                   channel_name: str = "") -> None:
        """Advance a chat's cursor to ``ts`` (never backwards)."""
        self.execute(
            "INSERT INTO social_sync (account, channel_id, channel_name, synced_ts) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (account, channel_id) DO UPDATE SET "
            "channel_name = CASE WHEN excluded.channel_name != '' "
            "THEN excluded.channel_name ELSE social_sync.channel_name END, "
            "synced_ts = CASE WHEN CAST(excluded.synced_ts AS REAL) > "
            "CAST(social_sync.synced_ts AS REAL) OR social_sync.synced_ts = '' "
            "THEN excluded.synced_ts ELSE social_sync.synced_ts END",
            (account, channel_id, channel_name, ts),
        )

    def synced_channels(self, account: str) -> list[dict]:
        return self.query_all(
            "SELECT channel_id, channel_name, synced_ts FROM social_sync "
            "WHERE account = ? ORDER BY channel_name", (account,),
        )


    # -- seats ---------------------------------------------------------------

    _MOUNT_COLS = ("account, channel_id, channel_name, node, job, session_name, "
                   "mounted_at")

    def holder(self, account: str, channel_id: str) -> dict | None:
        return self.query_one(
            f"SELECT {self._MOUNT_COLS} FROM social_mounts "
            "WHERE account = ? AND channel_id = ?", (account, channel_id))

    def mounts(self, account: str | None = None) -> list[dict]:
        if account:
            return self.query_all(
                f"SELECT {self._MOUNT_COLS} FROM social_mounts WHERE account = ? "
                "ORDER BY channel_name", (account,))
        return self.query_all(
            f"SELECT {self._MOUNT_COLS} FROM social_mounts ORDER BY account, channel_name")

    def mount(self, account: str, channel_id: str, channel_name: str,
              holder: dict) -> dict:
        """Make ``holder`` (node, job, name) the seat's holder, replacing any other."""
        self.execute(
            "INSERT INTO social_mounts (account, channel_id, channel_name, node, job, "
            "session_name, mounted_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (account, channel_id) DO UPDATE SET "
            "channel_name = excluded.channel_name, node = excluded.node, "
            "job = excluded.job, session_name = excluded.session_name, "
            "mounted_at = excluded.mounted_at",
            (account, channel_id, channel_name, holder["node"], holder["job"],
             holder.get("name") or "", _now()),
        )
        return self.holder(account, channel_id)

    def unmount(self, account: str, channel_id: str) -> bool:
        return self.execute(
            "DELETE FROM social_mounts WHERE account = ? AND channel_id = ?",
            (account, channel_id)) > 0

    def release_holder(self, node: str, job: str) -> list[dict]:
        """Free every seat ``(node, job)`` holds; returns the freed rows."""
        rows = self.query_all(
            f"SELECT {self._MOUNT_COLS} FROM social_mounts WHERE node = ? AND job = ?",
            (node, job))
        self.execute("DELETE FROM social_mounts WHERE node = ? AND job = ?", (node, job))
        return rows
