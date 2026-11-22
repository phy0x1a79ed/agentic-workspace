"""The conversations ``science new`` opened, each with the grants it added.

``science done`` reads this to know which grants to revoke, and ``status`` to
list conversations nobody closed. It lives outside ``OUTPUTS_ROOT`` so the
sandbox cannot edit it. A grant that existed before ``new`` is never recorded,
so ``done`` never revokes a standing grant.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from awm.config import AWM_DIR

LEDGER = Path(os.environ.get("CLAUDE_SCIENCE_LEDGER")
              or AWM_DIR / "claude-science-ledger.json")


def load() -> dict[str, dict]:
    try:
        return json.loads(LEDGER.read_text())
    except (OSError, ValueError):
        return {}


def _save(rows: dict[str, dict]) -> None:
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1, sort_keys=True))
    tmp.replace(LEDGER)


def add(conversation_id: str, mounts: list[str]) -> None:
    rows = load()
    rows[conversation_id] = {"mounts": mounts}
    _save(rows)


def own_mounts(conversation_id: str) -> list[str]:
    """The conversation's recorded mounts that no other record holds."""
    rows = load()
    mine = rows.pop(conversation_id, {}).get("mounts", [])
    shared = {m for row in rows.values() for m in row.get("mounts", [])}
    return [m for m in mine if m not in shared]


def forget(conversation_id: str) -> None:
    rows = load()
    if rows.pop(conversation_id, None) is not None:
        _save(rows)
