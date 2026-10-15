"""Which agent is calling: the name of the Claude Code session behind a stamped pid.

The gateway stamps every `operations` call with `_caller_pid`, the session that
made it, and a model cannot forge that. The session's own record names it: a
background job's `state.json` carries the current name, and any session record
may carry one too. The name is a label the swarm vouches for, not an identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .names import clean_agent


def session_name(pid: Any, *, lookup: Callable[[int], dict | None] | None = None,
                 jobs_dir: Path | None = None) -> str | None:
    """The calling session's name, or None for any reason it cannot be found. Never raises."""
    try:
        if lookup is None or jobs_dir is None:
            from awm.claudedaemon import roster
            lookup = lookup or roster.session_by_pid
            jobs_dir = jobs_dir or roster.default_jobs_dir()
        record = lookup(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None
        if not record:
            return None
        name = None
        if record.get("kind") == "bg" and record.get("jobId"):
            try:
                state = json.loads((jobs_dir / str(record["jobId"]) / "state.json").read_text())
                name = state.get("name") if isinstance(state, dict) else None
            except (OSError, ValueError):
                pass
        return clean_agent(name) or clean_agent(record.get("name"))
    except Exception:  # noqa: BLE001 — a missing name is an answer, never an error
        return None
