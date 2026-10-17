"""The queue: the cards the door has seen, the stream cursor and the card watchers.

It shares one SQLite file with the task ledger (`awm.reporting.ledger`) so a
card write and a cursor write can never disagree about which file survived. The
subscriber saves the cursor only after the card write has committed; a crash
between the two replays an event the queue absorbs, because every write is
keyed by card id.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

from awm.persistence import databases

from awm.reporting import ACTIVE, ASSIGNED, GONE, PROPOSED, STATUSES, TERMINAL

_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_id      TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    title        TEXT NOT NULL,
    sender       TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    assigned_to  TEXT,
    assigned_job TEXT,
    arrival      REAL NOT NULL,
    updated      REAL NOT NULL,
    notified     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS cards_status ON cards (status, arrival);
CREATE TABLE IF NOT EXISTS threads (
    card_id  TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchers (
    job    TEXT NOT NULL,
    target TEXT NOT NULL,
    since  REAL NOT NULL,
    PRIMARY KEY (job, target)
);
"""

_TASKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    node       TEXT NOT NULL,
    key        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    pid        INTEGER,
    session_id TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL DEFAULT '',
    project    TEXT,
    scope      TEXT,
    model      TEXT,
    effort     TEXT,
    state      TEXT NOT NULL,
    note_id    TEXT,
    shown      TEXT,
    dirty      INTEGER NOT NULL DEFAULT 0,
    plan       TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    urgent     INTEGER NOT NULL DEFAULT 0,
    blocked_at REAL,
    reminded   INTEGER NOT NULL DEFAULT 0,
    ended      INTEGER NOT NULL DEFAULT 0,
    owned      INTEGER NOT NULL DEFAULT 0,
    card_id    TEXT,
    fed_state  TEXT,
    agent      TEXT,
    agent_link TEXT,
    started    REAL NOT NULL,
    updated    REAL NOT NULL,
    closed     REAL,
    UNIQUE (node, key)
);
CREATE TABLE IF NOT EXISTS task_aliases (
    task_id    INTEGER NOT NULL REFERENCES tasks (id),
    session_id TEXT NOT NULL,
    PRIMARY KEY (task_id, session_id)
);
CREATE TABLE IF NOT EXISTS task_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks (id),
    at      REAL NOT NULL,
    kind    TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '',
    ref     TEXT,
    UNIQUE (task_id, ref)
);
CREATE TABLE IF NOT EXISTS todos (
    task_id    INTEGER NOT NULL REFERENCES tasks (id),
    item_id    TEXT NOT NULL,
    subject    TEXT NOT NULL,
    status     TEXT NOT NULL,
    first_seen REAL NOT NULL,
    removed_at REAL,
    PRIMARY KEY (task_id, item_id)
);
"""

SCHEMA_VERSION = 7

_FED_COLUMNS = "    card_id    TEXT,\n    fed_state  TEXT,\n"
_AGENT_COLUMNS = "    agent      TEXT,\n    agent_link TEXT,\n"
_TASKS_SCHEMA_V2 = (_TASKS_SCHEMA.replace("    owned      INTEGER NOT NULL DEFAULT 0,\n", "")
                    .replace(_FED_COLUMNS, "").replace(_AGENT_COLUMNS, ""))

_OWNED_MIGRATION = """
ALTER TABLE tasks ADD COLUMN owned INTEGER NOT NULL DEFAULT 0;
UPDATE tasks SET owned = 1 WHERE NOT EXISTS (
    SELECT 1 FROM task_events WHERE task_events.task_id = tasks.id AND task_events.kind = 'claimed');
