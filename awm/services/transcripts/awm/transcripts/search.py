"""Grep the archive without unpacking it.

An archived transcript is a gzipped JSONL file, so a match is found by streaming
it through gzip rather than by restoring the session first. That is the whole
reason the archive is gzip and not a tarball: the shell script's author kept it
greppable with ``zgrep``, and this keeps that property from Python.
"""

from __future__ import annotations

import gzip
import re

from awm.transcripts.layout import ARCHIVE, sessions

MAX_HITS = 200


def find(pattern: str, *, project: str | None = None,
         limit: int = MAX_HITS) -> dict:
    rx = re.compile(pattern)
    hits: list[dict] = []
    for s in sessions(ARCHIVE):
        if project and project not in s.project:
            continue
        if s.transcript is None:
            continue
        try:
            with gzip.open(s.transcript, "rt", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append({"project": s.project,
                                     "session": s.session_id,
                                     "line": lineno,
                                     "text": line[:400].rstrip()})
                        if len(hits) >= limit:
                            return {"pattern": pattern, "hits": hits,
                                    "truncated": True}
        except OSError:
            continue
    return {"pattern": pattern, "hits": hits, "truncated": False}


