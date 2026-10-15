"""Seats: a chat a bot account is in, held by one Claude Code background session.

A mount is a ``social_mounts`` row on the node that owns the account. Each new
message in a mounted seat is typed into the holder's prompt through
``reflection message`` on the holder's node, one line per message, in order per
holder. A seat is released by its holder, taken by a forced mount, or freed by
the prune once the holder's job is gone.

The holder is never named by the model. On the owner node the gateway stamps
``_caller_pid``. A call from another node arrives through that node's edge,
which stamps the verified ``peer:<node>``, and carries ``_caller_session``: the
job and name the caller's own node read from its session record.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from awm import config, gatewayclient
from awm.social.connectors.base import InboundMessage

log = logging.getLogger("awm.social.seats")

PUSH_RETRY_S = 120.0
PRUNE_S = 60.0
TEXT_MAX = 1500
_GONE_MARKERS = ("no session on this node has the job id", "is not running")
_DEAD_STATES = ("gone", "stopped")

Send = Callable[[str, str, str], Awaitable[str]]


class SeatError(ValueError):
    """A seat verb refused; the message says why and what to do instead."""


# -- the calling session -----------------------------------------------------

def session_claim(pid: int) -> dict | None:
    """The job and name of the local session ``pid``, or None when it has none."""
    from awm.claudedaemon import roster

    rec = roster.session_by_pid(pid)
    if not rec:
        return None
    return {"kind": rec.get("kind") or "", "job": str(rec.get("jobId") or ""),
            "name": str(rec.get("name") or "")}


def caller(args: dict, as_: str | None) -> dict:
    """The calling session as ``{node, job, name}``, or a :class:`SeatError`."""
    from awm.config import peerbook

    pid = args.get("_caller_pid")
    if isinstance(pid, int) and not isinstance(pid, bool):
        claim, node = session_claim(pid), config.node_name()
    else:
        claim, node = args.get("_caller_session"), peerbook.caller_peer(as_)
    if not node or not isinstance(claim, dict):
        raise SeatError("no calling session reached social: a seat is held by a "
                        "running Claude Code session, so call this from one")
    if claim.get("kind") != "bg" or not claim.get("job"):
        raise SeatError("only a background session can hold a seat: the push "
                        "types each message into the holder's job")
    return {"node": node, "job": str(claim["job"]), "name": str(claim.get("name") or "")}


def holder_label(row: dict) -> str:
    name = f" ({row['session_name']})" if row.get("session_name") else ""
    return f"{row['job']}@{row['node']}{name}"


# -- the pushed line ---------------------------------------------------------

def push_line(m: InboundMessage) -> str:
    """``<platform> from <sender>[ ↳<thread>]: <text>`` on one line."""
    who = m.sender_name or m.sender_id or "?"
    thread = (f" ↳{m.thread_id}"
              if m.thread_id and m.thread_id != m.message_id else "")
    text = " ⏎ ".join(line for line in (m.text or "").splitlines())
    if len(text) > TEXT_MAX:
        text = text[:TEXT_MAX] + " …(cut, full text in social history)"
    if m.attachments:
        n = len(m.attachments)
        text = f"{text} [+{n} file{'s' if n > 1 else ''}]".lstrip()
    return f"{m.platform} from {who}{thread}: {text}"


# -- delivery ----------------------------------------------------------------

async def reflect(node: str, job: str, text: str) -> str:
    """Type ``text`` into ``job`` on ``node``: ``ok``, ``gone`` or ``retry``."""
    args = {"job": job, "text": text}
    try:
        if node == config.node_name():
            reply = await gatewayclient.call("reflection", "message", args, timeout=60.0)
        else:
            reply = await gatewayclient.call_peer(node, "reflection", "message", args,
                                                  timeout=60.0)
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
    :data:`PUSH_RETRY_S`, then dropped: the synced history still has it."""

    def __init__(self, send: Send = reflect,
                 on_gone: Callable[[str, str], None] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 retry_s: float = PUSH_RETRY_S) -> None:
        self._send = send
        self._on_gone = on_gone
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
        """Wait until every queue is empty (for tests and shutdown)."""
        while self._workers:
            await asyncio.gather(*list(self._workers.values()), return_exceptions=True)

    async def _drain(self, key: tuple[str, str]) -> None:
        q = self._queues[key]
        try:
            while not q.empty():
                if await self._deliver(key, q.get_nowait()) == "gone":
                    while not q.empty():
                        q.get_nowait()
                    if self._on_gone is not None:
                        self._on_gone(*key)
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


# -- releasing dead holders --------------------------------------------------

async def live_jobs(node: str) -> set[str] | None:
    """The running background jobs on ``node``, or None when it cannot tell."""
    try:
        if node == config.node_name():
            reply = await gatewayclient.call("cx", "list", {}, timeout=30.0)
        else:
            reply = await gatewayclient.call_peer(node, "cx", "list", {}, timeout=30.0)
    except Exception as exc:  # noqa: BLE001 — an unreachable node keeps its seats
        log.info("cannot list sessions on %s: %s", node, exc)
        return None
    if not isinstance(reply, dict) or "sessions" not in reply:
        return None
    return {str(row["job"]) for row in reply["sessions"]
            if isinstance(row, dict) and row.get("job")
            and row.get("state") not in _DEAD_STATES}


async def prune(dao, list_jobs=live_jobs) -> list[dict]:  # noqa: ANN001
    """Release every seat whose holder's job is gone; returns the freed rows."""
    freed: list[dict] = []
    by_node: dict[str, set[str]] = {}
    for row in dao.mounts():
        by_node.setdefault(row["node"], set()).add(row["job"])
    for node, jobs in by_node.items():
        live = await list_jobs(node)
        if live is None:
            continue
        for job in sorted(jobs - live):
            rows = dao.release_holder(node, job)
            for row in rows:
                log.info("released seat %s/%s: holder %s is gone",
                         row["account"], row["channel_id"], holder_label(row))
            freed.extend(rows)
    return freed


async def prune_loop(dao, every_s: float = PRUNE_S) -> None:  # noqa: ANN001
    while True:
        await asyncio.sleep(every_s)
        try:
            await prune(dao)
        except Exception as exc:  # noqa: BLE001 — retried next round
            log.warning("seat prune failed: %s", exc)
