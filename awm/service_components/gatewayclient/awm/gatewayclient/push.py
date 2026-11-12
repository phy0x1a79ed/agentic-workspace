"""The push loop of channels.md R69: lines typed into a holder's session, in order per holder.

social's seats and the channel plugin both deliver through it. It is a library
here because a plugin may not import another plugin's code (install.md R117 item 1).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from awm import config
from awm.gatewayclient import call
from awm.gatewayclient.holders import row_live

log = logging.getLogger("awm.gatewayclient.push")

PUSH_RETRY_S = 120.0
_GONE_MARKERS = ("no session on this node has the id", "is not running")

Send = Callable[[str, str, str], Awaitable[str]]


async def _reflection(node: str, verb: str, args: dict) -> object:
    peer = None if node == config.node_name() else node
    return await call("reflection", verb, args, peer=peer, timeout=60.0)


async def reflect(node: str, session: str, text: str) -> str:
    """Type ``text`` into session ``session`` (canonical id) on ``node``: ``ok``, ``gone`` or ``retry``."""
    try:
        reply = await _reflection(node, "message", {"to": {"id": session}, "text": text})
    except Exception as exc:  # noqa: BLE001 — unreachable is retried, never a release
        log.info("push to %s@%s failed: %s", session, node, exc)
        return "retry"
    if isinstance(reply, dict) and reply.get("ok"):
        return "ok"
    reason = str((reply or {}).get("error") or (reply or {}).get("reason") or reply)
    if any(marker in reason for marker in _GONE_MARKERS):
        return "gone"
    log.info("push to %s@%s refused: %s", session, node, reason)
    return "retry"


class Pusher:
    """One ordered queue per holder. A line the holder refuses is retried for
    :data:`PUSH_RETRY_S`, then dropped: the caller's own history still has it.
    ``on_gone(node, session)`` runs once the holder's session is gone."""

    def __init__(self, send: Send = reflect,
                 on_gone: Callable[[str, str], None] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 retry_s: float = PUSH_RETRY_S) -> None:
        self._send = send
        self.on_gone = on_gone
        self._sleep = sleep
        self._retry_s = retry_s
        self._queues: dict[tuple[str, str], asyncio.Queue[str]] = {}
        self._workers: dict[tuple[str, str], asyncio.Task] = {}

    def push(self, node: str, session: str, line: str) -> None:
        key = (node, session)
        self._queues.setdefault(key, asyncio.Queue()).put_nowait(line)
        if key not in self._workers:
            self._workers[key] = asyncio.create_task(self._drain(key))

    async def idle(self) -> None:
        """Wait until every queue is empty."""
        while self._workers:
            await asyncio.gather(*list(self._workers.values()), return_exceptions=True)

    async def _drain(self, key: tuple[str, str]) -> None:
        q = self._queues[key]
        try:
            while not q.empty():
                if await self._deliver(key, q.get_nowait()) == "gone":
                    while not q.empty():
                        q.get_nowait()
                    if self.on_gone is not None:
                        self.on_gone(*key)
        finally:
            del self._workers[key]
            del self._queues[key]

    async def _deliver(self, key: tuple[str, str], line: str) -> str:
        node, session = key
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._retry_s
        delay = 2.0
        while True:
            outcome = await self._send(node, session, line)
            if outcome in ("ok", "gone"):
                return outcome
            if loop.time() + delay > deadline:
                log.warning("dropped a push to %s@%s after %.0fs of refusals",
                            session, node, self._retry_s)
                return "dropped"
            await self._sleep(delay)
            delay = min(delay * 2, 30.0)
