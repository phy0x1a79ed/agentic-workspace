"""The harness seam: how a session's observation and its transcript are read.

The core — ``watcher``, ``transcript``, ``inject`` — is harness-agnostic in
spirit but was for a long time exercised against exactly one harness. The two
places it reaches into the harness are (1) reading the session's *status record*
and (2) opening its *transcript*. An :class:`Observation` answers both, and
:func:`observation_for` picks the right one for a caller's pid.

Claude Code writes ``~/.claude/sessions/<pid>.json`` (status, on transitions)
and appends one JSON object per line to ``~/.claude/projects/<cwd>/<sid>.jsonl``
(what the session took in). :class:`ClaudeObservation` is a thin wrapper over
the exact reads the core always made — it must stay behaviourally identical,
because the whole existing suite pins it.

OpenCode has neither file: its sessions and their parts live in a SQLite DB
(``~/.local/share/opencode/opencode.db``), and its transcript is rows in that
DB rather than a jsonl stream. The opencode observation arrives with the
adapter that reads it; this module only fixes the shape both have to answer.

The watcher and transcript modules must not import this one: ``observation``
wraps *their* functions, so the dependency points this way and this way only.
"""
from __future__ import annotations

from typing import Protocol

from awm.reflection import transcript, watcher


class Observation(Protocol):
    """Answers the two harness-questions the core asks of a session."""

    def read_status(self, repl_pid: int):  # pragma: no cover - protocol
        """The session's ``(status, statusUpdatedAt)``, or ``None`` unreadable."""
        ...

    def open_tail(self, repl_pid: int):  # pragma: no cover - protocol
        """A transcript reader over the session's own record of what it took in."""
        ...


class ClaudeObservation:
    """Observes a Claude Code session: its per-pid record + jsonl transcript."""

    def read_status(self, pid: int):
        return watcher.read_status(pid)

    def open_tail(self, pid: int):
        return transcript.Tail(pid)


def observation_for(pid: int) -> Observation:
    """The observation appropriate to the caller's harness.

    Claude Code wins when it can: a process either has a per-pid session record
    or it does not, and the record is the more precise identity. Only a pid with
    no Claude record *and* a positive opencode exe is handed to the opencode
    observation. Anything else — a fake pid in a test, a process that is
    neither — stays on the Claude observation, whose reader is the module
    function the whole existing suite pins.
    """
    from awm.reflection import oc_session, session_target
    if _is_claude(pid):
        return ClaudeObservation()
    if oc_session._is_opencode(pid):
        from awm.reflection import oc_observe
        return oc_observe.observation_for(pid)
    return ClaudeObservation()


def _is_claude(pid: int) -> bool:
    """Does a Claude Code per-pid session record exist for ``pid``?

    Existence alone, deliberately — the same narrowing step the gateway's
    ancestry walk uses. The record's contents (and its procStart against the
    live process) are checked where it matters: when reflection actually
    injects.
    """
    from awm.reflection import session_target
    try:
        return (session_target.SESSIONS_DIR / f"{pid}.json").exists()
    except OSError:
        return False