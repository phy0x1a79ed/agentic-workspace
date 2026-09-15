"""rlm-chess data access: the start settings of each session, so `reset` can start a rematch."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "rlm-chess"
SCHEMA_VERSION = 1

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS chess_sessions (
    session_id   TEXT NOT NULL PRIMARY KEY,
    mode         TEXT NOT NULL,
    start_params TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

_initialized = False


def init() -> None:
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


class ChessDAO(BaseDAO):
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    def record(self, session_id: str, mode: str, start_params: dict[str, Any]) -> None:
        self.execute(
            "INSERT OR REPLACE INTO chess_sessions (session_id, mode, start_params, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, mode, json.dumps(start_params), datetime.now(timezone.utc).isoformat()),
        )

    def start_params(self, session_id: str) -> dict[str, Any] | None:
        row = self.query_one(
            "SELECT start_params FROM chess_sessions WHERE session_id = ?", (session_id,))
        return None if row is None else json.loads(row["start_params"])