"""

_SPEC_HASH_MIGRATION = "ALTER TABLE sessions ADD COLUMN spec_hash TEXT;"

_THREADS_MIGRATION = """
ALTER TABLE cards ADD COLUMN assigned_job TEXT;
ALTER TABLE cards ADD COLUMN thread_open INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS threads (
    card_id  TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL
);
"""

#: The persona queue becomes the intake record: a card nobody holds is
#: `proposed` and is told to Tony again, and the persona columns and sessions go.
_INTAKE_MIGRATION = """
UPDATE cards SET status = 'proposed', notified = 0 WHERE status IN ('claiming', 'queued');
UPDATE cards SET status = 'rejected' WHERE status = 'failed';
ALTER TABLE cards DROP COLUMN priority;
ALTER TABLE cards DROP COLUMN body;
ALTER TABLE cards DROP COLUMN reply_to;
ALTER TABLE cards DROP COLUMN thread_open;
ALTER TABLE cards DROP COLUMN announced_at;
DROP TABLE IF EXISTS sessions;
CREATE TABLE IF NOT EXISTS watchers (
    job    TEXT NOT NULL,
    target TEXT NOT NULL,
    since  REAL NOT NULL,
    PRIMARY KEY (job, target)
);
ALTER TABLE tasks ADD COLUMN card_id TEXT;
ALTER TABLE tasks ADD COLUMN fed_state TEXT;
"""

#: The session working a task, as its card's Agent line shows it.
_AGENT_MIGRATION = """
ALTER TABLE tasks ADD COLUMN agent TEXT;
ALTER TABLE tasks ADD COLUMN agent_link TEXT;
"""

_CURSOR = "cursor"


class Db:
    """The service's one SQLite file, opened at the current schema version."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        databases.init_db_at(self.path, _QUEUE_SCHEMA + _TASKS_SCHEMA,
                             schema_version=SCHEMA_VERSION,
                             migrations={(1, 2): _TASKS_SCHEMA_V2, (2, 3): _OWNED_MIGRATION,
                                         (3, 4): _SPEC_HASH_MIGRATION,
                                         (4, 5): _THREADS_MIGRATION,
                                         (5, 6): _INTAKE_MIGRATION,
                                         (6, 7): _AGENT_MIGRATION})

    def _conn(self) -> sqlite3.Connection:
        return databases.get_connection_at(self.path)


