"""Every channel this node holds, in one SQLite file the gateway's respawn survives.

The file holds each room's host token, so it lives in a directory only this
user can read.
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
    job       TEXT NOT NULL,
    holder    TEXT NOT NULL DEFAULT '',
    link      TEXT NOT NULL DEFAULT '',
    token     TEXT NOT NULL DEFAULT '',
    cursor    INTEGER NOT NULL DEFAULT 0,
    open      INTEGER NOT NULL DEFAULT 1,
    ended     TEXT,
    opened_at REAL NOT NULL,
    closed_at REAL,
    crypto    TEXT
)
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(SCHEMA)

    def add(self, *, name: str, kind: str, trust: str, holder: dict, link: str,
            token: str) -> dict:
        self._db.execute(
            "INSERT INTO channels (name, kind, trust, node, job, holder, link, token, opened_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, kind, trust, holder["node"], holder["job"], holder.get("name") or "",
             link, token, time.time()))
        return self.get(name)

    def get(self, name: str) -> dict | None:
        row = self._db.execute("SELECT * FROM channels WHERE name = ?", (name,)).fetchone()
        return _row(row)

    def open_rows(self) -> list[dict]:
        return [_row(r) for r in self._db.execute(
            "SELECT * FROM channels WHERE open = 1 ORDER BY opened_at")]

    def of_holder(self, node: str, job: str, *, open_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM channels WHERE node = ? AND job = ?"
        if open_only:
            sql += " AND open = 1"
        return [_row(r) for r in self._db.execute(sql + " ORDER BY opened_at", (node, job))]

    def set_cursor(self, name: str, cursor: int) -> None:
        self._db.execute("UPDATE channels SET cursor = ? WHERE name = ? AND cursor < ?",
                         (cursor, name, cursor))

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
