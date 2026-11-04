"""Every channel this node holds, in one SQLite file the gateway's respawn survives.

The file holds each room's host token, so it lives in a directory only this
user can read. A holder is a session: ``node`` and ``session`` name it, and
``job`` is where its lines are typed. For a machine, ``token`` is the tether
slot and ``ref`` the session id the stream gave its mint. ``cursor`` follows
the conversation and ``log_cursor`` the tool log, each in the daemon epoch
beside it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    name      TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,
    trust     TEXT NOT NULL,
    node      TEXT NOT NULL,
    session   TEXT NOT NULL DEFAULT '',
    job       TEXT NOT NULL,
    holder    TEXT NOT NULL DEFAULT '',
    link      TEXT NOT NULL DEFAULT '',
    token     TEXT NOT NULL DEFAULT '',
    cursor    INTEGER NOT NULL DEFAULT 0,
    open      INTEGER NOT NULL DEFAULT 1,
    ended     TEXT,
    opened_at REAL NOT NULL,
    closed_at REAL,
    crypto    TEXT,
    lasts     TEXT NOT NULL DEFAULT 'task',
    ref       TEXT NOT NULL DEFAULT '',
    epoch     INTEGER,
    log_cursor INTEGER NOT NULL DEFAULT 0,
    log_epoch INTEGER
)
"""

#: Columns a file written before they existed lacks: name and declaration.
ADDED = (
    ("session", "TEXT NOT NULL DEFAULT ''"),
    ("lasts", "TEXT NOT NULL DEFAULT 'task'"),
    ("ref", "TEXT NOT NULL DEFAULT ''"),
    ("epoch", "INTEGER"),
    ("log_cursor", "INTEGER NOT NULL DEFAULT 0"),
    ("log_epoch", "INTEGER"),
)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(SCHEMA)
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(channels)")}
        for column, decl in ADDED:
            if column not in have:
                self._db.execute(f"ALTER TABLE channels ADD COLUMN {column} {decl}")

    def add(self, *, name: str, kind: str, trust: str, holder: dict, link: str,
            token: str, lasts: str = "task", ref: str = "",
            epoch: int | None = None) -> dict:
        self._db.execute(
            "INSERT INTO channels (name, kind, trust, node, session, job, holder, link, token,"
            " opened_at, lasts, ref, epoch, log_epoch)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, kind, trust, holder["node"], holder["session"], holder["job"],
             holder.get("name") or "", link, token, time.time(), lasts, ref, epoch, epoch))
        return self.get(name)

    def get(self, name: str) -> dict | None:
        row = self._db.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()
        return _row(row)

    def open_rows(self) -> list[dict]:
        return [_row(r) for r in self._db.execute(
            "SELECT * FROM channels WHERE open = 1 ORDER BY opened_at")]

    def of_holder(self, node: str, session: str, *, open_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM channels WHERE node = ? AND session = ?"
        if open_only:
            sql += " AND open = 1"
        return [_row(r) for r in self._db.execute(sql + " ORDER BY opened_at",
                                                  (node, session))]

    def of_job(self, node: str, job: str) -> list[dict]:
        return [_row(r) for r in self._db.execute(
            "SELECT * FROM channels WHERE node = ? AND job = ? AND open = 1"
            " ORDER BY opened_at", (node, job))]

    def set_cursor(self, name: str, cursor: int) -> None:
        self._db.execute("UPDATE channels SET cursor = ? WHERE name = ? AND cursor < ?",
                         (cursor, name, cursor))

    def set_follow(self, name: str, cursor: int, epoch: int | None) -> None:
        """A machine's conversation cursor, which starts again in a new epoch."""
        self._db.execute("UPDATE channels SET cursor = ?, epoch = ? WHERE name = ?",
                         (cursor, epoch, name))

    def set_log(self, name: str, cursor: int, epoch: int | None) -> None:
        self._db.execute("UPDATE channels SET log_cursor = ?, log_epoch = ? WHERE name = ?",
                         (cursor, epoch, name))

    def set_ref(self, name: str, ref: str) -> None:
        self._db.execute("UPDATE channels SET ref = ? WHERE name = ?", (ref, name))

    def set_crypto(self, name: str, crypto: dict | None) -> None:
        self._db.execute("UPDATE channels SET crypto = ? WHERE name = ?",
                         (json.dumps(crypto) if crypto is not None else None, name))

    def close(self, name: str, why: str) -> bool:
        """Mark a channel closed. False when it was closed already."""
        cur = self._db.execute(
            "UPDATE channels SET open = 0, ended = ?, closed_at = ?, token = '', crypto = NULL"
            " WHERE name = ? AND open = 1", (why, time.time(), name))
        return cur.rowcount == 1


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out: dict[str, Any] = dict(row)
    out["open"] = bool(out["open"])
    out["crypto"] = json.loads(out["crypto"]) if out["crypto"] else None
    return out