class Offloaded:
    """Runs a `Db`'s methods on a worker thread, as coroutines.

    Every call opens a connection and commits, and on this machine's root disk
    an fsync can stall for tens of seconds. The service's event loop also
    answers the gateway's control-WS keepalive pings, so a commit made on the
    loop gets the lease closed. Code on the loop calls the database through
    this; the object it wraps stays the one with plain methods.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._db, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.to_thread(method, *args, **kwargs)

        return call


class Queue(Db):
    """The door's durable state."""

    # -- cards ---------------------------------------------------------------

    def enqueue(self, card: dict[str, Any], *, quiet: bool = False, reopen: bool = False) -> bool:
        """Record a card as ``proposed``, idempotently by card id. True when it is new or reopened.

        A card already in the queue keeps its status and assignment, and its
        title is refreshed. ``reopen`` puts a finished card back, which is what
        a drag back to Proposed on the board means: the claim is gone and Tony
        is told again. ``quiet`` records the card as already told, for a card
        too old to be news.
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
                        "INSERT INTO cards (card_id, kind, title, sender, status, arrival,"
                        " updated, notified) VALUES (?,?,?,?,?,?,?,?)",
                        (card["id"], *fields, PROPOSED, now, now, int(quiet)))
                    return True
                conn.execute("UPDATE cards SET kind=?, title=?, sender=?, updated=?"
                             " WHERE card_id = ?", (*fields, now, card["id"]))
                if reopen and row["status"] in TERMINAL + (ASSIGNED,):
                    conn.execute(
                        "UPDATE cards SET status=?, assigned_to=NULL, assigned_job=NULL,"
                        " notified=0 WHERE card_id=?", (PROPOSED, card["id"]))
                    return True
                return False
        finally:
            conn.close()

    def take(self, card: dict[str, Any], agent: str, job: str | None) -> None:
        """Record that a local session claimed the card. ``job`` is the session new messages wake."""
        now = time.time()
        fields = _fields(card)
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO cards (card_id, kind, title, sender, status, assigned_to,"
                    " assigned_job, arrival, updated, notified) VALUES (?,?,?,?,?,?,?,?,?,1)"
                    " ON CONFLICT(card_id) DO UPDATE SET status=excluded.status,"
                    " assigned_to=excluded.assigned_to, assigned_job=excluded.assigned_job,"
                    " title=excluded.title, updated=excluded.updated",
                    (card["id"], *fields, ASSIGNED, agent, job, now, now))
        finally:
            conn.close()

    def release(self, card_id: str) -> None:
        """Forget the local assignment of a card: it is proposed again."""
        conn = self._conn()
        try:
            with conn:
                conn.execute("UPDATE cards SET status=?, assigned_to=NULL, assigned_job=NULL,"
                             " updated=? WHERE card_id=? AND status=?",
                             (PROPOSED, time.time(), card_id, ASSIGNED))
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

        ``gone`` applies only to a card still open: a card that finished is not
        made gone by a later re-address.
        """
        if status not in TERMINAL:
            raise ValueError(f"mark takes {TERMINAL}, not {status!r}")
        guard = "status IN (?,?)" if status == GONE else "status != ?"
        args = (ACTIVE if status == GONE else (status,))
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    f"UPDATE cards SET status=?, updated=? WHERE card_id=? AND {guard}",
                    (status, time.time(), card_id, *args))
                return cur.rowcount > 0
        finally:
            conn.close()

    def get(self, card_id: str) -> dict | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
        finally:
            conn.close()
        return _public(row) if row else None

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

    def needs_dm(self, card_id: str) -> bool:
        """Whether the card is proposed and Tony has not been told of it."""
        conn = self._conn()
        try:
            row = conn.execute("SELECT status, notified FROM cards WHERE card_id = ?",
                               (card_id,)).fetchone()
        finally:
            conn.close()
        return bool(row) and row["status"] == PROPOSED and not row["notified"]

    def mark_notified(self, card_id: str) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute("UPDATE cards SET notified=1 WHERE card_id=?", (card_id,))
        finally:
            conn.close()

    # -- threads -------------------------------------------------------------

    def thread_seq(self, card_id: str) -> int:
        """The last message sequence the door routed on ``card_id``. 0 for none."""
        conn = self._conn()
        try:
            row = conn.execute("SELECT last_seq FROM threads WHERE card_id=?", (card_id,)).fetchone()
        finally:
            conn.close()
        return int(row["last_seq"]) if row else 0

    def thread_seqs(self) -> dict[str, int]:
        conn = self._conn()
        try:
            return {r["card_id"]: int(r["last_seq"])
                    for r in conn.execute("SELECT card_id, last_seq FROM threads")}
        finally:
            conn.close()

    def set_thread_seq(self, card_id: str, seq: int) -> None:
        """Move the card's thread cursor forward. It never moves back."""
        conn = self._conn()
        try:
            with conn:
                conn.execute("INSERT INTO threads (card_id, last_seq) VALUES (?, ?)"
                             " ON CONFLICT(card_id) DO UPDATE SET last_seq = excluded.last_seq"
                             " WHERE excluded.last_seq > threads.last_seq", (card_id, int(seq)))
        finally:
            conn.close()

    # -- watchers ------------------------------------------------------------

    def watch(self, job: str, target: str) -> bool:
        """Record that ``job`` follows ``target``: a card id, or ``posted:<agent>``. True if new."""
        conn = self._conn()
        try:
            with conn:
                return conn.execute("INSERT OR IGNORE INTO watchers (job, target, since)"
                                    " VALUES (?,?,?)", (job, target, time.time())).rowcount > 0
        finally:
            conn.close()

    def unwatch(self, job: str, target: str | None = None) -> int:
        """Stop ``job`` following ``target``, or everything. Returns the number removed."""
        conn = self._conn()
        try:
            with conn:
                if target is None:
                    return conn.execute("DELETE FROM watchers WHERE job=?", (job,)).rowcount
                return conn.execute("DELETE FROM watchers WHERE job=? AND target=?",
                                    (job, target)).rowcount
        finally:
            conn.close()

    def watched_by(self, job: str) -> list[str]:
        conn = self._conn()
        try:
            return [r["target"] for r in conn.execute(
                "SELECT target FROM watchers WHERE job=? ORDER BY since", (job,))]
        finally:
            conn.close()

    def watchers_of(self, card_id: str, posted_by: str | None = None) -> list[str]:
        """The jobs following a card by id, or following every card ``posted_by`` posted."""
        targets = [card_id] + ([f"posted:{posted_by}"] if posted_by else [])
        marks = ",".join("?" for _ in targets)
        conn = self._conn()
        try:
            return [r["job"] for r in conn.execute(
                f"SELECT DISTINCT job FROM watchers WHERE target IN ({marks}) ORDER BY job", targets)]
        finally:
            conn.close()

    def prune_watchers(self, live_jobs: set[str]) -> int:
        """Drop the watchers whose job is not in ``live_jobs``. Returns the number dropped."""
        conn = self._conn()
        try:
            with conn:
                jobs = [r["job"] for r in conn.execute("SELECT DISTINCT job FROM watchers")]
                gone = [j for j in jobs if j not in live_jobs]
                conn.executemany("DELETE FROM watchers WHERE job=?", [(j,) for j in gone])
                return len(gone)
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


def _fields(card: dict[str, Any]) -> tuple:
    return (
        str(card.get("kind") or "request"),
        str(card.get("title") or ""),
        _sender(card),
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
    out["arrival_at"] = _iso(out["arrival"])
    out["updated_at"] = _iso(out["updated"])
    return out
