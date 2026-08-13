"""Schedule state and run history for the dvc service's backup jobs.

Two tables on the service's own SQLite DB (``AWM_DIR/services/dvc/dvc.db``):

* ``dvc_jobs`` — one row per job, holding the *mutable* half of its definition
  (cron, enabled, catch-up window, last fire). The immutable half — what the job
  actually does — is code, in :mod:`awm.dvc.jobs`. Code seeds a row on first
  insert only, so an operator's ``schedule`` edit survives a service restart
  instead of being stomped by the default at every boot.
* ``dvc_runs`` — one row per attempt, from before submission through to a
  verdict. This is the history that did not exist when the schedule was a
  systemd timer and the only record was a log line.

THE SINGLE-FLIGHT GUARD IS AN INDEX, NOT A LOCK
``UNIQUE (job) WHERE status IN ('submitting','running')`` is what stops two
full-cache scans stacking. It replaced a module-level ``threading.Lock``, which
could only ever guard one process — and there are three that submit (the
scheduler, the MCP verb, and the ``awm-dvc-sync`` console script). The insert
*is* the acquire: :meth:`RunsDAO.begin_run` returns ``None`` on the
``IntegrityError``, and that ``None`` means "already in flight". Per-job only;
the two jobs are deliberately allowed to run concurrently, since serializing
them would skip the mirror every night the 319 GB cache scan ran long.

Status domain: ``submitting`` → ``running`` → ``SUCCEEDED`` | ``FAILED``. The
terminal pair is spelled verbatim as Globus spells it (see
``globus.TERMINAL``) so the string in a run row means the same thing as the one
in ``dvc task``. ``skipped`` (declined, or superseded by a force) and ``error``
(never reached Globus at all) are ours. There is deliberately no ``stalled``
status: a long run stamps ``alerted_at`` and keeps polling, because only Globus
gets to decide a verdict.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from time import time
from typing import Any

from awm.persistence.dao import BaseDAO
from awm.persistence.databases import init_service_db, new_uuid

SERVICE = "dvc"
SCHEMA_VERSION = 1

# The statuses that mean "this run owns the job's single-flight slot". The SQL
# literal is spelled out beside them because it also appears inside the partial
# indexes below, and SQLite matches those by expression text — the two must say
# exactly the same thing or the guard silently stops applying.
LIVE = ("submitting", "running")
_LIVE_SQL = "('submitting', 'running')"

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS dvc_jobs (
    name             TEXT    NOT NULL PRIMARY KEY,
    cron             TEXT    NOT NULL DEFAULT '',
    enabled          INTEGER NOT NULL DEFAULT 1,
    catchup_window_s INTEGER NOT NULL DEFAULT 0,
    last_fire_at     REAL    NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT '',
    updated_at       TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dvc_runs (
    id                TEXT    NOT NULL PRIMARY KEY,
    job               TEXT    NOT NULL,
    trigger           TEXT    NOT NULL DEFAULT 'manual',
    task_id           TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL DEFAULT 'submitting',
    label             TEXT    NOT NULL DEFAULT '',
    started_at        REAL    NOT NULL DEFAULT 0,
    submitted_at      REAL    NOT NULL DEFAULT 0,
    polled_at         REAL    NOT NULL DEFAULT 0,
    finished_at       REAL    NOT NULL DEFAULT 0,
    files             INTEGER NOT NULL DEFAULT 0,
    files_transferred INTEGER NOT NULL DEFAULT 0,
    files_skipped     INTEGER NOT NULL DEFAULT 0,
    bytes_transferred INTEGER NOT NULL DEFAULT 0,
    faults            INTEGER NOT NULL DEFAULT 0,
    nice_status       TEXT    NOT NULL DEFAULT '',
    error             TEXT    NOT NULL DEFAULT '',
    note              TEXT    NOT NULL DEFAULT '',
    alerted_at        REAL    NOT NULL DEFAULT 0
);

-- Load-bearing: this is the single-flight guard. See the module docstring.
CREATE UNIQUE INDEX IF NOT EXISTS dvc_runs_single_flight
    ON dvc_runs (job) WHERE status IN ('submitting', 'running');
-- Adoption idempotence: one row per Globus task, so a task can never be
-- watched by two rows that then disagree about its verdict.
CREATE UNIQUE INDEX IF NOT EXISTS dvc_runs_one_row_per_task
    ON dvc_runs (task_id) WHERE task_id != '';
-- The adopt sweep's scan, run every tick.
CREATE INDEX IF NOT EXISTS dvc_runs_live
    ON dvc_runs (status) WHERE status IN ('submitting', 'running');
CREATE INDEX IF NOT EXISTS dvc_runs_history
    ON dvc_runs (job, started_at DESC);
"""

