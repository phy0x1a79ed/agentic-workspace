"""Everything the routine service asks of other plugins, behind one interface.

The service calls other plugins only through the gateway (install.md R117), with
:func:`awm.gatewayclient.call`. Tests replace :class:`Outbound` with a fake.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol

from awm import gatewayclient

log = logging.getLogger("awm.routine.outbound")


class Outbound(Protocol):
    async def start_session(self, *, peer: str, project: str, scope: str,
                            prompt: str, started_by: str, name: str) -> dict[str, Any]:
        """Start a background session, in the lobby when ``scope`` is empty.
        Returns at least ``{"job": ...}``."""

    async def notify_operator(self, text: str) -> None:
        """Send the operator a Discord DM."""

    async def journal(self, project: str, scope: str, body: str) -> None:
        """Post to a scope's journal."""

    async def card_of(self, pid: int) -> str:
        """The Swarm-board task of the session at ``pid`` on this node, or ``""``."""

    async def card_post(self, card: str, body: str) -> None:
        """Post a message on a Swarm-board card."""

    async def invoke(self, tool: str, verb: str, args: dict[str, Any],
                     *, peer: str | None = None, timeout: float = 60.0) -> Any:
        """Call any tool verb, here or on a peer."""


class GatewayOutbound:
    """The real outbound, through this node's gateway."""

    async def invoke(self, tool: str, verb: str, args: dict[str, Any],
                     *, peer: str | None = None, timeout: float = 60.0) -> Any:
        try:
            result = await gatewayclient.call(tool, verb, args, peer=peer, timeout=timeout)
        except gatewayclient.GatewayCallError as exc:
            raise RuntimeError(f"{tool} {verb}: HTTP {exc.status}: {exc.body[:400]}") from exc
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(f"{tool} {verb}: {result['error']}")
        return result

    async def start_session(self, *, peer: str, project: str, scope: str,
                            prompt: str, started_by: str, name: str) -> dict[str, Any]:
        args: dict[str, Any] = {"prompt": prompt, "started_by": started_by, "name": name,
                                "model": "opus", "effort": "high"}
        if scope:
            args |= {"project": project, "scope": scope}
        res = await self.invoke("reflection", "start_other", args, peer=peer, timeout=120.0)
        if not isinstance(res, dict) or not res.get("job"):
            raise RuntimeError(f"start_other returned no job: {res!r}"[:400])
        return res

    async def notify_operator(self, text: str) -> None:
        from awm.config import operator_dm
        target = operator_dm()
        if target is None:
            raise RuntimeError("this node has no operator DM channel")
        account, channel = target
        await self.invoke("social", "send", {
            "account": account, "channel": channel, "text": text},
            peer=os.environ.get("AWM_SOCIAL_PEER") or None)

    async def card_of(self, pid: int) -> str:
        who = await self.invoke("reflection", "session_of", {"pid": pid}, timeout=10.0)
        sid = who.get("session_id") if isinstance(who, dict) and who.get("known") else ""
        if not sid:
            return ""
        res = await self.invoke("operations", "task_for_session", {"session_id": sid},
                                timeout=10.0)
        return str(res.get("task_id") or "") if isinstance(res, dict) else ""

    async def card_post(self, card: str, body: str) -> None:
        await self.invoke("operations", "swarm_message_post", {"card_id": card, "body": body})

    async def journal(self, project: str, scope: str, body: str) -> None:
        await self.invoke("journal", "post", {
            "project": project, "scope": scope, "author": "routine", "body": body})
