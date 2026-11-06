"""The rooms this relay carries, their members and their messages, in one SQLite file.

The file outlives a restart of the service, so a message no member has fetched
yet waits here (``channels.md`` R72 item 5). It holds each room's host token, so
it lives in a directory only this user can read. A message keeps the body the
relay carried: plaintext in a plain room, and in an encrypted room only the
sealed text and the key exchange, which the member's own node opens (R73).

A member is one session on one node, ``(room, node, session)``. Its ``cursor``
is the last message id it acknowledged, and ``lease_at`` the last time its node
said the session still runs.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    name      TEXT PRIMARY KEY,
    lasts     TEXT NOT NULL,
    link      TEXT NOT NULL,
    token     TEXT NOT NULL,
    cursor    INTEGER NOT NULL DEFAULT 0,
    sealed    INTEGER NOT NULL DEFAULT 0,
    open      INTEGER NOT NULL DEFAULT 1,
    ended     TEXT,
    opened_at REAL NOT NULL,
    closed_at REAL
);
CREATE TABLE IF NOT EXISTS members (
    room      TEXT NOT NULL,
    node      TEXT NOT NULL,
    session   TEXT NOT NULL,
    name      TEXT NOT NULL DEFAULT '',
    level     TEXT NOT NULL DEFAULT 'untrusted',
    cursor    INTEGER NOT NULL DEFAULT 0,
    lease_at  REAL NOT NULL,
    joined_at REAL NOT NULL,
    left_at   REAL,
    left_why  TEXT,
    PRIMARY KEY (room, node, session)
);
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    room      TEXT NOT NULL,
    seq       INTEGER,
    by        TEXT NOT NULL,
    node      TEXT NOT NULL DEFAULT '',
    session   TEXT NOT NULL DEFAULT '',
    who       TEXT NOT NULL DEFAULT '',
    level     TEXT NOT NULL DEFAULT 'untrusted',
    body      TEXT NOT NULL,
    at        REAL NOT NULL,
    UNIQUE (room, seq)
);
"""

