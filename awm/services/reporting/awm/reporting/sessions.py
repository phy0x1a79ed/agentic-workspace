"""The service's view of the node's sessions, through the cx service.

It never reads the daemon roster itself. It asks `cx list` which sessions
exist, and `cx start` for a new one.
"""

from __future__ import annotations

from typing import Any

from awm import gatewayclient

START_TIMEOUT_S = 240.0
LIST_TIMEOUT_S = 30.0
GONE = "gone"
STOPPED = "stopped"


class CxUnavailable(RuntimeError):
    """cx did not give a usable answer, so nothing can be said about the sessions."""


class Cx:
    """A thin client for the two cx verbs the service uses."""

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
