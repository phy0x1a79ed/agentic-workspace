"""Run history for the transcripts service.

One table on the service's own SQLite DB (``AWM_DIR/services/transcripts/
transcripts.db``). It exists because the shell script this replaces recorded
each sweep as one line appended to a log file, which answers "did it run" and
nothing else — not how much it moved, not what it failed on, not whether the
backlog is shrinking.

Rows are append-only. A sweep that fails partway still writes its row, carrying
the failures it collected, because a sweep that archived 300 sessions and choked
on one is a success with a footnote rather than a failure.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db, new_uuid

SERVICE = "transcripts"
SCHEMA_VERSION = 2

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS transcripts_runs (
    id             TEXT    NOT NULL PRIMARY KEY,
    kind           TEXT    NOT NULL,
    trigger        TEXT    NOT NULL DEFAULT 'manual',
    dry_run        INTEGER NOT NULL DEFAULT 0,
    retention_days REAL    NOT NULL DEFAULT 0,
    sessions       INTEGER NOT NULL DEFAULT 0,
    orphans        INTEGER NOT NULL DEFAULT 0,
    files          INTEGER NOT NULL DEFAULT 0,
    vanished       INTEGER NOT NULL DEFAULT 0,
    bytes_before   INTEGER NOT NULL DEFAULT 0,
    bytes_after    INTEGER NOT NULL DEFAULT 0,
    failures       INTEGER NOT NULL DEFAULT 0,
    detail         TEXT    NOT NULL DEFAULT '',
    started_at     TEXT    NOT NULL DEFAULT '',
    seconds        REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS transcripts_runs_started
    ON transcripts_runs (started_at DESC);
"""


# v2 added `vanished`, the count of files that disappeared between the directory
# listing and the copy. Capella's first sweep found one and aborted that session
# on it, leaving the transcript archived and its sidecar half-moved.
MIGRATIONS = {
    (1, 2): "ALTER TABLE transcripts_runs ADD COLUMN vanished INTEGER NOT NULL "
            "DEFAULT 0;",
}


def init() -> None:
    init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION,
                    migrations=MIGRATIONS)


class RunsDAO(BaseDAO):
    def __init__(self, conn=None) -> None:
        super().__init__(SERVICE, conn)

    def record(self, kind: str, result: dict[str, Any], *,
               trigger: str = "manual", detail: str = "") -> str:
        run_id = new_uuid()
        self.execute(
            "INSERT INTO transcripts_runs (id, kind, trigger, dry_run, "
            "retention_days, sessions, orphans, files, vanished, bytes_before, "
            "bytes_after, failures, detail, started_at, seconds) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, kind, trigger, int(bool(result.get("dry_run"))),
             float(result.get("retention_days") or 0),
             int(result.get("sessions") or 0),
             int(result.get("orphans") or 0),
             int(result.get("files") or 0),
             int(result.get("vanished") or 0),
             int(result.get("bytes_before") or 0),
             int(result.get("bytes_after") or result.get("bytes_freed") or 0),
             len(result.get("failed") or ()),
             detail,
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             float(result.get("seconds") or 0)),
        )
        return run_id

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM transcripts_runs ORDER BY started_at DESC LIMIT ?",
            (int(limit),))