_RUN_COLS = (
    "id, job, trigger, task_id, status, label, started_at, submitted_at, "
    "polled_at, finished_at, files, files_transferred, files_skipped, "
    "bytes_transferred, faults, nice_status, error, note, alerted_at"
)

_initialized = False


def init() -> None:
    """Idempotently create the dvc service's DB and its two tables."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso(ts: float | None) -> str:
    """Epoch seconds as an ISO-8601 UTC string; ``""`` for 0/None."""
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def to_epoch(value: Any) -> float | None:
    """Coerce an epoch number or ISO-8601 string to epoch seconds, else None."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


# Globus counter fields copied verbatim from a `task_status` document onto a run
# row. Named once so a poll, a finish, and an adoption cannot drift apart.
_COUNTERS = (
    "files",
    "files_transferred",
    "files_skipped",
    "bytes_transferred",
    "faults",
)


def _counter_values(status_doc: dict) -> list[Any]:
    return [int(status_doc.get(k) or 0) for k in _COUNTERS]


class RunsDAO(BaseDAO):
    """Schedule state (``dvc_jobs``) and run history (``dvc_runs``)."""

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        super().__init__(SERVICE, conn=conn)

    # -- jobs ---------------------------------------------------------------

    def seed_job(
        self, name: str, *, cron: str, catchup_window_s: int, enabled: bool = True
    ) -> dict:
        """Insert the job's row if absent; leave an existing one untouched.

        ``DO NOTHING`` rather than an upsert is the point: the defaults live in
        code, but a schedule the operator changed is state, and a restart must
        not silently revert it.
        """
        now = _now_iso()
        self.execute(
            "INSERT INTO dvc_jobs (name, cron, enabled, catchup_window_s, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO NOTHING",
            (name, cron, 1 if enabled else 0, int(catchup_window_s), now, now),
        )
        return self.get_job(name)

    def get_job(self, name: str) -> dict | None:
        return self.query_one("SELECT * FROM dvc_jobs WHERE name = ?", (name,))

    def list_jobs(self) -> list[dict]:
        return self.query_all("SELECT * FROM dvc_jobs ORDER BY name")

    def set_schedule(
        self, name: str, *, cron: str | None = None, enabled: bool | None = None
    ) -> dict | None:
        sets, params = [], []
        if cron is not None:
            sets.append("cron = ?")
            params.append(cron)
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(1 if enabled else 0)
        if sets:
            sets.append("updated_at = ?")
            params.append(_now_iso())
            params.append(name)
            self.execute(
                f"UPDATE dvc_jobs SET {', '.join(sets)} WHERE name = ?", params
            )
        return self.get_job(name)

    def set_last_fire(self, name: str, when: float) -> None:
        self.execute(
            "UPDATE dvc_jobs SET last_fire_at = ?, updated_at = ? WHERE name = ?",
            (float(when), _now_iso(), name),
        )

    # -- runs: creation -----------------------------------------------------

    def begin_run(
        self, job: str, *, trigger: str = "manual", note: str = ""
    ) -> dict | None:
        """Claim the job's single-flight slot with a fresh ``submitting`` row.

        Returns the row, or ``None`` if another run already holds the slot —
        the ``IntegrityError`` from the partial unique index *is* the answer, so
        there is no check-then-act window between two processes.
        """
        run_id = new_uuid()
        try:
            self.execute(
                "INSERT INTO dvc_runs (id, job, trigger, status, started_at, note) "
                "VALUES (?, ?, ?, 'submitting', ?, ?)",
                (run_id, job, trigger, time(), note),
            )
        except sqlite3.IntegrityError:
            return None
        return self.get_run(run_id)

    def record_skip(self, job: str, *, trigger: str, note: str) -> dict:
        """Record a run that was declined — a row, so silence never needs reading.

        The in-flight task's id goes in ``note``, never in ``task_id``: that
        column is unique per task and belongs to the run that owns the transfer,
        not to the one that stood down for it.
        """
        run_id = new_uuid()
        now = time()
        self.execute(
            "INSERT INTO dvc_runs (id, job, trigger, status, started_at, "
            "finished_at, note) VALUES (?, ?, ?, 'skipped', ?, ?, ?)",
            (run_id, job, trigger, now, now, note),
        )
        return self.get_run(run_id)

    # -- runs: transitions --------------------------------------------------

    def mark_submitted(self, run_id: str, task_id: str, *, label: str = "") -> None:
        self.execute(
            "UPDATE dvc_runs SET status = 'running', task_id = ?, label = ?, "
            "submitted_at = ? WHERE id = ?",
            (task_id, label, time(), run_id),
        )

    def record_poll(self, run_id: str, status_doc: dict) -> None:
        """Persist a Globus status document. Always called *before* emitting."""
        self.execute(
            "UPDATE dvc_runs SET polled_at = ?, nice_status = ?, "
            "files = ?, files_transferred = ?, files_skipped = ?, "
            "bytes_transferred = ?, faults = ? WHERE id = ?",
            [
                time(),
                str(status_doc.get("nice_status") or ""),
                *_counter_values(status_doc),
                run_id,
            ],
        )

    def finish(self, run_id: str, status: str, status_doc: dict | None = None) -> None:
        """Stamp a terminal verdict, folding in the final counters."""
        doc = status_doc or {}
        now = time()
        self.execute(
            "UPDATE dvc_runs SET status = ?, finished_at = ?, polled_at = ?, "
            "nice_status = ?, files = ?, files_transferred = ?, files_skipped = ?, "
            "bytes_transferred = ?, faults = ? WHERE id = ?",
            [
                status,
                now,
                now,
                str(doc.get("nice_status") or ""),
                *_counter_values(doc),
                run_id,
            ],
        )

    def fail(self, run_id: str, error: str) -> None:
        """A run that never reached Globus — config, auth, or a build blowing up."""
        now = time()
        self.execute(
            "UPDATE dvc_runs SET status = 'error', error = ?, finished_at = ? "
            "WHERE id = ?",
            (str(error)[:2000], now, run_id),
        )

    def set_note(self, run_id: str, note: str) -> None:
        self.execute(
            "UPDATE dvc_runs SET note = ? WHERE id = ?", (str(note)[:2000], run_id)
        )

    def mark_alerted(self, run_id: str) -> None:
        self.execute(
            "UPDATE dvc_runs SET alerted_at = ? WHERE id = ?", (time(), run_id)
        )

    def close_live(self, job: str, *, note: str) -> int:
        """Retire whatever holds ``job``'s slot, so a forced run can claim it."""
        now = time()
        return self.execute(
            "UPDATE dvc_runs SET status = 'skipped', finished_at = ?, note = ? "
            f"WHERE job = ? AND status IN {_LIVE_SQL}",
            (now, note, job),
        )

    # -- runs: reads --------------------------------------------------------

    def get_run(self, run_id: str) -> dict | None:
        return self.query_one(
            f"SELECT {_RUN_COLS} FROM dvc_runs WHERE id = ?", (run_id,)
        )

    def run_for_task(self, task_id: str) -> dict | None:
        return self.query_one(
            f"SELECT {_RUN_COLS} FROM dvc_runs WHERE task_id = ?", (task_id,)
        )

    def live_runs(self) -> list[dict]:
        return self.query_all(
            f"SELECT {_RUN_COLS} FROM dvc_runs WHERE status IN {_LIVE_SQL} "
            "ORDER BY started_at"
        )

    def live_run(self, job: str) -> dict | None:
        return self.query_one(
            f"SELECT {_RUN_COLS} FROM dvc_runs WHERE job = ? AND status IN {_LIVE_SQL}",
            (job,),
        )

    def last_run(self, job: str) -> dict | None:
        return self.query_one(
            f"SELECT {_RUN_COLS} FROM dvc_runs WHERE job = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (job,),
        )

    def history(
        self,
        *,
        job: str | None = None,
        limit: int = 20,
        status: str | None = None,
        since: float | None = None,
    ) -> list[dict]:
        where, params = [], []
        if job:
            where.append("job = ?")
            params.append(job)
        if status:
            where.append("status = ?")
            params.append(status)
        if since:
            where.append("started_at >= ?")
            params.append(float(since))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(max(1, min(int(limit), 500)))
        return self.query_all(
            f"SELECT {_RUN_COLS} FROM dvc_runs {clause} "
            "ORDER BY started_at DESC LIMIT ?",
            params,
        )
