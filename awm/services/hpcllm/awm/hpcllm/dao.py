from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db

SERVICE = "hpcllm"
SCHEMA_VERSION = 1

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS serve_requests (
    serve_id      TEXT NOT NULL PRIMARY KEY,
    model         TEXT NOT NULL,
    cluster       TEXT NOT NULL DEFAULT 'sockeye',
    provider      TEXT NOT NULL DEFAULT 'llamacpp',
    status        TEXT NOT NULL DEFAULT 'pending',
    slurm_job_id  TEXT NOT NULL DEFAULT '',
    node          TEXT NOT NULL DEFAULT '',
    api_port      INTEGER NOT NULL DEFAULT 8000,
    local_port    INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT ''
);
"""

_initialized = False


def init() -> None:
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


class HpcllmDAO(BaseDAO):
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    def create(self, serve_id: str, model: str, cluster: str,
               provider: str = "llamacpp") -> dict:
        now = datetime.now(timezone.utc).isoformat()
        self.execute(
            """INSERT INTO serve_requests
               (serve_id, model, cluster, provider, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (serve_id, model, cluster, provider, now, now),
        )
        return self.get(serve_id)

    def get(self, serve_id: str) -> dict | None:
        return self.query_one(
            "SELECT * FROM serve_requests WHERE serve_id = ?",
            (serve_id,),
        )

    def update_status(self, serve_id: str, status: str, *,
                      slurm_job_id: str = "",
                      node: str = "",
                      api_port: int = 0,
                      local_port: int = 0,
                      error_message: str = "") -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        self.execute(
            """UPDATE serve_requests SET
               status = ?, slurm_job_id = ?, node = ?,
               api_port = ?, local_port = ?, error_message = ?,
               updated_at = ?
               WHERE serve_id = ?""",
            (status, slurm_job_id, node, api_port,
             local_port, error_message, now, serve_id),
        )
        return self.get(serve_id)

    def list_active(self) -> list[dict]:
        return self.query_all(
            "SELECT * FROM serve_requests WHERE status NOT IN ('cancelled', 'error', 'done') "
            "ORDER BY created_at DESC",
        )

    def delete(self, serve_id: str) -> bool:
        rows = self.execute(
            "DELETE FROM serve_requests WHERE serve_id = ?",
            (serve_id,),
        )
        return rows > 0
