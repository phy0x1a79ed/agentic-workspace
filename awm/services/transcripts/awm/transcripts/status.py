"""What the live tree and the archive currently hold.

The number the operator actually wants is the backlog: how many sessions are
past the retention and have not been swept. A sweep that reports success while
the backlog grows is what the shell script did for months.
"""

from __future__ import annotations

from awm.transcripts import sweep
from awm.transcripts.layout import ARCHIVE, PROJECTS, sessions


def _tally(root, days):
    all_ = sessions(root)
    old = sweep.aged(root, days)
    return {
        "sessions": len(all_),
        "orphans": sum(1 for s in all_ if s.transcript is None),
        "bytes": sum(s.bytes for s in all_),
        "past_retention": len(old),
        "past_retention_bytes": sum(s.bytes for s in old),
    }


def report(*, days: float = sweep.DEFAULT_RETENTION_DAYS,
           prune_days: float | None = None) -> dict:
    return {
        "live": {"path": str(PROJECTS), "retention_days": days,
                 **_tally(PROJECTS, days)},
        "archive": {"path": str(ARCHIVE),
                    "retention_days": prune_days,
                    **_tally(ARCHIVE, prune_days if prune_days else 1e9)},
    }
