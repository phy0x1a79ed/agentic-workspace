"""The task ledger: one row per session task, an append-only event history and the todo items.

A task is keyed by node plus job for a background session, or node plus pid
plus start time for an interactive one. Session ids are aliases, because a
conversation can be cleared or resumed under the same job. Nothing is deleted:
a closed task stays as the record, and a removed todo item keeps its row with
the time it left the list.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, Iterator

from awm.reporting.store import Db

PROPOSED, PLANNED, ACTIVE, BLOCKED, REVIEW = "proposed", "planned", "active", "blocked", "review"
DONE, REJECTED, CANCELLED = "done", "rejected", "cancelled"
OPEN = (PROPOSED, PLANNED, ACTIVE, BLOCKED, REVIEW)
CLOSED = (DONE, REJECTED, CANCELLED)
#: A cancelled task has no column: its card was deleted from the board.
COLUMNS = {PROPOSED: "Proposed", PLANNED: "Planned", ACTIVE: "Active",
           BLOCKED: "Blocked", REVIEW: "Pending review", DONE: "Done", REJECTED: "Rejected"}
ARCHIVE_AFTER_S = 14 * 86400.0
#: ``shown`` for a card folded into its column's Older book. ``archived`` is the old name.
OLDER_SHOWN = "older"

_FIELDS = frozenset({
    "session_id", "title", "project", "scope", "model", "effort", "state", "note_id", "shown", "dirty",
    "plan", "reason", "urgent", "blocked_at", "reminded", "ended", "closed", "owned",
    "card_id", "fed_state", "agent", "agent_link", "tags"})


class Ledger(Db):
    @contextlib.contextmanager
    def _tx(self) -> Iterator[Any]:
        conn = self._conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _one(self, sql: str, args: tuple) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._tx() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    # -- tasks ---------------------------------------------------------------

    def get(self, task_id: int) -> dict | None:
        return self._one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def find(self, node: str, key: str) -> dict | None:
        return self._one("SELECT * FROM tasks WHERE node=? AND key=?", (node, key))

    def open_for_card(self, card_id: str) -> dict | None:
        """The open task holding the federation card, if any."""
        marks = ",".join("?" for _ in OPEN)
        return self._one(f"SELECT * FROM tasks WHERE card_id=? AND state IN ({marks})"
                         " ORDER BY id DESC", (card_id, *OPEN))

    def archivable(self, node: str, now: float | None = None) -> list[dict]:
        """Finished tasks whose card has sat in Done or Rejected for the archive period, not yet folded.

        A claimed note of Tony's own is never folded: only a card the service
        made or a federation card is.
        """
        cutoff = (time.time() if now is None else now) - ARCHIVE_AFTER_S
        return self._all(
            "SELECT * FROM tasks WHERE node=? AND state IN (?,?) AND closed < ?"
            " AND note_id IS NOT NULL AND COALESCE(shown, '') NOT IN ('archived', ?)"
            " AND (owned = 1 OR card_id IS NOT NULL) ORDER BY closed",
            (node, DONE, REJECTED, cutoff, OLDER_SHOWN))

    def ensure(self, node: str, key: str, kind: str, *, state: str = PLANNED,
               pid: int | None = None, **fields: Any) -> tuple[dict, bool]:
        """The task for this session, made if there is none. True when it was made.

        A closed task is the same session again (a resumed job): it reopens.
        """
        now = time.time()
        fields = {k: v for k, v in fields.items() if k in _FIELDS and v is not None}
        with self._tx() as conn:
            made = conn.execute(
                "INSERT OR IGNORE INTO tasks (node, key, kind, pid, state, started, updated)"
                " VALUES (?,?,?,?,?,?,?)", (node, key, kind, pid, state, now, now)).rowcount > 0
        task = self.find(node, key)
        if not made and task["state"] in CLOSED:
            self.update(task["id"], state=state, closed=None, ended=0, **fields)
            self.event(task["id"], "reopened")
        elif fields:
            self.update(task["id"], **{k: v for k, v in fields.items() if not task.get(k)})
        return self.get(task["id"]), made

    def update(self, task_id: int, **fields: Any) -> None:
        unknown = set(fields) - _FIELDS
        if unknown:
            raise KeyError(unknown)
        marks = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as conn:
            conn.execute(f"UPDATE tasks SET {marks}{', ' if marks else ''}updated=? WHERE id=?",
                         (*fields.values(), time.time(), task_id))

    def rekey(self, task_id: int, key: str) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE tasks SET key=?, updated=? WHERE id=?", (key, time.time(), task_id))

    def open(self, node: str | None = None) -> list[dict]:
        marks = ",".join("?" for _ in OPEN)
        where = f"state IN ({marks})" + (" AND node=?" if node else "")
        return self._all(f"SELECT * FROM tasks WHERE {where} ORDER BY id",
                         (*OPEN, *([node] if node else [])))

    def closed(self, node: str) -> list[dict]:
        marks = ",".join("?" for _ in CLOSED)
        return self._all(f"SELECT * FROM tasks WHERE node=? AND state IN ({marks})"
                         " ORDER BY closed, id", (node, *CLOSED))

    def close(self, task_id: int, state: str = DONE) -> None:
        if state not in CLOSED:
            raise ValueError(f"close takes {CLOSED}, not {state!r}")
        self.update(task_id, state=state, closed=time.time())
        self.event(task_id, state)

    def alias(self, task_id: int, session_id: str) -> None:
        with self._tx() as conn:
            conn.execute("INSERT OR IGNORE INTO task_aliases (task_id, session_id) VALUES (?,?)",
                         (task_id, session_id))

    def aliases(self, task_id: int) -> list[str]:
        return [r["session_id"] for r in self._all(
            "SELECT session_id FROM task_aliases WHERE task_id=? ORDER BY rowid", (task_id,))]

    # -- events --------------------------------------------------------------

    def event(self, task_id: int, kind: str, detail: str = "", ref: str | None = None) -> bool:
        """Append to the history. False when ``ref`` was already recorded for this task."""
        with self._tx() as conn:
            return conn.execute(
                "INSERT OR IGNORE INTO task_events (task_id, at, kind, detail, ref)"
                " VALUES (?,?,?,?,?)", (task_id, time.time(), kind, detail, ref)).rowcount > 0

    def events(self, task_id: int, kind: str | None = None) -> list[dict]:
        return self._all("SELECT * FROM task_events WHERE task_id=?"
                         + (" AND kind=?" if kind else "") + " ORDER BY id",
                         (task_id, *([kind] if kind else [])))

    # -- todos ---------------------------------------------------------------

    def set_todos(self, task_id: int, items: list[dict]) -> bool:
        """Make ``items`` the live list. An item no longer in it is removed, never deleted.

        True when anything changed. An item that is listed again after it was
        removed comes back, and one whose status is ``deleted`` counts as gone.
        """
        now = time.time()
        live = {str(i["id"]): i for i in items if i.get("status") != "deleted"}
        changed = False
        with self._tx() as conn:
            have = {r["item_id"]: r for r in conn.execute(
                "SELECT * FROM todos WHERE task_id=?", (task_id,))}
            for item_id, item in live.items():
                subject, status = str(item.get("subject") or ""), str(item.get("status") or "pending")
                row = have.get(item_id)
                if row is None:
                    conn.execute("INSERT INTO todos (task_id, item_id, subject, status, first_seen)"
                                 " VALUES (?,?,?,?,?)", (task_id, item_id, subject, status, now))
                elif (row["subject"], row["status"], row["removed_at"]) != (subject, status, None):
                    conn.execute("UPDATE todos SET subject=?, status=?, removed_at=NULL"
                                 " WHERE task_id=? AND item_id=?", (subject, status, task_id, item_id))
                else:
                    continue
                changed = True
            for item_id, row in have.items():
                if item_id not in live and row["removed_at"] is None:
                    conn.execute("UPDATE todos SET removed_at=? WHERE task_id=? AND item_id=?",
                                 (now, task_id, item_id))
                    changed = True
        return changed

    def todos(self, task_id: int) -> tuple[list[dict], list[dict]]:
        """``(live, removed)``: live in first-seen order, removed in removal order."""
        rows = self._all("SELECT * FROM todos WHERE task_id=? ORDER BY first_seen, rowid", (task_id,))
        live = [r for r in rows if r["removed_at"] is None]
        gone = sorted((r for r in rows if r["removed_at"] is not None),
                      key=lambda r: r["removed_at"])
        return live, gone
