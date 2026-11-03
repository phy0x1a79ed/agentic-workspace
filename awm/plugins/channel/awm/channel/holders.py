"""Who holds a channel, how a line reaches them, and how to tell they are gone.

The holder is never named by the model. On this node the gateway stamps
``_caller_pid``. A call from another node arrives through that node's edge,
which stamps the verified ``peer:<node>`` and carries ``_caller_session``, the
job and name that node read from its own session record. Only a background
session can hold a channel, because the push types each line into its job.

The push and the dead-holder check follow ``social/seats.py``. They are copied
rather than imported, because a channel and a seat are separate plugins.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from awm import config, gatewayclient

log = logging.getLogger("awm.channel.holders")

PUSH_RETRY_S = 120.0
_GONE_MARKERS = ("no session on this node has the job id", "is not running")
_DEAD_STATES = ("gone", "stopped")

Send = Callable[[str, str, str], Awaitable[str]]


class ChannelError(ValueError):
    """A channel verb refused. The message says why and what to do instead."""


def _session_claim(pid: int) -> dict | None:
    from awm.claudedaemon import roster

    rec = roster.session_by_pid(pid)
    if not rec:
        return None
    return {"kind": rec.get("kind") or "", "job": str(rec.get("jobId") or ""),
            "name": str(rec.get("name") or "")}


def caller(args: dict, as_: str | None) -> dict:
    """The calling session as ``{node, job, name}``, or a :class:`ChannelError`.

    ``pid`` joins them for a session on this node, so a call made for it can
    carry its stamp on to the next tool.
    """
    from awm.config import peerbook

    pid = args.get("_caller_pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        claim, node = _session_claim(pid), config.node_name()
    else:
        claim, node = args.get("_caller_session"), peerbook.caller_peer(as_)
    if not node or not isinstance(claim, dict):
        raise ChannelError("no calling session reached channel: a channel is held "
                           "by a running Claude Code session, so call this from one")
    if claim.get("kind") != "bg" or not claim.get("job"):
        raise ChannelError("only a background session can hold a channel: each "
                           "message is typed into the holder's job")
    holder = {"node": node, "job": str(claim["job"]), "name": str(claim.get("name") or "")}
    if isinstance(pid, int) and not isinstance(pid, bool):
        holder["pid"] = pid
    return holder


async def reflect(node: str, job: str, text: str) -> str:
    """Type ``text`` into ``job`` on ``node``: ``ok``, ``gone`` or ``retry``."""
    args = {"job": job, "text": text}
    try:
        if node == config.node_name():
            reply = await gatewayclient.call("reflection", "message", args, timeout=60.0)
        else:
            reply = await gatewayclient.call_peer(node, "reflection", "message", args,
                                                  timeout=60.0)
    except Exception as exc:  # noqa: BLE001 — unreachable is retried, never a close
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
    :data:`PUSH_RETRY_S`, then dropped: the transcript still has it."""

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
    """The running background jobs on ``node``, or None when it cannot tell."""
    try:
        if node == config.node_name():
            reply = await gatewayclient.call("cx", "list", {}, timeout=30.0)
        else:
            reply = await gatewayclient.call_peer(node, "cx", "list", {}, timeout=30.0)
    except Exception as exc:  # noqa: BLE001 — an unreachable node keeps its channels
        log.info("cannot list sessions on %s: %s", node, exc)
        return None
    if not isinstance(reply, dict) or "sessions" not in reply:
        return None
    return {str(row["job"]) for row in reply["sessions"]
            if isinstance(row, dict) and row.get("job")
            and row.get("state") not in _DEAD_STATES and row.get("alive") is not False}
