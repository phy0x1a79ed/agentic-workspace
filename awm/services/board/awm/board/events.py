"""The event log: an append-only record that exists so a stream can resume.

The vault is the source of truth for cards. Nothing here is read to decide what
a card is, only to tell a reconnecting client what happened while it was away.
The log also holds the board's memory of the last status it saw for each card,
which the watcher compares against the vault to notice a drag in the GUI.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from awm.persistence import databases

from .parties import can_see

TYPES = ("card.posted", "card.claimed", "card.completed", "card.failed", "card.moved")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT NOT NULL,
    card_id    TEXT NOT NULL,
    sender     TEXT NOT NULL,
    recipient  TEXT NOT NULL,
    card       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS last_seen (
    card_id TEXT PRIMARY KEY,
    status  TEXT NOT NULL
);
"""


class Events:
    """The event log in a SQLite file at ``path``."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        databases.init_db_at(self._path, _SCHEMA, schema_version=1)

    def _conn(self) -> sqlite3.Connection:
        return databases.get_connection_at(self._path)

    def append(self, type: str, card: dict) -> int:
        """Record one event with a snapshot of the card. Ids only ever increase."""
        if type not in TYPES:
            raise ValueError(f"unknown event type {type!r}")
        conn = self._conn()
        try:
            cur = conn.execute(
                "INSERT INTO events (type, card_id, sender, recipient, card, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (type, card["id"], card["sender"]["swarm"], card["recipient"],
                 json.dumps(card), time.time()),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def since(self, last_id: int, party: dict) -> list[dict]:
        """Events after ``last_id`` that ``party`` may see, oldest first."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, type, card, created_at FROM events WHERE id > ? ORDER BY id",
                (int(last_id or 0),),
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            card = json.loads(r["card"])
            if can_see(party, card):
                out.append({"id": r["id"], "type": r["type"], "card": card,
                            "created_at": r["created_at"]})
        return out

    def latest_id(self) -> int:
        conn = self._conn()
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM events").fetchone()
        finally:
            conn.close()
        return int(row["n"])

    def prune(self, days: float = 30) -> int:
        """Drop events older than ``days``. Returns how many went."""
        cutoff = time.time() - days * 86400
        conn = self._conn()
        try:
            cur = conn.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    # -- the watcher's memory --------------------------------------------------

    def seen_all(self) -> dict[str, str]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT card_id, status FROM last_seen").fetchall()
        finally:
            conn.close()
        return {r["card_id"]: r["status"] for r in rows}

    def seen_set(self, card_id: str, status: str) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO last_seen (card_id, status) VALUES (?,?)"
                " ON CONFLICT(card_id) DO UPDATE SET status=excluded.status",
                (card_id, status),
            )
            conn.commit()
        finally:
            conn.close()

    def seen_drop(self, card_ids: list[str]) -> None:
        if not card_ids:
            return
        conn = self._conn()
        try:
            conn.executemany("DELETE FROM last_seen WHERE card_id=?",
                             [(c,) for c in card_ids])
            conn.commit()
        finally:
            conn.close()
