"""The session behind a caller, and a line typed into a session, through reflection.

A service holds the caller pid the gateway stamped on a call. These ask reflection,
which asks the session's harness plugin, so a service reads no harness's files.
"""
from __future__ import annotations

from typing import Any, Optional


def session_of(pid: Any, *, timeout: float = 10.0) -> Optional[dict]:
    """The session that ``pid`` belongs to, or None when it is none or reflection is down.

    The record carries ``session_id`` (R9), ``claude_session_id`` for a Claude Code
    session, ``name``, ``job``, ``hosting`` (``background`` or ``terminal``), ``pid``,
    ``proc_start``, ``cwd`` and ``harness``.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    from awm.gatewayclient import call_sync

    try:
        out = call_sync("reflection", "session_of", {"pid": pid}, timeout=timeout)
    except Exception:  # noqa: BLE001 - no answer is no session
        return None
    return out if isinstance(out, dict) and out.get("ok") and out.get("session_id") else None


def tell(job: str, text: str, *, timeout: float = 30.0) -> dict:
    """Type ``text`` into session ``job`` and submit it. Raises when it was not delivered."""
    from awm.gatewayclient import call_sync

    out = call_sync("reflection", "message", {"to": job, "text": text}, timeout=timeout)
    if not isinstance(out, dict) or not out.get("ok"):
        why = (out or {}).get("reason") or (out or {}).get("error") if isinstance(out, dict) else out
        raise RuntimeError(f"could not type into session {job}: {why}")
    return out
