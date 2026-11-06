"""The routine service's own SQLite store: installed routines and their runs.

A partial unique index allows one live run per routine, so the database itself
refuses a second one (routines.md R38).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from contextlib import contextmanager

SCHEMA = """\
CREATE TABLE IF NOT EXISTS routines (
    project      TEXT NOT NULL,
    name         TEXT NOT NULL,
    root         TEXT NOT NULL,
    path         TEXT NOT NULL,
    commit_sha   TEXT NOT NULL DEFAULT '',
    definition   TEXT NOT NULL,
    scheduled    INTEGER NOT NULL DEFAULT 0,
    installed_at TEXT NOT NULL,
    PRIMARY KEY (project, name)
);
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    name        TEXT NOT NULL,
    trigger     TEXT NOT NULL,
    caller      TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL,
    failed_step TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    steps       TEXT NOT NULL DEFAULT '[]',
    dispatched  TEXT NOT NULL DEFAULT '[]',
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_run
    ON runs(project, name) WHERE state = 'running';
CREATE INDEX IF NOT EXISTS runs_by_routine ON runs(project, name, started_at);
"""

_JSON_COLS = {"steps", "dispatched"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class LiveRunExists(Exception):
    """A routine already has a live run."""


def default_path() -> Path:
    from awm.persistence.databases import service_db_path
    return service_db_path("routine")


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for k in _JSON_COLS & d.keys():
            d[k] = json.loads(d[k] or "[]")
        if "definition" in d:
            d["definition"] = json.loads(d["definition"])
        return d

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [self._row(r) for r in self._conn.execute(sql, args)]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            return self._row(self._conn.execute(sql, args).fetchone())

    # -- routines ---------------------------------------------------------

    def put_routine(self, *, project: str, name: str, root: str, path: str,
                    commit_sha: str, definition: dict[str, Any]) -> dict[str, Any]:
        with self._tx() as c:
            c.execute(
                "INSERT INTO routines(project, name, root, path, commit_sha, definition,"
                " installed_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(project, name) DO UPDATE SET root=excluded.root,"
                " path=excluded.path, commit_sha=excluded.commit_sha,"
                " definition=excluded.definition, installed_at=excluded.installed_at",
                (project, name, root, path, commit_sha, json.dumps(definition), now()))
        return self.get_routine(project, name)  # type: ignore[return-value]

    def get_routine(self, project: str, name: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM routines WHERE project=? AND name=?",
                         (project, name))

    def list_routines(self, project: str | None = None) -> list[dict[str, Any]]:
        if project:
            return self._all("SELECT * FROM routines WHERE project=? ORDER BY name",
                             (project,))
        return self._all("SELECT * FROM routines ORDER BY project, name")

    def delete_routine(self, project: str, name: str) -> bool:
        with self._tx() as c:
            cur = c.execute("DELETE FROM routines WHERE project=? AND name=?",
                            (project, name))
        return cur.rowcount > 0

    def set_scheduled(self, project: str, name: str, scheduled: bool) -> None:
        with self._tx() as c:
            c.execute("UPDATE routines SET scheduled=? WHERE project=? AND name=?",
                      (int(scheduled), project, name))

    # -- runs -------------------------------------------------------------

    def start_run(self, *, project: str, name: str, trigger: str,
                  caller: str = "") -> dict[str, Any]:
        rid = new_id()
        try:
            with self._tx() as c:
                c.execute(
                    "INSERT INTO runs(id, project, name, trigger, caller, state, started_at)"
                    " VALUES (?,?,?,?,?, 'running', ?)",
                    (rid, project, name, trigger, caller, now()))
        except sqlite3.IntegrityError as e:
            live = self.live_run(project, name)
            raise LiveRunExists(
                f"{project}/{name} already has a live run"
                + (f" ({live['id']})" if live else "")) from e
        return self.get_run(rid)  # type: ignore[return-value]

    def live_run(self, project: str, name: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM runs WHERE project=? AND name=? AND state='running'",
            (project, name))

    def get_run(self, rid: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM runs WHERE id=?", (rid,))

    def list_runs(self, project: str | None = None, name: str | None = None,
                  limit: int = 20) -> list[dict[str, Any]]:
        where, args = [], []
        if project:
            where.append("project=?")
            args.append(project)
        if name:
            where.append("name=?")
            args.append(name)
        sql = "SELECT * FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        return self._all(sql, (*args, int(limit)))

    def record_step(self, rid: str, outcome: dict[str, Any]) -> None:
        with self._tx() as c:
            row = c.execute("SELECT steps FROM runs WHERE id=?", (rid,)).fetchone()
            steps = json.loads(row["steps"]) if row else []
            steps.append(outcome)
            c.execute("UPDATE runs SET steps=? WHERE id=?", (json.dumps(steps), rid))

    def record_dispatch(self, rid: str, entry: dict[str, Any]) -> None:
        with self._tx() as c:
            row = c.execute("SELECT dispatched FROM runs WHERE id=?", (rid,)).fetchone()
            items = json.loads(row["dispatched"]) if row else []
            items.append(entry)
            c.execute("UPDATE runs SET dispatched=? WHERE id=?",
                      (json.dumps(items), rid))

    def finish_run(self, rid: str, state: str, *, failed_step: str = "",
                   error: str = "") -> dict[str, Any]:
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET state=?, failed_step=?, error=?, ended_at=?"
                " WHERE id=? AND state='running'",
                (state, failed_step, error[-4000:], now(), rid))
        return self.get_run(rid)  # type: ignore[return-value]

    def orphan_live(self) -> int:
        """Close runs a previous process left live. Returns the count."""
        with self._tx() as c:
            n = c.execute(
                "UPDATE runs SET state='failed', error='the routine service restarted"
                " mid-run', ended_at=? WHERE state='running'", (now(),)).rowcount
        return n
