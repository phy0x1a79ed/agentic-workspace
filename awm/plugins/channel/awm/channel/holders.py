"""Who holds a channel, how a line reaches them, and how to tell they are gone.

The holder is never named by the model. A session is known by its R9 id
(``sessions.md`` R9), and a channel belongs to that session: it closes when the
session ends. The id comes from ``_caller_session_id`` when the gateway sends
one, as the Rust gateway does. Otherwise the gateway's ``_caller_pid`` names a
session on this node, read from its record. A call from another node arrives
through that node's edge, which stamps the verified ``peer:<node>`` and carries
``_caller_session``, the claim that node read from its own record.

A line is typed into the holder's job, so only a background session can hold a
channel. The push and the dead-holder check follow ``social/seats.py``. They
are copied rather than imported until the two share one push loop (R69).
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

SESSION_KEY = "_caller_session_id"
PID_KEY = "_caller_pid"
CLAIM_KEY = "_caller_session"

Send = Callable[[str, str, str], Awaitable[str]]


class ChannelError(ValueError):
    """A channel verb refused. The message says why and what to do instead."""


def r9(claude_session_id: object) -> str | None:
    """The R9 id of a Claude Code session: ``cc`` and its UUID without dashes."""
    digits = str(claude_session_id or "").strip().lower().replace("-", "")
    if len(digits) != 32 or any(c not in "0123456789abcdef" for c in digits):
        return None
    return "cc" + digits


def _record(pid: int) -> dict | None:
    """``{background, job, session, name}`` for the session behind ``pid``.

    Asks reflection where this node's gatewayclient can (``sessions.session_of``),
    else reads Claude Code's own session record.
    """
    try:
        from awm.gatewayclient import sessions
    except ImportError:
        sessions = None
    if sessions is not None:
        rec = sessions.session_of(pid)
        if not rec:
            return None
        return {"background": rec.get("hosting") == "background",
                "job": str(rec.get("job") or ""), "session": rec.get("session_id"),
                "name": str(rec.get("name") or "")}
    from awm.claudedaemon import roster

    rec = roster.session_by_pid(pid)
    if not rec:
        return None
    return {"background": rec.get("kind") == "bg", "job": str(rec.get("jobId") or ""),
            "session": r9(rec.get("sessionId")), "name": str(rec.get("name") or "")}


def _session_rows(include_stopped: bool = False) -> list[dict] | None:
    """Every session on this node, from ``reflection list_other``, or None.

    None when reflection cannot answer, when a harness plugin errored, or when
    the rows carry no R9 ``session_id`` (a reflection older than its harness
    plugins). The caller then falls back to Claude Code's own records.
    """
    args = {"include_stopped": True} if include_stopped else {}
    try:
        out = gatewayclient.call_sync("reflection", "list_other", args, timeout=30.0)
    except Exception as exc:  # noqa: BLE001 — "could not tell" closes nothing
        log.info("reflection list_other failed: %s", exc)
        return None
    if not isinstance(out, dict) or out.get("ok") is False or out.get("errors"):
        return None
    rows = [r for r in out.get("sessions") or [] if isinstance(r, dict)]
    if not rows or not all(r.get("session_id") for r in rows):
        return None
    return rows


def _row_live(row: dict) -> bool:
    return (row.get("state") not in _DEAD_STATES and row.get("live") is not False
            and row.get("alive") is not False)


def _job_of_session(session: str) -> str | None:
    """The background job on this node whose session is ``session``."""
    rows = _session_rows(include_stopped=True)
    if rows is not None:
        for row in rows:
            if str(row["session_id"]).lower() == session and row.get("job"):
                return str(row["job"])
        return None
    return _job_of_session_on_disk(session)


def _job_of_session_on_disk(session: str) -> str | None:
    import json

    try:
        from awm.claudedaemon import roster
    except ImportError:
        return None
    jobs = roster.default_jobs_dir()
    try:
        states = list(jobs.glob("*/state.json"))
    except OSError:
        return None
    for state in states:
        try:
            rec = json.loads(state.read_text())
        except (OSError, ValueError):
            continue
        if session in (r9(rec.get("sessionId")), r9(rec.get("resumeSessionId"))):
            return state.parent.name
    return None


def caller(args: dict, as_: str | None) -> dict:
    """The calling session as ``{node, session, job, name}``, or a :class:`ChannelError`.

    ``session`` is the R9 id when one is known, else ``job:<job>``. ``pid`` joins
    them for a session on this node.
    """
    from awm.config import peerbook

    pid = args.get(PID_KEY)
    has_pid = isinstance(pid, int) and not isinstance(pid, bool)
    sid = args.get(SESSION_KEY)
    sid = sid.strip() if isinstance(sid, str) and sid.strip() else None
    if has_pid:
        claim, node = _record(pid), config.node_name()
    elif sid:
        job = _job_of_session(sid)
        claim = {"background": bool(job), "job": job or "", "session": sid, "name": ""}
        node = config.node_name()
    else:
        raw, node = args.get(CLAIM_KEY), peerbook.caller_peer(as_)
        claim = None
        if isinstance(raw, dict):
            claim = {"background": raw.get("kind") == "bg" or raw.get("hosting") == "background",
                     "job": str(raw.get("job") or ""),
                     "session": raw.get("session_id") or r9(raw.get("sessionId")),
                     "name": str(raw.get("name") or "")}
    if not node or not isinstance(claim, dict):
        raise ChannelError("no calling session reached channel: a channel is held "
                           "by a running agent session, so call this from one")
    if not claim["background"] or not claim["job"]:
        raise ChannelError("only a background session can hold a channel: each "
                           "message is typed into the holder's job")
    session = sid or claim.get("session") or f"job:{claim['job']}"
    holder = {"node": node, "session": str(session), "job": claim["job"],
              "name": claim["name"]}
    if has_pid:
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


def live_sessions() -> set[str] | None:
    """The R9 ids of the sessions running on this node, or None when it cannot tell.

    Blocking: call it off the event loop.
    """
    rows = _session_rows()
    if rows is not None:
        return {str(r["session_id"]).lower() for r in rows if _row_live(r)}
    try:
        from awm.claudedaemon import roster
        raw = roster.live_session_ids()
    except Exception as exc:  # noqa: BLE001 — "could not tell" closes nothing
        log.info("cannot list this node's sessions: %s", exc)
        return None
    return {sid for sid in (r9(r) for r in raw) if sid}


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
