"""Claude session UUIDs that a running Claude Code process holds, from cch.

CAUTION: unreadable means live. Any failure to get the list raises
:class:`Unknown`, and the sweep stops instead of archiving a session in use.
"""

from __future__ import annotations

import logging

log = logging.getLogger("awm.transcripts.live")


class Unknown(RuntimeError):
    """cch could not say which sessions are live."""


def _ask() -> dict:
    from awm.gatewayclient import call_sync

    return call_sync("cch", "live_session_ids", {}, timeout=30.0)


def session_ids() -> set[str]:
    try:
        out = _ask()
    except Exception as exc:  # noqa: BLE001 - no answer is no list
        log.error("transcripts: sweep blocked, cch did not answer: %s", exc)
        raise Unknown(f"cch did not answer live_session_ids: {exc}") from exc
    if not isinstance(out, dict) or not out.get("ok"):
        reason = out.get("reason") if isinstance(out, dict) else out
        log.error("transcripts: sweep blocked: %s", reason)
        raise Unknown(f"cch cannot list the live sessions: {reason}")
    return {str(s) for s in out.get("session_ids") or []}
