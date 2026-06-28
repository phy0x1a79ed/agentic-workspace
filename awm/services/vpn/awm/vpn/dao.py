"""vpn service data access — the ``vpn_sessions`` table on the service's OWN
SQLite DB (``AWM_DIR/services/vpn/vpn.db``).

Per the modular invariant there is no shared ``state.db``: this service owns its
table and stands it up via ``init_service_db`` at startup. Each row is one VPN
exit, keyed by ``profile`` — the PRIMARY KEY is what makes each profile a
**singleton** (at most one ``awm-vpn-<profile>`` exit at a time).

The DB is a cache of intent, not the source of truth: the live container state
comes from ``docker`` (see ``container.py``), which reconciles against this table
on every ``up``/``status``. A stale row (service crashed while a container ran,
or vice-versa) is corrected from ``docker ps``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "vpn"
SCHEMA_VERSION = 1

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS vpn_sessions (
    profile     TEXT NOT NULL PRIMARY KEY,
    container   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'down',
    exit_ip     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT ''
);
"""

_initialized = False


def init() -> None:
    """Idempotently create the vpn service's DB + ``vpn_sessions`` table."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class VpnDAO(BaseDAO):
    """CRUD over ``vpn_sessions`` (one row per VPN profile/exit)."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    def upsert(self, profile: str, *, container: str, status: str,
               exit_ip: str = "") -> dict:
        """Insert-or-update the single row for ``profile``; return it."""
        p = str(profile).strip()
        now = _now()
        self.execute(
            """\
            INSERT INTO vpn_sessions (profile, container, status, exit_ip, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile) DO UPDATE SET
                container = excluded.container,
                status    = excluded.status,
                exit_ip   = excluded.exit_ip,
                updated_at = excluded.updated_at
            """,
            (p, str(container or ""), str(status or "").strip(),
             str(exit_ip or ""), now, now),
        )
        return self.get(p)

    def get(self, profile: str) -> dict | None:
        if not profile:
            return None
        return self.query_one(
            "SELECT profile, container, status, exit_ip, created_at, updated_at "
            "FROM vpn_sessions WHERE profile = ?",
            (str(profile).strip(),),
        )

    def list_all(self) -> list[dict]:
        return self.query_all(
            "SELECT profile, container, status, exit_ip, created_at, updated_at "
            "FROM vpn_sessions ORDER BY profile"
        )

    def delete(self, profile: str) -> bool:
        """Return True if a row was deleted."""
        rows = self.execute(
            "DELETE FROM vpn_sessions WHERE profile = ?",
            (str(profile).strip(),),
        )
        return rows > 0
