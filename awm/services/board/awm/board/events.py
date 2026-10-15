"""The event log: an append-only record that exists so a stream can resume.

The vault is the source of truth for cards. Nothing here is read to decide what
a card is, only to tell a reconnecting client what happened while it was away.
The log also holds the board's memory of the last state it saw for each card,
which the watcher compares against the vault to notice an edit made outside
this process.

Card messages are the exception: the `messages` table is their record, and each
message's Trilium note is a mirror the board restores from it. Trilium enforces
`#readOnly` only in its GUI and knows no author but the vault's operator, so a
note alone could be neither final nor attributed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from awm.persistence import databases

from .parties import OPEN

TYPES = ("card.posted", "card.claimed", "card.completed", "card.failed", "card.moved",
         "card.message")

#: Events a single `since` call returns at most. A stream calls again from its cursor.
DEFAULT_LIMIT = 500

_INDEXES = """
CREATE INDEX IF NOT EXISTS events_sender ON events (sender, id);
CREATE INDEX IF NOT EXISTS events_recipient ON events (recipient, id);
CREATE INDEX IF NOT EXISTS events_created ON events (created_at);
"""

_MESSAGES_V3 = """
CREATE TABLE IF NOT EXISTS messages (
    card_id   TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    at        TEXT NOT NULL,
    swarm     TEXT NOT NULL,
    principal TEXT NOT NULL,
    party     TEXT NOT NULL,
    origin    TEXT NOT NULL,
    body      TEXT NOT NULL,
    note_id   TEXT,
    PRIMARY KEY (card_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_note ON messages (note_id);
"""

#: The reply cards the thread migration has folded into a root card's thread.
_FOLDED = """
CREATE TABLE IF NOT EXISTS folded (
    reply_id TEXT PRIMARY KEY,
    root_id  TEXT NOT NULL,
    at       REAL NOT NULL
);
"""

_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    card_id   TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    at        TEXT NOT NULL,
    swarm     TEXT NOT NULL,
    principal TEXT NOT NULL,
    party     TEXT NOT NULL,
    origin    TEXT NOT NULL,
    body      TEXT NOT NULL,
    note_id   TEXT,
    agent     TEXT,
    PRIMARY KEY (card_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_note ON messages (note_id);
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
""" + _INDEXES + _MESSAGES + _FOLDED

_MIGRATIONS = {
    (1, 2): ("ALTER TABLE last_seen ADD COLUMN recipient TEXT;\n"
             "ALTER TABLE last_seen ADD COLUMN claimant TEXT;\n" + _INDEXES),
    (2, 3): _MESSAGES_V3,
    (3, 4): "ALTER TABLE messages ADD COLUMN agent TEXT;\n" + _FOLDED,
}

_MESSAGE_COLUMNS = "card_id, seq, at, swarm, principal, party, origin, body, note_id, agent"


def _message(row: sqlite3.Row) -> dict:
    return {"card_id": row["card_id"], "seq": row["seq"], "at": row["at"],
            "sender": {"swarm": row["swarm"], "principal": row["principal"],
                       "party": row["party"]},
            "origin": row["origin"], "body": row["body"], "note_id": row["note_id"],
            "agent": row["agent"]}


class Events:
    """The event log in a SQLite file at ``path``."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        databases.init_db_at(self._path, _SCHEMA, schema_version=4, migrations=_MIGRATIONS)

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

    def for_card(self, card_id: str, party: dict, since: int = 0,
                 limit: int = DEFAULT_LIMIT) -> list[dict]:
        """One card's events after ``since`` that ``party`` may see, oldest first. Same filter as `since`."""
        swarm = party["swarm"]
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, type, card, created_at FROM events"
                " WHERE card_id = ? AND id > ? AND (sender = ? OR recipient IN (?, ?))"
                " ORDER BY id LIMIT ?",
                (card_id, int(since or 0), swarm, swarm, OPEN, max(1, int(limit))),
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

    # -- card messages ----------------------------------------------------------

    def message_add(self, card_id: str, *, at: str, sender: dict, origin: str,
                    body: str, note_id: str | None = None, agent: str | None = None) -> dict:
        """Record a message as the card's next one. The sequence is the order of the thread."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO messages (" + _MESSAGE_COLUMNS + ")"
                " SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ?"
                " FROM messages WHERE card_id = ?",
                (card_id, at, sender["swarm"], sender["principal"], sender["party"],
                 origin, body, note_id, agent, card_id))
            row = conn.execute(
                "SELECT " + _MESSAGE_COLUMNS + " FROM messages WHERE card_id = ?"
                " ORDER BY seq DESC LIMIT 1", (card_id,)).fetchone()
            conn.commit()
        finally:
            conn.close()
        return _message(row)

    def message_set_note(self, card_id: str, seq: int, note_id: str) -> None:
        conn = self._conn()
        try:
            conn.execute("UPDATE messages SET note_id = ? WHERE card_id = ? AND seq = ?",
                         (note_id, card_id, seq))
            conn.commit()
        finally:
            conn.close()

    def messages(self, card_id: str, after: int = 0, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """The card's messages after sequence ``after``, in sequence order."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT " + _MESSAGE_COLUMNS + " FROM messages"
                " WHERE card_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (card_id, int(after or 0), max(1, int(limit)))).fetchall()
        finally:
            conn.close()
        return [_message(r) for r in rows]

    def message_by_note(self, note_id: str) -> dict | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT " + _MESSAGE_COLUMNS + " FROM messages WHERE note_id = ?",
                               (note_id,)).fetchone()
        finally:
            conn.close()
        return _message(row) if row else None

    def message_page(self, after_rowid: int, limit: int) -> tuple[list[dict], int]:
        """A page of every message in insertion order, and the cursor for the next page (0 at the end)."""
        limit = max(1, int(limit))
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT rowid, " + _MESSAGE_COLUMNS + " FROM messages WHERE rowid > ?"
                " ORDER BY rowid LIMIT ?", (int(after_rowid), limit)).fetchall()
        finally:
            conn.close()
        cursor = rows[-1]["rowid"] if len(rows) == limit else 0
        return [_message(r) for r in rows], cursor

    # -- the thread migration ---------------------------------------------------

    def folded_ids(self, reply_ids: list[str]) -> set[str]:
        """Which of ``reply_ids`` the thread migration has already folded into a thread."""
        if not reply_ids:
            return set()
        conn = self._conn()
        try:
            marks = ",".join("?" * len(reply_ids))
            rows = conn.execute(f"SELECT reply_id FROM folded WHERE reply_id IN ({marks})",
                                list(reply_ids)).fetchall()
        finally:
            conn.close()
        return {r["reply_id"] for r in rows}

    def messages_backfill(self, root_id: str, history: list[dict]) -> list[dict]:
        """Put ``history`` at the head of the root card's thread, in one transaction.

        Each entry is ``{reply_id, at, sender, body}``, already in the order it
        should read. The messages the thread holds keep their order and move up
        by ``len(history)``, so every backfilled sequence number sorts before
        them. Two passes through negative numbers keep the primary key unique
        while the rows shift. Returns the whole thread afterwards.
        """
        shift = len(history)
        conn = self._conn()
        try:
            conn.execute("UPDATE messages SET seq = -(seq + ?) WHERE card_id = ?",
                         (shift, root_id))
            conn.execute("UPDATE messages SET seq = -seq WHERE card_id = ? AND seq < 0",
                         (root_id,))
            for seq, item in enumerate(history, start=1):
                sender = item["sender"]
                conn.execute(
                    "INSERT INTO messages (" + _MESSAGE_COLUMNS + ")"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (root_id, seq, item["at"], sender["swarm"], sender["principal"],
                     sender["party"], "migrated", item["body"], None, None))
                conn.execute("INSERT INTO folded (reply_id, root_id, at) VALUES (?,?,?)",
                             (item["reply_id"], root_id, time.time()))
            conn.commit()
            rows = conn.execute(
                "SELECT " + _MESSAGE_COLUMNS + " FROM messages WHERE card_id = ? ORDER BY seq",
                (root_id,)).fetchall()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return [_message(r) for r in rows]
