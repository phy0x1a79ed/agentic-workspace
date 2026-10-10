"""The queue: one SQLite file holding the cards, the stream cursor and the
sessions the door started.

All of it lives in one database so a card write and a cursor write can never
disagree about which file survived. The subscriber saves the cursor only after
the card write has committed; a crash between the two replays an event the
queue absorbs, because every write is keyed by card id.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from awm.persistence import databases

from awm.representative import ACTIVE, ASSIGNED, CLAIMING, DONE, QUEUED, STATUSES, TERMINAL

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_id      TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    priority     TEXT NOT NULL DEFAULT 'normal',
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    sender       TEXT NOT NULL DEFAULT '',
    reply_to     TEXT,
    status       TEXT NOT NULL,
    assigned_to  TEXT,
    arrival      REAL NOT NULL,
    updated      REAL NOT NULL,
    notified     INTEGER NOT NULL DEFAULT 0,
    announced_at REAL
);
CREATE INDEX IF NOT EXISTS cards_status ON cards (status, arrival);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    role    TEXT NOT NULL,
    job     TEXT NOT NULL,
    started REAL NOT NULL,
    PRIMARY KEY (role, job)
);
"""

_URGENT_FIRST = "CASE priority WHEN 'urgent' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"
_CURSOR = "cursor"
MAX_LIST = 200
MESSAGE = "message"


