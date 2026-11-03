"""Tasks that span sessions (sessions.md R57, R58), on the reporting ledger's file.

This is the stand-in for redux-queue's task queue on alcor (fleet.md R106). The
verb names and record shapes match the queue's, so the cutover swaps the backend
and nothing else. A ledger row is still one session. A task groups the rows of
the sessions that worked on it, by session id. Nothing is deleted: a task that
folds into another keeps its row and points at the one it joined.
"""

from __future__ import annotations

import time
from typing import Any

from awm.reporting.ledger import CLOSED, Ledger
from awm.reporting.task_ids import claude_uuid, is_task_id, mint_task_id, session_key
from awm.reporting.work_schema import WORK_SCHEMA  # noqa: F401  re-exported for tests

MAIN, WORKER, JOINED = "main", "worker", "joined"
ROLES = (MAIN, WORKER, JOINED)


#: Longest fold chain followed before the lookup gives up: a cycle is a bug, not data.
_MAX_FOLDS = 32


class Work(Ledger):
    """The task records and their sessions."""

    def _resolve(self, conn: Any, task_id: str) -> str | None:
        seen = 0
        while seen < _MAX_FOLDS:
            row = conn.execute("SELECT task_id, folded_into FROM work WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                return None
            if not row["folded_into"]:
                return row["task_id"]
            task_id, seen = row["folded_into"], seen + 1
        raise ValueError(f"task {task_id} folds in a cycle")

    def _current(self, conn: Any, session: str) -> str | None:
        row = conn.execute("SELECT task_id FROM work_sessions WHERE session=? ORDER BY joined DESC",
                           (session,)).fetchone()
        return self._resolve(conn, row["task_id"]) if row else None

    def _require(self, conn: Any, task_id: str) -> str:
        if not is_task_id(task_id):
            raise ValueError(f"{task_id!r} is not a task id")
        resolved = self._resolve(conn, task_id)
        if resolved is None:
            raise ValueError(f"no task {task_id}")
        return resolved

    def _main(self, conn: Any, task_id: str) -> str | None:
        row = conn.execute("SELECT session FROM work_sessions WHERE task_id=? AND role=?"
                           " ORDER BY joined LIMIT 1", (task_id, MAIN)).fetchone()
        return row["session"] if row else None

    def _move(self, conn: Any, src: str, dst: str, now: float) -> None:
        """Fold task ``src`` into ``dst``: its sessions join ``dst`` and ``src`` points there."""
        for r in conn.execute("SELECT * FROM work_sessions WHERE task_id=?", (src,)).fetchall():
            role = r["role"] if r["role"] != MAIN else JOINED
            conn.execute("INSERT OR IGNORE INTO work_sessions (task_id, session, role, node, joined, ended,"
                         " name, rc_url) VALUES (?,?,?,?,?,?,?,?)",
                         (dst, r["session"], role, r["node"], now, r["ended"], r["name"], r["rc_url"]))
        conn.execute("UPDATE work SET folded_into=?, updated=? WHERE task_id=?", (dst, now, src))
        conn.execute("UPDATE work SET updated=? WHERE task_id=?", (now, dst))

    # -- the agreed verbs ----------------------------------------------------

    def task_open(self, title: str, session_id: str, node: str = "", *,
                  name: str | None = None, rc_url: str | None = None) -> str:
        """A new task with ``session_id`` as its main session (R57 step 1)."""
        session, now = session_key(session_id), time.time()
        with self._tx() as conn:
            while True:
                task_id = mint_task_id()
                if conn.execute("SELECT 1 FROM work WHERE task_id=?", (task_id,)).fetchone() is None:
                    break
            conn.execute("INSERT INTO work (task_id, title, node, created, updated) VALUES (?,?,?,?,?)",
                         (task_id, title or "", node, now, now))
            conn.execute("INSERT INTO work_sessions (task_id, session, role, node, joined, name, rc_url)"
                         " VALUES (?,?,?,?,?,?,?)", (task_id, session, MAIN, node, now, name, rc_url))
        return task_id

    def task_ensure(self, title: str, session_id: str, node: str = "") -> tuple[str, bool]:
        """The session's task, opened now when it has none. True when it was opened."""
        with self._tx() as conn:
            have = self._current(conn, session_key(session_id))
        return (have, False) if have else (self.task_open(title, session_id, node), True)

    def task_attach(self, task_id: str, session_id: str, role: str = WORKER, node: str = "", *,
                    name: str | None = None, rc_url: str | None = None) -> dict:
        """Put ``session_id`` on ``task_id`` (R57 step 3).

        A task the session already had folds in, so a session that opened its
        own task before the attach landed ends up on one task.
        """
        if role not in ROLES:
            raise ValueError(f"role is one of {ROLES}, not {role!r}")
        session, now = session_key(session_id), time.time()
        with self._tx() as conn:
            target = self._require(conn, task_id)
            old = self._current(conn, session)
            folded = None
            if old and old != target:
                self._move(conn, old, target, now)
                folded = old
            conn.execute("INSERT INTO work_sessions (task_id, session, role, node, joined, name, rc_url)"
                         " VALUES (?,?,?,?,?,?,?) ON CONFLICT (task_id, session) DO UPDATE SET"
                         " role=excluded.role, name=COALESCE(excluded.name, name),"
                         " rc_url=COALESCE(excluded.rc_url, rc_url)",
                         (target, session, role, node, now, name, rc_url))
            return {"task_id": target, "main_session_id": self._main(conn, target), "folded_task_id": folded}

    def task_join(self, task_id: str, session_id: str, node: str = "") -> dict:
        """Join a running task (R57 step 5). The caller sends the main session its notice."""
        return self.task_attach(task_id, session_id, JOINED, node)

    def task_for_session(self, session_id: str) -> str | None:
        with self._tx() as conn:
            return self._current(conn, session_key(session_id))

    def task_session_end(self, task_id: str, session_id: str) -> bool:
        session = session_key(session_id)
        with self._tx() as conn:
            target = self._require(conn, task_id)
            return conn.execute("UPDATE work_sessions SET ended=? WHERE task_id=? AND session=? AND ended IS NULL",
                                (time.time(), target, session)).rowcount > 0

    def task_sessions(self, task_id: str) -> list[dict]:
        """Every session of the task, active or finished (R58)."""
        with self._tx() as conn:
            target = self._require(conn, task_id)
            rows = [dict(r) for r in conn.execute(
                "SELECT session, role, node, joined, ended, name, rc_url FROM work_sessions WHERE task_id=?"
                " ORDER BY joined, rowid", (target,))]
        for r in rows:
            r["session_id"] = r.pop("session")
            r["active"] = r["ended"] is None and self._ledger_live(r["session_id"])
        return rows

    def task_get(self, task_id: str) -> dict:
        with self._tx() as conn:
            target = self._require(conn, task_id)
            row = dict(conn.execute("SELECT * FROM work WHERE task_id=?", (target,)).fetchone())
        row["sessions"] = self.task_sessions(target)
        if target != task_id:
            row["asked_for"] = task_id
        return row

    def _ledger_live(self, session: str) -> bool:
        """False when the ledger holds this session's row and that row is closed or ended."""
        raw = claude_uuid(session)
        if raw is None:
            return True
        row = self._one("SELECT t.state, t.ended FROM tasks t WHERE t.session_id=? OR t.id IN"
                        " (SELECT task_id FROM task_aliases WHERE session_id=?) ORDER BY t.id DESC",
                        (raw, raw))
        return row is None or (row["state"] not in CLOSED and not row["ended"])
