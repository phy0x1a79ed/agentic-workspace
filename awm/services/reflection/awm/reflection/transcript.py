"""Read what a session actually did with the text we typed into it.

The session record says *what a session is doing*; its transcript says *what it
has taken in*. Reflection needs both, and for one reason: ``status`` cannot tell
a foreground tool call from a background one. A session holding a Monitor or a
``run_in_background`` job reports ``shell`` continuously after its turn has
ended — so a watcher that waits for ``idle`` waits for something that will not
happen until the background job does, which in an autopilot session may be never.

The transcript settles it. Claude Code appends one JSON object per line to
``~/.claude/projects/<cwd-slug>/<session-id>.jsonl``, and a foreground tool call
leaves an ``assistant`` ``tool_use`` block with no matching ``tool_result``
behind it. A background task does not: it returns its result immediately (a task
id) and reports later. So ``shell`` *plus* a clean tail means the foreground is
free.

The same stream says whether a line we typed was taken in, which the record
cannot say at all:

* a plain ``user`` message carrying the text — it went straight in;
* ``queue-operation enqueue`` — it reached the composer while the session was
  busy and is now queued;
* ``queue-operation remove`` with that content, alongside an ``attachment`` of
  type ``queued_command`` carrying it as ``prompt`` — the queue handed it to the
  running turn. ``remove`` is the *delivery* path, not a drop;
* ``queue-operation dequeue`` — the whole queue drained into the next turn. It
  carries no content, so it settles every line queued up to that point.

Every question here is phrased as a positive observation, and every one answers
``None`` for "could not tell". An unreadable transcript must leave a caller
waiting, never conclude that a session ignored us — the same stance
:func:`watcher.read_status` takes for an unreadable record.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from awm.reflection import session_target

log = logging.getLogger("awm.reflection.transcript")

# Overridable for tests, like session_target.SESSIONS_DIR.
PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))

# How far back the first poll reaches. It has to cover the turn in progress —
# otherwise a tool call already running when we opened would look like no tool
# call at all — and nothing beyond that is any of our business. A megabyte is
# several turns of a talkative session.
MAX_CATCHUP_BYTES = 1024 * 1024

_QUEUE_CONSUMES = ("remove", "popAll")


def session_id_for(repl_pid: int) -> Optional[str]:
    """The session id Claude Code is *currently* writing under, or ``None``.

    Re-read rather than cached: ``/clear`` replaces a conversation in place and
    mints a new id, so an id captured when a wait started can name a transcript
    that has stopped growing.
    """
    try:
        record = json.loads(
            (session_target.SESSIONS_DIR / f"{repl_pid}.json").read_text())
    except (OSError, ValueError):
        return None
    session_id = record.get("sessionId")
    return str(session_id) if session_id else None


def path_for(session_id: str) -> Optional[Path]:
    """The transcript file for ``session_id``, found by search rather than rule.

    Claude Code files transcripts under a slug derived from the session's cwd.
    Deriving that slug here would be a second copy of somebody else's rule, and
    a session that changes directory would break it; a glob asks the filesystem
    instead.
    """
    if not session_id:
        return None
    try:
        matches = sorted(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return None
    return matches[0] if matches else None


def _text_of(entry: dict) -> str:
    """The user-visible text of a ``user`` entry, or ``""`` for a tool result."""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text") or ""
            for block in content
            if isinstance(block, dict) and block.get("type") == "text")
    return ""


class Tail:
    """Follows one session's transcript from wherever it stood when opened.

    Opened at (or just before) the moment something is typed in, so that the
    events it sees are exactly the session's reaction to it. Reads are
    incremental — the offset is remembered between polls — because a watcher
    calls :meth:`poll` every couple of seconds against a file that grows all
    session.
    """

    def __init__(self, repl_pid: int, *, from_start: bool = False):
        self.repl_pid = repl_pid
        self._session_id: Optional[str] = None
        self._path: Optional[Path] = None
        self._offset = 0 if from_start else None
        self._partial = ""
        self._ever_read = False
        # Only text we have been asked about is retained, so following a session
        # for an hour costs a fixed amount of memory rather than its transcript.
        self._watched: set[str] = set()
        self._queued: set[str] = set()
        self._consumed: set[str] = set()
        self._open_tools: set[str] = set()

    # -- what we are looking for -------------------------------------------

    def watch(self, text: str) -> None:
        """Track ``text`` from here on; call before the text is typed in."""
        self._watched.add((text or "").strip())

    # -- reading ------------------------------------------------------------

    def _resolve(self) -> Optional[Path]:
        session_id = session_id_for(self.repl_pid)
        if session_id and session_id != self._session_id:
            # A /clear mid-wait: the old file stops growing and a new one starts
            # empty, so follow the new one from its beginning.
            self._session_id = session_id
            self._path = path_for(session_id)
            self._offset = 0
            self._partial = ""
        elif self._path is None and session_id:
            self._path = path_for(session_id)
        return self._path

    def poll(self) -> bool:
        """Ingest whatever has been appended since the last call.

        Returns ``False`` if the transcript could not be read this time, which
        is not evidence about the session — only about the file.
        """
        path = self._resolve()
        if path is None:
            return False
        try:
            size = path.stat().st_size
            with path.open("r", encoding="utf-8", errors="replace") as fp:
                if self._offset is None:
                    # First poll of a live session: start at the end, and only
                    # walk back far enough to catch an injection in progress.
                    self._offset = max(0, size - MAX_CATCHUP_BYTES)
                    if self._offset:
                        fp.seek(self._offset)
                        fp.readline()  # drop the partial line we landed inside
                        self._offset = fp.tell()
                    else:
                        fp.seek(0)
                elif self._offset > size:
                    # Truncated or replaced under us; re-read from the top.
                    self._offset = 0
                    self._partial = ""
                    fp.seek(0)
                else:
                    fp.seek(self._offset)
                chunk = fp.read()
                self._offset = fp.tell()
        except OSError:
            return False

        self._ever_read = True
        data = self._partial + chunk
        lines = data.split("\n")
        # The last element is whatever follows the final newline: either empty,
        # or a line still being written. Either way it is not ours to parse yet.
        self._partial = lines.pop()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                self._ingest(entry)
        return True

    def _ingest(self, entry: dict) -> None:
        kind = entry.get("type")

        # A subagent's own turns are written into the same file. They are not
        # this session's foreground work — and the Agent call that started one
        # is itself an unmatched tool_use, so nothing is missed by skipping them.
        if entry.get("isSidechain"):
            return

        if kind == "assistant":
            for block in (entry.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    if block.get("id"):
                        self._open_tools.add(str(block["id"]))
            return

        if kind == "user":
            content = (entry.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._open_tools.discard(str(block.get("tool_use_id")))
            text = _text_of(entry).strip()
            if text and text in self._watched:
                self._consumed.add(text)
                self._queued.discard(text)
            return

        if kind == "queue-operation":
            op = entry.get("operation")
            text = (entry.get("content") or "").strip()
            if op == "enqueue":
                if text in self._watched:
                    self._queued.add(text)
            elif op in _QUEUE_CONSUMES:
                if text in self._watched:
                    self._consumed.add(text)
                    self._queued.discard(text)
            elif op == "dequeue":
                # Carries no content: the queue drained wholesale into the turn
                # that is starting, so everything outstanding went with it.
                self._consumed |= self._queued
                self._queued.clear()
            return

        if kind == "attachment":
            attachment = entry.get("attachment")
            if not isinstance(attachment, dict):
                return
            if attachment.get("type") == "queued_command":
                text = (attachment.get("prompt") or "").strip()
                if text in self._watched:
                    self._consumed.add(text)
                    self._queued.discard(text)

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

    def queued(self, text: str) -> Optional[bool]:
        """Is ``text`` sitting in the session's queue? ``None`` if unknown."""
        if not self._ever_read:
            return None
        return (text or "").strip() in self._queued

    def landed(self, text: str) -> Optional[bool]:
        """Did ``text`` reach the session at all — consumed *or* queued?

        This is the question a sender asks: the keystrokes arrived and the
        session acknowledged them. Whether the queue has handed them to a turn
        yet is :meth:`consumed`.
        """
        if not self._ever_read:
            return None
        text = (text or "").strip()
        return text in self._consumed or text in self._queued

    def tool_call_in_flight(self) -> Optional[bool]:
        """Is a *foreground* tool call outstanding? ``None`` if unknown.

        The distinction the session record cannot make. An unmatched ``tool_use``
        at the tail means the session is mid-turn waiting on a tool; a background
        task leaves none, because its ``tool_result`` came back the moment it was
        launched.
        """
        if not self._ever_read:
            return None
        return bool(self._open_tools)