class Queue:
    """The door's durable state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        databases.init_db_at(self.path, _SCHEMA, schema_version=1)

    def _conn(self) -> sqlite3.Connection:
        return databases.get_connection_at(self.path)

    # -- cards ---------------------------------------------------------------

    def enqueue(self, card: dict[str, Any], *, reopen: bool = False) -> bool:
        """Queue a board card, idempotently by card id.

        Returns True when the card was newly queued, left the ``claiming`` mark,
        or was reopened. A card already in the queue keeps its status and
        assignment, and its text is refreshed. ``reopen`` puts a finished card
        back in the queue, which is what a drag back to Posted on the board means.
        """
        now = time.time()
        fields = _fields(card)
        conn = self._conn()
        try:
            with conn:
                row = conn.execute("SELECT status FROM cards WHERE card_id = ?",
                                   (card["id"],)).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO cards (card_id, kind, priority, title, body, sender,"
                        " reply_to, status, arrival, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (card["id"], *fields, QUEUED, now, now))
                    return True
                conn.execute(
                    "UPDATE cards SET kind=?, priority=?, title=?, body=?, sender=?, reply_to=?,"
                    " updated=? WHERE card_id = ?", (*fields, now, card["id"]))
                if row["status"] == CLAIMING or (reopen and row["status"] in TERMINAL):
                    conn.execute(
                        "UPDATE cards SET status=?, assigned_to=NULL, notified=0,"
                        " announced_at=NULL WHERE card_id=?", (QUEUED, card["id"]))
                    return True
                return False
        finally:
            conn.close()

    def begin_claim(self, card: dict[str, Any]) -> bool:
        """Record that the door is about to claim ``card``. False if the queue already holds it."""
        now = time.time()
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO cards (card_id, kind, priority, title, body, sender,"
                    " reply_to, status, arrival, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (card["id"], *_fields(card), CLAIMING, now, now))
                return cur.rowcount > 0
        finally:
            conn.close()

    def drop_claim(self, card_id: str) -> None:
        """Forget a claim attempt the board refused."""
        conn = self._conn()
        try:
            with conn:
                conn.execute("DELETE FROM cards WHERE card_id=? AND status=?", (card_id, CLAIMING))
        finally:
            conn.close()

    def status_of(self, card_id: str) -> str | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT status FROM cards WHERE card_id = ?", (card_id,)).fetchone()
        finally:
            conn.close()
        return row["status"] if row else None

    def mark(self, card_id: str, status: str) -> bool:
        """Set a finished status on a card the queue holds. True if it changed.

        ``gone`` applies only to a card still in the door's hands: a card that
        finished is not made gone by a later re-address.
        """
        if status not in TERMINAL:
            raise ValueError(f"mark takes {TERMINAL}, not {status!r}")
        guard = " AND status IN (?,?,?)" if status == "gone" else " AND status != ?"
        args: tuple = (status, time.time(), card_id,
                       *((*ACTIVE, CLAIMING) if status == "gone" else (status,)))
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    f"UPDATE cards SET status=?, updated=? WHERE card_id=?{guard}", args)
                return cur.rowcount > 0
        finally:
            conn.close()

    def assign(self, card_id: str, agent: str) -> tuple[dict | None, str | None]:
        """Record the hand-off target. Returns ``(card, None)`` or ``(None, reason)``.

        A request becomes ``assigned``: the agent completes it on the board. A
        message is never completed on the board, so handing it off finishes it.
        """
        conn = self._conn()
        try:
            with conn:
                row = conn.execute("SELECT status, kind FROM cards WHERE card_id = ?",
                                   (card_id,)).fetchone()
                if row is None:
                    return None, f"no card {card_id} in the queue"
                if row["status"] not in ACTIVE:
                    return None, f"card {card_id} is {row['status']}, not open for hand-off"
                status = DONE if row["kind"] == MESSAGE else ASSIGNED
                conn.execute(
                    "UPDATE cards SET status=?, assigned_to=?, updated=? WHERE card_id=?",
                    (status, agent, time.time(), card_id))
        finally:
            conn.close()
        return self.get(card_id), None

    def get(self, card_id: str) -> dict | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
        finally:
            conn.close()
        return _public(row) if row else None

    def list(self, status: str | None = None, limit: int = 50) -> list[dict]:
        """Cards, urgent first then oldest first. All statuses when ``status`` is None."""
        if status is not None and status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        limit = max(1, min(int(limit), MAX_LIST))
        sql = "SELECT * FROM cards"
        args: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            args.append(status)
        sql += f" ORDER BY {_URGENT_FIRST}, arrival, card_id LIMIT ?"
        args.append(limit)
        conn = self._conn()
        try:
            return [_public(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def counts(self) -> dict[str, int]:
        conn = self._conn()
        try:
            found = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM cards GROUP BY status")}
        finally:
            conn.close()
        return {s: found.get(s, 0) for s in STATUSES}

    def active_ids(self) -> list[str]:
        marks = ",".join("?" for _ in ACTIVE)
        conn = self._conn()
        try:
            return [r["card_id"] for r in conn.execute(
                f"SELECT card_id FROM cards WHERE status IN ({marks}) ORDER BY arrival", ACTIVE)]
        finally:
            conn.close()

    def has(self, card_id: str) -> bool:
        return self.status_of(card_id) is not None

    # -- the wake-up batch ---------------------------------------------------

    def unannounced(self, stale_after_s: float | None = None,
                    now: float | None = None) -> list[tuple[str, str, bool]]:
        """Queued cards the representative should hear about: ``(card_id, priority, new)``.

        A card is new until it has been announced once. With ``stale_after_s``
        a card that stayed queued that long since its last announcement counts
        again, with ``new`` False.
        """
        now = time.time() if now is None else now
        cutoff = None if stale_after_s is None else now - stale_after_s
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT card_id, priority, notified FROM cards WHERE status=? AND"
                " (notified=0 OR (? IS NOT NULL AND COALESCE(announced_at, 0) < ?))"
                " ORDER BY arrival", (QUEUED, cutoff, cutoff)).fetchall()
        finally:
            conn.close()
        return [(r["card_id"], r["priority"], r["notified"] == 0) for r in rows]

    def mark_announced(self, card_ids: list[str], now: float | None = None) -> None:
        now = time.time() if now is None else now
        conn = self._conn()
        try:
            with conn:
                conn.executemany("UPDATE cards SET notified=1, announced_at=? WHERE card_id=?",
                                 [(now, c) for c in card_ids])
        finally:
            conn.close()

    def reset_announced(self) -> None:
        """Forget which queued cards were announced, so a new session hears of all of them."""
        conn = self._conn()
        try:
            with conn:
                conn.execute("UPDATE cards SET notified=0, announced_at=NULL WHERE status=?",
                             (QUEUED,))
        finally:
            conn.close()

    # -- the stream cursor ---------------------------------------------------

    def cursor(self) -> int | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (_CURSOR,)).fetchone()
        finally:
            conn.close()
        return int(row["value"]) if row else None

    def set_cursor(self, event_id: int, *, force: bool = False) -> None:
        """Save the cursor. It only moves forward unless ``force`` says the board's log restarted."""
        conn = self._conn()
        try:
            with conn:
                row = conn.execute("SELECT value FROM meta WHERE key=?", (_CURSOR,)).fetchone()
                if row is not None and not force and int(row["value"]) >= event_id:
                    return
                conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                             " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                             (_CURSOR, str(int(event_id))))
        finally:
            conn.close()

    # -- the sessions the door started --------------------------------------

    def record_session(self, role: str, job: str) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute("INSERT OR IGNORE INTO sessions (role, job, started) VALUES (?,?,?)",
                             (role, job, time.time()))
        finally:
            conn.close()

    def session_jobs(self, role: str) -> set[str]:
        conn = self._conn()
        try:
            return {r["job"] for r in conn.execute(
                "SELECT job FROM sessions WHERE role=?", (role,))}
        finally:
            conn.close()


def _fields(card: dict[str, Any]) -> tuple:
    return (
        str(card.get("kind") or "request"),
        str(card.get("priority") or "normal"),
        str(card.get("title") or ""),
        str(card.get("body") or ""),
        _sender(card),
        card.get("reply_to") or None,
    )


def _sender(card: dict[str, Any]) -> str:
    sender = card.get("sender")
    if isinstance(sender, dict):
        return str(sender.get("swarm") or "")
    return str(sender or "")


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _public(row: sqlite3.Row) -> dict:
    out = dict(row)
    out.pop("notified", None)
    out.pop("announced_at", None)
    out["arrival_at"] = _iso(out["arrival"])
    out["updated_at"] = _iso(out["updated"])
    return out
