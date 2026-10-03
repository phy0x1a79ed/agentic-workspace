"""Where Claude Code keeps session logs, and what one session's files are.

Claude Code writes each session as a transcript file beside a sidecar directory
of the same name::

    ~/.claude/projects/<project-slug>/
        <sessionId>.jsonl          the conversation
        <sessionId>/               sidecar, present only when the session used it
            tool-results/          saved tool output
            subagents/*.jsonl      one transcript per subagent
        memory/                    the auto-memory — NOT session data

The sidecar sits at exactly the depth a project's ``memory/`` folder does, which
is the whole reason this module exists: a sweep that walks by depth rather than
by session will eventually reach the memory. :data:`RESERVED` is the guard, and
every directory listing here goes through :func:`sessions`.

CAUTION: the archive mirrors this tree with ``.gz`` appended to each file, so an
archived session is ``<slug>/<sessionId>.jsonl.gz`` beside
``<slug>/<sessionId>/…``. That layout is inherited from the shell script this
service replaces, and it is what makes the existing 328 MB archive readable by
this code without a migration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Directories inside a project that are not sessions and must never be swept.
RESERVED = frozenset({"memory"})

PROJECTS = Path(os.path.expanduser("~/.claude/projects"))
ARCHIVE = Path(os.path.expanduser("~/.claude/projects-archive"))


@dataclass(frozen=True)
class Session:
    """One session's files under one project, either live or archived.

    ``transcript`` may be absent while ``sidecar`` exists — that is the orphan
    the old sweep leaves behind, and it is the common case on a machine that ran
    the shell script for a while.
    """

    project: str
    session_id: str
    transcript: Path | None
    sidecar: Path | None

    @property
    def mtime(self) -> float:
        """Age the sweep decides on: the transcript's, or the sidecar's newest.

        An orphan has no transcript, so its sidecar has to answer. Taking the
        newest file in the tree rather than the directory's own mtime avoids
        archiving a session whose subagent wrote a minute ago into a directory
        that has not been touched since it was created.
        """
        best = 0.0
        if self.transcript is not None:
            try:
                best = self.transcript.stat().st_mtime
            except OSError:
                pass
        if self.sidecar is not None:
            for dirpath, _, names in os.walk(self.sidecar):
                for n in names:
                    try:
                        best = max(best, os.stat(os.path.join(dirpath, n)).st_mtime)
                    except OSError:
                        pass
        return best

    @property
    def bytes(self) -> int:
        total = 0
        if self.transcript is not None:
            try:
                total += self.transcript.stat().st_size
            except OSError:
                pass
        if self.sidecar is not None:
            for dirpath, _, names in os.walk(self.sidecar):
                for n in names:
                    try:
                        total += os.stat(os.path.join(dirpath, n)).st_size
                    except OSError:
                        pass
        return total


def _suffix(root: Path) -> str:
    return ".jsonl.gz" if root == ARCHIVE else ".jsonl"


def sessions(root: Path) -> list[Session]:
    """Every session under ``root``, live tree or archive.

    A session is named by whichever of its two parts exists, so an orphaned
    sidecar is returned with ``transcript=None`` rather than skipped.
    """
    suffix = _suffix(root)
    out: list[Session] = []
    try:
        projects = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return out
    for proj in projects:
        found: dict[str, list[Path | None]] = {}
        try:
            entries = sorted(proj.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir():
                if e.name in RESERVED:
                    continue
                found.setdefault(e.name, [None, None])[1] = e
            elif e.name.endswith(suffix):
                sid = e.name[: -len(suffix)]
                found.setdefault(sid, [None, None])[0] = e
        for sid, (transcript, sidecar) in sorted(found.items()):
            out.append(Session(project=proj.name, session_id=sid,
                               transcript=transcript, sidecar=sidecar))
    return out