#: Who wrote a message: the guest over the link, one of our members, or the relay.
GUEST, MEMBER, NOTICE = "guest", "member", "notice"


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    # -- rooms ---------------------------------------------------------------

    def add_room(self, *, name: str, lasts: str, link: str, token: str) -> dict:
        self._db.execute("INSERT INTO rooms (name, lasts, link, token, opened_at)"
                         " VALUES (?, ?, ?, ?, ?)", (name, lasts, link, token, time.time()))
        return self.room(name)

    def room(self, name: str) -> dict | None:
        return _row(self._db.execute("SELECT * FROM rooms WHERE name = ?", (name,)).fetchone())

    def open_rooms(self) -> list[dict]:
        return [_row(r) for r in self._db.execute(
            "SELECT * FROM rooms WHERE open = 1 ORDER BY opened_at")]

    def set_cursor(self, name: str, cursor: int) -> None:
        self._db.execute("UPDATE rooms SET cursor = ? WHERE name = ? AND cursor < ?",
                         (cursor, name, cursor))

    def seal(self, name: str, level: int) -> None:
        """Raise how far the room has moved to encryption; it never goes back."""
        self._db.execute("UPDATE rooms SET sealed = MAX(sealed, ?) WHERE name = ?", (level, name))

    def close_room(self, name: str, why: str) -> bool:
        """Mark a room closed. False when it was closed already."""
        cur = self._db.execute(
            "UPDATE rooms SET open = 0, ended = ?, closed_at = ?, token = ''"
            " WHERE name = ? AND open = 1", (why, time.time(), name))
        return cur.rowcount == 1

    # -- members -------------------------------------------------------------

    def add_member(self, room: str, member: dict) -> None:
        now = time.time()
        self._db.execute(
            "INSERT INTO members (room, node, session, name, level, cursor, lease_at, joined_at)"
            " VALUES (?, ?, ?, ?, ?, (SELECT COALESCE(MAX(id), 0) FROM messages WHERE room = ?),"
            " ?, ?)"
            " ON CONFLICT (room, node, session) DO UPDATE SET left_at = NULL, left_why = NULL,"
            " lease_at = excluded.lease_at, name = excluded.name, level = excluded.level",
            (room, member["node"], member["session"], member.get("name") or "",
             member.get("level") or "untrusted", room, now, now))

    def member(self, room: str, node: str, session: str) -> dict | None:
        return _row(self._db.execute(
            "SELECT * FROM members WHERE room = ? AND node = ? AND session = ?"
            " AND left_at IS NULL", (room, node, session)).fetchone())

    def members(self, room: str) -> list[dict]:
        return [_row(r) for r in self._db.execute(
            "SELECT * FROM members WHERE room = ? AND left_at IS NULL ORDER BY joined_at",
            (room,))]

    def of_node(self, node: str) -> list[dict]:
        """Every present membership of ``node``'s sessions in an open room or one
        with messages still to fetch."""
        return [_row(r) for r in self._db.execute(
            "SELECT m.* FROM members m JOIN rooms r ON r.name = m.room"
            " WHERE m.node = ? AND m.left_at IS NULL ORDER BY m.joined_at", (node,))]

    def of_session(self, node: str, session: str) -> list[dict]:
        return [_row(r) for r in self._db.execute(
            "SELECT r.*, m.cursor AS member_cursor, m.left_at, m.left_why FROM members m"
            " JOIN rooms r ON r.name = m.room WHERE m.node = ? AND m.session = ?"
            " ORDER BY r.opened_at", (node, session))]

    def renew(self, node: str, sessions: list[str] | None) -> None:
        """Renew the lease of ``node``'s members in ``sessions``, or of all of them."""
        now = time.time()
        if sessions is None:
            self._db.execute("UPDATE members SET lease_at = ? WHERE node = ? AND left_at IS NULL",
                             (now, node))
            return
        self._db.executemany(
            "UPDATE members SET lease_at = ? WHERE node = ? AND session = ? AND left_at IS NULL",
            [(now, node, s) for s in sessions])

    def ack(self, room: str, node: str, session: str, through: int) -> None:
        self._db.execute("UPDATE members SET cursor = ? WHERE room = ? AND node = ?"
                         " AND session = ? AND cursor < ?", (through, room, node, session, through))

    def leave(self, room: str, node: str, session: str, why: str) -> bool:
        cur = self._db.execute(
            "UPDATE members SET left_at = ?, left_why = ? WHERE room = ? AND node = ?"
            " AND session = ? AND left_at IS NULL", (time.time(), why, room, node, session))
        return cur.rowcount == 1

    def lapsed(self, before: float) -> list[dict]:
        return [_row(r) for r in self._db.execute(
            "SELECT * FROM members WHERE left_at IS NULL AND lease_at < ?", (before,))]

    # -- messages ------------------------------------------------------------

    def add_message(self, room: str, *, by: str, body: dict, seq: int | None = None,
                    member: dict | None = None) -> int:
        """Keep one message and return its id. The relay's ``seq`` is unique per room,
        so a message carried twice is kept once."""
        m = member or {}
        cur = self._db.execute(
            "INSERT INTO messages (room, seq, by, node, session, who, level, body, at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (room, seq) DO NOTHING",
            (room, seq, by, m.get("node") or "", m.get("session") or "", m.get("who") or "",
             m.get("level") or "untrusted", json.dumps(body), time.time()))
        return int(cur.lastrowid or 0)

    def pending(self, member: dict) -> list[dict]:
        """The messages after ``member``'s cursor that another side wrote."""
        rows = self._db.execute(
            "SELECT * FROM messages WHERE room = ? AND id > ?"
            " AND NOT (by = ? AND node = ? AND session = ?) ORDER BY id",
            (member["room"], member["cursor"], MEMBER, member["node"], member["session"]))
        return [_message(r) for r in rows]

    def forget(self, before: float) -> None:
        """Drop the messages of rooms that closed before ``before``."""
        self._db.execute("DELETE FROM messages WHERE room IN"
                         " (SELECT name FROM rooms WHERE open = 0 AND closed_at < ?)", (before,))


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out: dict[str, Any] = dict(row)
    for key in ("open",):
        if key in out:
            out[key] = bool(out[key])
    return out


def _message(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["body"] = json.loads(out["body"])
    return out
