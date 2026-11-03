"""The post kinds the journal's own code names (fleet.md R107 folds kinds into tags).

Every test of a kind goes through this module, so the move to tags changes one place.
"""

from __future__ import annotations

from typing import Any

JOURNAL = "journal"
GOAL = "goal"
BRIEF = "brief"


def is_journal(kind: Any) -> bool:
    return kind == JOURNAL
