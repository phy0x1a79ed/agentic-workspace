"""Read what an opencode session is doing and has taken in — from its SQLite DB.

The claude observation reads two files Claude Code writes about itself: a
per-pid record (status, on transitions) and a jsonl transcript (what the
session took in). OpenCode writes neither. Its state lives in a SQLite DB
(``~/.local/share/opencode/opencode.db``), and the observation must read it
from there, answering the *same questions* the claude tail answers so the one
watcher and one sender can run against either harness.

The shapes are not identical, and the differences are worth naming:

* **status** — Claude's record carries a closed vocabulary (``busy``/``shell``/
  ``idle``/``waiting``) written on transitions. OpenCode has no such field, so
  status is *derived*: a tool part still ``running``/``pending``, or a
  ``step-start`` with no matching ``step-finish``, means a turn is in flight
  (``busy``); otherwise the session is between turns (``idle``). ``waiting``
  has no opencode analogue and is never produced — which is safe, because the
  watcher treats it as *not settled*, and an idle-read-as-not-blocked is the
  direction that costs nothing.
* **started** — Claude Code writes a plain ``user`` line as a prompt is taken
  up. OpenCode records a ``user`` message whose text part is the command. That
  is the same "the command has begun" signal, dated by the message row.
* **compacted** — Claude Code writes a ``compact_boundary`` system entry.
  OpenCode writes a ``compaction`` part. Same role, different row.
* **queue** — Claude Code's queue has a rich vocabulary (enqueue/remove/
  dequeue); opencode's ``session_input`` table is empty and no queue semantics
  are observable. So ``queued`` is always ``None`` (unknown), which the watcher
  reads as not-settled for the ``shell`` path — the safe direction.

Every question answers ``None`` for "could not tell", the same stance
:func:`transcript.Tail` takes. An unreadable DB must leave a caller waiting,
never conclude that a session ignored us.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

from awm.reflection import oc_session

log = logging.getLogger("awm.reflection.oc_observe")

DB_PATH = oc_session.DB_PATH

# A tool part whose state has not reached a terminal status is in flight.
_ACTIVE_STATUS = ("pending", "running")


def _connect() -> sqlite3.Connection:
    """A read-only connection. The opencode server holds the DB in WAL mode, so
    a concurrent reader is safe — and reflection must never write to it."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_text(data) -> str:
    """The text of a ``user`` message's text part, or ``""``."""
    try:
        for part in data.get("parts") or []:
            if part.get("type") == "text":
                return (part.get("text") or "").strip()
    except AttributeError:
        pass
    return ""


def read_status(repl_pid: int) -> Optional[tuple[str, int]]:
    """``(status, updated_ms)`` for the opencode session ``repl_pid`` owns.

    ``None`` if the pid is not a live opencode session or the DB cannot be
    read this time — the same tri-state an unreadable Claude record gives.
    """
    resolved = oc_session.session_id_for(repl_pid)
    if resolved is None:
        return None
    session_id, _ = resolved
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT time_updated FROM session WHERE id = ?",
                (session_id,)).fetchone()
            if row is None:
                return None
            tool = conn.execute(
                "SELECT COUNT(*) FROM part WHERE session_id = ? AND "
                "json_extract(data, '$.type') = 'tool' AND "
                "json_extract(data, '$.state.status') IN (?, ?)",
                (session_id, *_ACTIVE_STATUS)).fetchone()[0]
            steps = conn.execute(
                "SELECT COUNT(*) FROM part WHERE session_id = ? AND "
                "json_extract(data, '$.type') = 'step-start'",
                (session_id,)).fetchone()[0]
            finishes = conn.execute(
                "SELECT COUNT(*) FROM part WHERE session_id = ? AND "
                "json_extract(data, '$.type') = 'step-finish'",
                (session_id,)).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("reflection: could not read the opencode session DB: %s", exc)
        return None
    status = "busy" if tool or steps > finishes else "idle"
    return status, int(row["time_updated"] or 0)


