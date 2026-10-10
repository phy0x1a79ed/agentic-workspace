"""The door's view of the node's sessions, through the cx service.

The door never reads the daemon roster itself. It asks `cx list` which sessions
exist and calls `cx start` for a missing one. It has no stop: a session the door
started belongs to whoever attaches to it.

A session counts as the representative or the secretary only if the door
started it (the door records each job id `cx start` returns) and it carries the
role's *mode* label. The label alone is not trusted: another caller could start
a session with it. The mode, unlike a name, survives a rename from the Claude
app.
"""

from __future__ import annotations

import logging
from typing import Any

from awm import gatewayclient

log = logging.getLogger("awm.representative.sessions")

START_TIMEOUT_S = 240.0
LIST_TIMEOUT_S = 30.0
GONE = "gone"


class CxUnavailable(RuntimeError):
    """cx did not give a usable answer, so nothing can be said about the sessions."""


class Cx:
    """A thin client for the two cx verbs the door uses."""

    async def list(self) -> list[dict]:
        try:
            reply = await gatewayclient.call("cx", "list", {}, timeout=LIST_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — any failure means "cannot tell"
            raise CxUnavailable(f"cx list failed: {exc}") from exc
        if not isinstance(reply, dict) or reply.get("ok") is False or "sessions" not in reply:
            reason = reply.get("reason") if isinstance(reply, dict) else None
            raise CxUnavailable(f"cx list refused: {reason or reply!r}")
        return [row for row in reply["sessions"] if isinstance(row, dict)]

    async def start(self, spec: dict[str, Any]) -> dict:
        args = {k: v for k, v in spec.items() if v is not None}
        try:
            reply = await gatewayclient.call("cx", "start", args, timeout=START_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"cx start failed: {exc}"}
        return reply if isinstance(reply, dict) else {"ok": False, "reason": repr(reply)}


def _live_with_mode(rows: list[dict], mode: str) -> list[dict]:
    return [r for r in rows if r.get("mode") == mode and r.get("state") != GONE]


def holders(rows: list[dict], mode: str, jobs: set[str]) -> list[dict]:
    """Live sessions with the mode label *that the door started* (``jobs`` is its record)."""
    return [r for r in _live_with_mode(rows, mode) if r.get("job") in jobs]


def strangers(rows: list[dict], mode: str, jobs: set[str]) -> list[dict]:
    """Live sessions that carry the mode label but were not started by the door."""
    return [r for r in _live_with_mode(rows, mode) if r.get("job") not in jobs]


def find_job(rows: list[dict], mode: str, jobs: set[str]) -> str | None:
    """The job id of the door's live session with this mode, or None."""
    for row in holders(rows, mode, jobs):
        if row.get("job"):
            return row["job"]
    return None
