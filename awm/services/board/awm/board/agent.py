"""Which agent is calling: the name of the session behind a stamped pid.

The gateway stamps every `operations` call with `_caller_pid`, the session that
made it, and a model cannot forge that. reflection asks the session's harness
plugin for its current name. The name is a label the swarm vouches for, not an
identity.
"""

from __future__ import annotations

from typing import Any, Callable

from .names import clean_agent


def session_name(pid: Any, *, lookup: Callable[[int], dict | None] | None = None
                 ) -> str | None:
    """The calling session's name, or None for any reason it cannot be found. Never raises."""
    try:
        if lookup is None:
            from awm.gatewayclient.sessions import session_of as lookup
        record = lookup(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None
        return clean_agent(record.get("name")) if record else None
    except Exception:  # noqa: BLE001 — a missing name is an answer, never an error
        return None
