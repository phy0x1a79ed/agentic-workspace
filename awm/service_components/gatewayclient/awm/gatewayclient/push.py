"""The push loop of channels.md R69: lines typed into a holder's background job, in order per holder.

social's seats and the channel plugin both deliver through it. It is a library
here because a plugin may not import another plugin's code (install.md R117 item 1).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from awm import config
from awm.gatewayclient import call

log = logging.getLogger("awm.gatewayclient.push")

PUSH_RETRY_S = 120.0
_GONE_MARKERS = ("no session on this node has the job id", "is not running")
_DEAD_STATES = ("gone", "stopped")

Send = Callable[[str, str, str], Awaitable[str]]


async def _reflection(node: str, verb: str, args: dict) -> object:
    peer = None if node == config.node_name() else node
    return await call("reflection", verb, args, peer=peer, timeout=60.0)


async def reflect(node: str, job: str, text: str) -> str:
    """Type ``text`` into ``job`` on ``node``: ``ok``, ``gone`` or ``retry``."""
    try:
        reply = await _reflection(node, "message", {"job": job, "text": text})
    except Exception as exc:  # noqa: BLE001 — unreachable is retried, never a release
        log.info("push to %s@%s failed: %s", job, node, exc)
        return "retry"
    if isinstance(reply, dict) and reply.get("ok"):
        return "ok"
    reason = str((reply or {}).get("error") or (reply or {}).get("reason") or reply)
    if any(marker in reason for marker in _GONE_MARKERS):
        return "gone"
    log.info("push to %s@%s refused: %s", job, node, reason)
    return "retry"


class Pusher:
    """One ordered queue per holder. A line the holder refuses is retried for
    :data:`PUSH_RETRY_S`, then dropped: the caller's own history still has it.
    ``on_gone(node, job)`` runs once the holder's job is gone."""

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

    def push(self, node: str, job: str, line: str) -> None:
        key = (node, job)
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
        node, job = key
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._retry_s
        delay = 2.0
        while True:
            outcome = await self._send(node, job, line)
            if outcome in ("ok", "gone"):
                return outcome
            if loop.time() + delay > deadline:
                log.warning("dropped a push to %s@%s after %.0fs of refusals",
                            job, node, self._retry_s)
                return "dropped"
            await self._sleep(delay)
            delay = min(delay * 2, 30.0)


async def live_jobs(node: str) -> set[str] | None:
    """The running background jobs on ``node`` across its harnesses, or None when it cannot tell."""
    try:
        reply = await _reflection(node, "list_other", {})
    except Exception as exc:  # noqa: BLE001 — an unreachable node keeps what it holds
        log.info("cannot list sessions on %s: %s", node, exc)
        return None
    if not isinstance(reply, dict) or "sessions" not in reply or reply.get("errors"):
        return None
    return {str(row["job"]) for row in reply["sessions"]
            if isinstance(row, dict) and row.get("job")
            and row.get("state") not in _DEAD_STATES and row.get("alive") is not False
            and row.get("live") is not False}
