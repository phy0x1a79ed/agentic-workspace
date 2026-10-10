"""The event log: an append-only record that exists so a stream can resume.

The vault is the source of truth for cards. Nothing here is read to decide what
a card is, only to tell a reconnecting client what happened while it was away.
The log also holds the board's memory of the last state it saw for each card,
which the watcher compares against the vault to notice an edit made outside
this process.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from awm.persistence import databases

from .parties import OPEN

TYPES = ("card.posted", "card.claimed", "card.completed", "card.failed", "card.moved")

#: Events a single `since` call returns at most. A stream calls again from its cursor.
DEFAULT_LIMIT = 500

_INDEXES = """
CREATE INDEX IF NOT EXISTS events_sender ON events (sender, id);
CREATE INDEX IF NOT EXISTS events_recipient ON events (recipient, id);
CREATE INDEX IF NOT EXISTS events_created ON events (created_at);
"""

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
    card_id   TEXT PRIMARY KEY,
    status    TEXT NOT NULL,
    recipient TEXT,
    claimant  TEXT
);
""" + _INDEXES

_MIGRATIONS = {
    (1, 2): ("ALTER TABLE last_seen ADD COLUMN recipient TEXT;\n"
             "ALTER TABLE last_seen ADD COLUMN claimant TEXT;\n" + _INDEXES),
}


class Events:
    """The event log in a SQLite file at ``path``."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        databases.init_db_at(self._path, _SCHEMA, schema_version=2, migrations=_MIGRATIONS)

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

    def since(self, last_id: int, party: dict, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """Up to ``limit`` events after ``last_id`` that ``party`` may see, oldest first.

        The filter is the same rule as `parties.can_see`, written in SQL so a
        quiet party does not read everyone else's events to find its own.
        """
        swarm = party["swarm"]
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, type, card, created_at FROM events"
                " WHERE id > ? AND (sender = ? OR recipient IN (?, ?))"
                " ORDER BY id LIMIT ?",
                (int(last_id or 0), swarm, swarm, OPEN, max(1, int(limit))),
            ).fetchall()
        finally:
            conn.close()
        return [{"id": r["id"], "type": r["type"], "card": json.loads(r["card"]),
                 "created_at": r["created_at"]} for r in rows]

    def latest_id(self) -> int:
        conn = self._conn()
        try:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM events").fetchone()
        finally:
            conn.close()
        return int(row["n"])

    def oldest_id(self) -> int:
        """The smallest retained event id, or 0 when the log is empty."""
        conn = self._conn()
        try:
            row = conn.execute("SELECT COALESCE(MIN(id), 0) AS n FROM events").fetchone()
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

    def seen_all(self) -> dict[str, dict]:
        """card id to ``{status, recipient, claimant}``. The last two are None in rows an older schema wrote."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT card_id, status, recipient, claimant FROM last_seen").fetchall()
        finally:
            conn.close()
        return {r["card_id"]: {"status": r["status"], "recipient": r["recipient"],
                               "claimant": r["claimant"]} for r in rows}

    def seen_set(self, card_id: str, status: str, recipient: str | None = None,
                 claimant: str | None = None) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO last_seen (card_id, status, recipient, claimant) VALUES (?,?,?,?)"
                " ON CONFLICT(card_id) DO UPDATE SET status=excluded.status,"
                " recipient=excluded.recipient, claimant=excluded.claimant",
                (card_id, status, recipient, claimant),
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
