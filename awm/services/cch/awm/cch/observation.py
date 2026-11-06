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

The watcher and transcript modules must not import this one: ``observation``
wraps *their* functions, so the dependency points this way and this way only.
"""
from __future__ import annotations

from typing import Protocol

from awm.cch import transcript, watcher


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
    """The observation for ``pid``. Every session cch serves is a Claude Code one."""
    return ClaudeObservation()

