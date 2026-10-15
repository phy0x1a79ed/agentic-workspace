"""The board's server-sent event stream, and the loops that keep the board tidy.

The stream replays from ``Last-Event-ID`` out of the event log, then tails it.
The log, not this module, decides which events a party may see: ``Events.since``
takes the party and filters. A subscriber that fell behind therefore gets the
same answer whether it reconnects or just reads slowly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, AsyncIterator, Callable

from awm.board import EVENT_RETENTION_DAYS, HEARTBEAT_S

log = logging.getLogger("awm.board.stream")

#: A comment line. Clients ignore it; Cloudflare sees traffic and keeps the stream open.
HEARTBEAT = b": keepalive\n\n"

#: Sent once at the top so a reconnecting client backs off briefly rather than at once.
PREAMBLE = b"retry: 3000\n\n"

#: How often a tail polls the log when nothing woke it. The wakeup is the fast
#: path; the poll is what catches an event appended by a writer that holds no
#: reference to this process's ``Wakeup``.
POLL_S = 1.0

SWEEP_S = 5.0
PRUNE_S = 24 * 3600.0


class Wakeup:
    """Lets the threads that append events nudge the streams waiting on them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    def subscribe(self) -> asyncio.Event:
        event = asyncio.Event()
        with self._lock:
            self._waiters.add((asyncio.get_running_loop(), event))
        return event

    def unsubscribe(self, event: asyncio.Event) -> None:
        with self._lock:
            self._waiters = {w for w in self._waiters if w[1] is not event}

    def notify(self) -> None:
        with self._lock:
            waiters = list(self._waiters)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # the loop closed under us; its stream is gone
                pass


class NotifyingEvents:
    """The event log, plus a nudge to the streams whenever something is appended."""

    def __init__(self, events: Any, wakeup: Wakeup) -> None:
        self._events = events
        self.wakeup = wakeup

    def append(self, *args: Any, **kwargs: Any) -> Any:
        result = self._events.append(*args, **kwargs)
        self.wakeup.notify()
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._events, name)


def unpack(entry: Any) -> tuple[int, str, dict]:
    """One log entry as ``(id, type, card)``, whether the log returns dicts or tuples."""
    if isinstance(entry, dict):
        return int(entry["id"]), str(entry["type"]), entry.get("card") or {}
    event_id, event_type, card = entry[0], entry[1], entry[2]
    return int(event_id), str(event_type), card or {}


def frame(event_id: int, event_type: str, card: dict) -> bytes:
    """One SSE event. The data is a single line, so no line of it can end the event early."""
    data = json.dumps(card, separators=(",", ":"), default=str)
    return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n".encode()


def parse_last_event_id(value: str | None) -> int | None:
    """The cursor a client sent, or ``None`` when it sent none or sent nonsense."""
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def head(events: Any, party: dict) -> int:
    """The newest event id in the log, for a stream that wants live events only."""
    probe = getattr(events, "latest_id", None)
    if callable(probe):
        return int(probe())
    last = 0
    for entry in events.since(0, party):
        last = max(last, unpack(entry)[0])
    return last


def oldest(events: Any) -> int | None:
    """The oldest event id still retained, or ``None`` when the log cannot say."""
    probe = getattr(events, "oldest_id", None)
    return int(probe()) if callable(probe) else None


def is_stale(last_id: int, latest: int, retained_from: int | None) -> bool:
    """True when a cursor points somewhere the log cannot replay from.

    Newer than the log's head means the log was reset or the client is confused.
    Older than the oldest retained event means pruning removed events the
    client never saw. Either way the client must catch up from the card list.
    """
    if last_id > latest:
        return True
    return bool(retained_from) and last_id < retained_from - 1


async def event_stream(
    events: Any,
    party: dict,
    last_id: int | None,
    *,
    wakeup: Wakeup | None = None,
    heartbeat_s: float = HEARTBEAT_S,
    poll_s: float = POLL_S,
    still_valid: Callable[[], bool] | None = None,
) -> AsyncIterator[bytes]:
    """Replay what the party missed after ``last_id``, then tail the log forever.

    ``last_id=None`` means a live stream: the cursor starts at the newest event
    and the first frame carries it as a bare ``id:`` so the client holds a
    cursor before its first real event. A cursor the log cannot replay from
    gets one ``resync`` frame and then a live stream from the head.
    ``still_valid`` runs once per heartbeat interval whatever else was sent, so
    a party revoked mid-stream loses its stream even while events keep flowing.
    """
    latest = await asyncio.to_thread(head, events, party)
    opening = [PREAMBLE]
    if last_id is None:
        cursor = latest
        opening.append(f"id: {latest}\n\n".encode())
    else:
        retained_from = await asyncio.to_thread(oldest, events)
        if is_stale(last_id, latest, retained_from):
            cursor = latest
            opening.append(frame(latest, "resync", {"latest": latest, "oldest": retained_from}))
        else:
            cursor = last_id
    waiter = wakeup.subscribe() if wakeup is not None else None
    for chunk in opening:
        yield chunk
    next_beat = time.monotonic() + heartbeat_s
    next_check = next_beat if still_valid is not None else float("inf")
    try:
        while True:
            if waiter is not None:
                waiter.clear()
            if still_valid is not None and time.monotonic() >= next_check:
                if not await asyncio.to_thread(still_valid):
                    return
                next_check = time.monotonic() + heartbeat_s
            batch = await asyncio.to_thread(events.since, cursor, party)
            for entry in batch:
                event_id, event_type, card = unpack(entry)
                if event_id <= cursor:
                    continue
                cursor = event_id
                yield frame(event_id, event_type, card)
                next_beat = time.monotonic() + heartbeat_s
            if batch:
                continue
            remaining = min(next_beat, next_check) - time.monotonic()
            if remaining <= 0:
                if time.monotonic() >= next_beat:
                    yield HEARTBEAT
                    next_beat = time.monotonic() + heartbeat_s
                continue
            timeout = min(poll_s, remaining)
            if waiter is None:
                await asyncio.sleep(timeout)
            else:
                try:
                    await asyncio.wait_for(waiter.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
    finally:
        if waiter is not None and wakeup is not None:
            wakeup.unsubscribe(waiter)


async def maintain(
    board: Any,
    events: Any,
    *,
    sweep_s: float = SWEEP_S,
    prune_s: float = PRUNE_S,
) -> None:
    """Sweep stale claims every few seconds and prune the event log daily. Never returns."""
    next_prune = time.monotonic()
    while True:
        try:
            await asyncio.to_thread(board.sweep)
        except Exception:  # noqa: BLE001 — a bad sweep must not end the loop
            log.exception("board: sweep failed")
        if time.monotonic() >= next_prune:
            try:
                await asyncio.to_thread(events.prune, EVENT_RETENTION_DAYS)
            except Exception:  # noqa: BLE001
                log.exception("board: event prune failed")
            next_prune = time.monotonic() + prune_s
        await asyncio.sleep(sweep_s)