class OpencodeTail:
    """A transcript reader over one opencode session's DB rows.

    Mirrors :class:`transcript.Tail`'s interface — the one watcher and one
    sender must be able to ask either harness the same questions. Reads are
    incremental (the last-seen message id is remembered) because a watcher
    polls every couple of seconds against a session that grows all day.
    """

    def __init__(self, repl_pid: int, *, from_start: bool = False):
        self.repl_pid = repl_pid
        self._session_id: Optional[str] = None
        self._started: dict[str, int] = {}
        self._consumed: set[str] = set()
        self._compacted_at = 0
        self._ever_read = False
        self._watched: set[str] = set()

    # -- what we are looking for -------------------------------------------

    def watch(self, text: str) -> None:
        """Track ``text`` from here on; call before the text is typed in."""
        self._watched.add((text or "").strip())

    # -- reading ------------------------------------------------------------

    def _resolve(self) -> Optional[str]:
        """The session id, found (and re-found) by pid → cwd → DB."""
        if self._session_id is None:
            resolved = oc_session.session_id_for(self.repl_pid)
            if resolved is None:
                return None
            self._session_id, _ = resolved
        return self._session_id

    def poll(self) -> bool:
        """Ingest whatever has been appended since the last call.

        Returns ``False`` if the DB could not be read this time — evidence
        about the DB, not about the session.
        """
        session_id = self._resolve()
        if session_id is None:
            return False
        try:
            conn = _connect()
            try:
                parts = conn.execute(
                    "SELECT p.data, m.time_created AS msg_ts FROM part p "
                    "JOIN message m ON m.id = p.message_id "
                    "WHERE p.session_id = ? AND "
                    "json_extract(p.data, '$.type') IN ('text', 'compaction')",
                    (session_id,)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            log.warning("reflection: could not read the opencode session DB: %s", exc)
            return False
        self._ever_read = True
        for part in parts:
            self._ingest(part["data"], int(part["msg_ts"] or 0))
        return True

    def _ingest(self, raw: str, ts: int) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return
        kind = data.get("type")
        if kind == "text":
            text = (data.get("text") or "").strip()
            if text in self._watched:
                self._started[text] = max(self._started.get(text, 0), ts)
                self._consumed.add(text)
        elif kind == "compaction":
            self._compacted_at = max(self._compacted_at, ts)

    # -- questions ----------------------------------------------------------

    @property
    def readable(self) -> bool:
        """Whether this transcript has ever been read. ``False`` means silent."""
        return self._ever_read

    def consumed(self, text: str) -> Optional[bool]:
        """Has ``text`` been taken into the conversation? ``None`` if unknown."""
        if not self._ever_read:
            return None
        return (text or "").strip() in self._consumed

    def started(self, text: str, *, since_ms: Optional[int] = None) -> Optional[bool]:
        """Has the turn that runs ``text`` begun? ``None`` if unknown.

        A ``user`` message whose text part is the command is opencode writing
        that a prompt was taken up — the direct analogue of Claude Code's start
        line. ``since_ms`` bounds it to this command, not an identical one from
        an earlier compaction.
        """
        if not self._ever_read:
            return None
        stamp = self._started.get((text or "").strip())
        if stamp is None:
            return False
        return stamp > since_ms if since_ms is not None else True

    def compacted(self, *, since_ms: Optional[int] = None) -> Optional[bool]:
        """Has a compaction completed? ``None`` if unknown.

        The ``compaction`` part, which is the one place opencode says outright
        that a conversation was replaced — the ``compact_boundary`` analogue.
        """
        if not self._ever_read:
            return None
        if not self._compacted_at:
            return False
        return (self._compacted_at > since_ms if since_ms is not None else True)

    def queued(self, text: str) -> Optional[bool]:
        """Is ``text`` sitting in a queue? Always ``None`` for opencode.

        OpenCode's ``session_input`` table is empty; no queue semantics are
        observable, and the watcher reads ``None`` as not-settled for the
        ``shell`` path — the safe direction.
        """
        return None

    def landed(self, text: str) -> Optional[bool]:
        """Did ``text`` reach the session at all? ``None`` if unknown."""
        if not self._ever_read:
            return None
        return (text or "").strip() in self._consumed

    def tool_call_in_flight(self) -> Optional[bool]:
        """Is a tool still running? ``None`` if the DB was unreadable."""
        if not self._ever_read:
            return None
        session_id = self._session_id
        if session_id is None:
            return None
        try:
            conn = _connect()
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM part WHERE session_id = ? AND "
                    "json_extract(data, '$.type') = 'tool' AND "
                    "json_extract(data, '$.state.status') IN (?, ?)",
                    (session_id, *_ACTIVE_STATUS)).fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        return n > 0


class OpencodeObservation:
    """The opencode implementation of the core's :class:`Observation` seam."""

    def __init__(self, repl_pid: int, session_id: Optional[str] = None):
        self.repl_pid = repl_pid
        self._session_id = session_id

    def read_status(self, repl_pid: int):
        return read_status(repl_pid)

    def open_tail(self, repl_pid: int):
        return OpencodeTail(repl_pid)


def observation_for(repl_pid: int) -> OpencodeObservation:
    """The opencode observation for a caller pid."""
    return OpencodeObservation(repl_pid)