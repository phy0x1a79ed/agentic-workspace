"""The :class:`AgentSession` base + the generic ``run_once`` drain.

A backend subclasses :class:`AgentSession` and implements ``_start`` (spawn the
harness), ``send`` (push a user turn), ``_event_source`` (an async generator of
:class:`AgentEvent`), and ``close``. The base provides:

- a fan-out ``events()`` async-iterator that any number of consumers can drain
  concurrently (each gets every event from the moment it subscribes), backed by
  a single ``_pump`` task draining ``_event_source``;
- backpressure handling for high-volume ``partial`` events (a per-subscriber
  bounded queue that drops the oldest ``partial`` when full, never a
  message/tool/result/error);
- ``run_once``: open → send → drain until terminal → close (lives in
  ``api.py`` as the public wrapper, but the drain helper is here).

This module is a LEAF — no awm.config / gateway imports.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional

from .types import AgentConfig, AgentEvent


# Per-subscriber queue cap. Past this, the OLDEST queued `partial` is dropped
# to make room (partials are coalescible deltas); non-partials are never
# dropped — if the queue is full of non-partials we block the pump briefly.
_SUB_QUEUE_MAX = 512


class _Subscriber:
    """One consumer's view of the event stream (a bounded, partial-droppable
    queue fed by the session pump)."""

    __slots__ = ("queue", "closed")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Optional[AgentEvent]] = asyncio.Queue(
            maxsize=_SUB_QUEUE_MAX
        )
        self.closed = False

    def offer(self, event: Optional[AgentEvent]) -> None:
        """Non-blocking put with partial-drop backpressure. ``None`` is the
        end-of-stream sentinel and is always delivered (best effort)."""
        if self.closed:
            return
        q = self.queue
        if event is None:
            # Sentinel: try hard to deliver.
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                _drop_one_partial(q)
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass
            return
        try:
            q.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        # Full: drop the oldest partial to make room, then retry once.
        if _drop_one_partial(q):
            try:
                q.put_nowait(event)
                return
            except asyncio.QueueFull:
                pass
        # Still full (no partials to drop): drop this event if it's a partial,
        # otherwise force-evict the head to keep newest non-partials flowing.
        if event.kind == "partial":
            return
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _drop_one_partial(q: "asyncio.Queue[Optional[AgentEvent]]") -> bool:
    """Remove the oldest queued ``partial`` from ``q`` in place. Returns True if
    one was dropped. O(n) but n is bounded by the queue cap and this only runs
    under backpressure."""
    drained: list[Optional[AgentEvent]] = []
    dropped = False
    try:
        while True:
            drained.append(q.get_nowait())
    except asyncio.QueueEmpty:
        pass
    for item in drained:
        if not dropped and item is not None and item.kind == "partial":
            dropped = True
            continue
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            break
    return dropped


class AgentSession(ABC):
    """Open handle to one harness conversation.

    Backends subclass this. The base owns the fan-out + pump; subclasses own the
    subprocess and the raw → :class:`AgentEvent` mapping (``_event_source``).
    """

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._subscribers: set[_Subscriber] = set()
        self._pump_task: Optional[asyncio.Task] = None
        self._started = False
        self._closed = False
        self._start_lock = asyncio.Lock()

    # ---- lifecycle ----

    async def start(self) -> None:
        """Spawn the harness + begin pumping. Idempotent."""
        async with self._start_lock:
            if self._started:
                return
            await self._start()
            self._started = True
            self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        """Drain ``_event_source`` and fan out to every subscriber."""
        try:
            async for event in self._event_source():
                for sub in list(self._subscribers):
                    sub.offer(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface as an error event
            err = AgentEvent(kind="error", text=str(exc),
                             data={"exception": type(exc).__name__})
            for sub in list(self._subscribers):
                sub.offer(err)
        finally:
            for sub in list(self._subscribers):
                sub.offer(None)  # end-of-stream sentinel

    # ---- public surface ----

    def subscribe(self) -> AsyncIterator[AgentEvent]:
        """Register a subscriber synchronously and return an async-iterator.

        Use this to begin capturing events **before** the first ``send`` so no
        early event (e.g. the claude ``init`` status) is missed by a
        subscribe-after-the-pump-drained race. The subscriber is registered the
        moment this returns (not when iteration starts).
        """
        sub = _Subscriber()
        self._subscribers.add(sub)
        return self._iter_subscriber(sub)

    async def _iter_subscriber(
        self, sub: "_Subscriber"
    ) -> AsyncIterator[AgentEvent]:
        try:
            while True:
                event = await sub.queue.get()
                if event is None:
                    return
                yield event
        finally:
            sub.closed = True
            self._subscribers.discard(sub)

    async def events(self) -> AsyncIterator[AgentEvent]:
        """Async-iterate the normalized event stream from now on.

        Multiple concurrent callers each get every event from their
        subscription point. The iterator ends when the harness stream ends
        (``_event_source`` exhausted) or the session is closed. Registers the
        subscriber up front (before awaiting ``start``) so the pump can't drain
        past it.
        """
        sub = _Subscriber()
        self._subscribers.add(sub)
        if not self._started:
            await self.start()
        async for event in self._iter_subscriber(sub):
            yield event

    @abstractmethod
    async def send(self, text: str) -> None:
        """Push one user turn to the harness."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Tear down the subprocess and stop pumping."""
        ...

    # ---- backend hooks ----

    @abstractmethod
    async def _start(self) -> None:
        """Spawn the subprocess / warm the server. Called once under lock."""
        ...

    @abstractmethod
    def _event_source(self) -> AsyncIterator[AgentEvent]:
        """Async-generator of normalized events for the whole session."""
        ...

    # ---- shared teardown helper ----

    async def _stop_pump(self) -> None:
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Wake any blocked consumers.
        for sub in list(self._subscribers):
            sub.offer(None)
        self._closed = True
