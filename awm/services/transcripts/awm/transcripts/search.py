"""Grep the archive without unpacking it.

An archived transcript is a gzipped JSONL file, so a match is found by streaming
it through gzip rather than by restoring the session first. That is the whole
reason the archive is gzip and not a tarball: the shell script's author kept it
greppable with ``zgrep``, and this keeps that property from Python.

A session is filed under the directory it started in, but it can ``cd`` anywhere.
Each line records its ``cwd``, so a caller that may not see some paths is
shown nothing from a session that ever worked in one of them.
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path
from typing import Callable, Iterable

from awm.transcripts.layout import ARCHIVE, Hide, sessions

MAX_HITS = 200

#: A test on a working directory; a session that ever ran in one it holds true for is left out.
HidePath = Callable[[str], bool]

_CWD = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _cwds(line: str) -> Iterable[str]:
    for m in _CWD.finditer(line):
        try:
            yield json.loads(f'"{m.group(1)}"')
        except ValueError:
            yield m.group(1)


class Entered:
    """Whether a line ran in a hidden path, testing each distinct ``cwd`` once."""

    def __init__(self, hide_path: HidePath) -> None:
        self.hide_path = hide_path
        self.seen: dict[str, bool] = {}

    def __call__(self, line: str) -> bool:
        if '"cwd"' not in line:
            return False
        for cwd in _cwds(line):
            if cwd not in self.seen:
                self.seen[cwd] = self.hide_path(cwd)
            if self.seen[cwd]:
                return True
        return False


def entered(transcript: Path, hide_path: HidePath) -> bool:
    """Whether a gzipped transcript ever ran in a hidden path; an unreadable one counts as having done so."""
    test = Entered(hide_path)
    try:
        with gzip.open(transcript, "rt", errors="replace") as fh:
            return any(test(line) for line in fh)
    except (OSError, EOFError):
        return True


def find(pattern: str, *, project: str | None = None,
         limit: int = MAX_HITS, hide: Hide | None = None,
         hide_path: HidePath | None = None) -> dict:
    rx = re.compile(pattern)
    hits: list[dict] = []
    test = Entered(hide_path) if hide_path else None
    for s in sessions(ARCHIVE, hide):
        if project and project not in s.project:
            continue
        if s.transcript is None:
            continue
        room = limit - len(hits)
        found: list[dict] = []
        tainted = False
        try:
            with gzip.open(s.transcript, "rt", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if test and test(line):
                        tainted = True
                        break
                    if len(found) < room and rx.search(line):
                        found.append({"project": s.project,
                                      "session": s.session_id,
                                      "line": lineno,
                                      "text": line[:400].rstrip()})
                        if len(found) >= room and not test:
                            break
        except OSError:
            tainted = tainted or test is not None
        if tainted:
            continue
        hits += found
        if len(hits) >= limit:
            return {"pattern": pattern, "hits": hits, "truncated": True}
    return {"pattern": pattern, "hits": hits, "truncated": False}
