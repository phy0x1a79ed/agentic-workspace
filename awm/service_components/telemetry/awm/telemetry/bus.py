"""In-process live bus — the live side of a telemetry stream.

As a service appends an event it also publishes it here, so a live WS session
can stream new events after replaying the cursored backfill. Generalizes the
agents transcript ``agent_bus`` to arbitrary streams (keyed by the event's
``stream``) and to the event wire shape (``{seq, id, stream, kind, body, meta,
ts}``).

Backpressure mirrors the agents/scopes ``lagged`` pattern: on a full subscriber
queue we enqueue a single ``{"type": "lagged"}`` sentinel (drop-newest rather
than block the publisher) and let the session writer close — the client
reconnects with its last cursor and replays the gap by poll.
"""

from __future__ import annotations

import asyncio


class TelemetryBus:
    """Fan one published event out to every live subscriber of its stream."""

    def __init__(self) -> None:
        # stream -> set of subscriber queues
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def attach_live(self, stream: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.setdefault(stream, set()).add(queue)

    async def detach_live(self, stream: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            bucket = self._subscribers.get(stream)
            if bucket is None:
                return
            bucket.discard(queue)
            if not bucket:
                self._subscribers.pop(stream, None)

    def publish(self, event: dict) -> None:
        """Fan ``event`` (which carries its own ``stream``) to live subscribers.

        Synchronous, like the append it follows — never blocks. On a full
        subscriber queue, enqueue a ``lagged`` sentinel instead of the event."""
        stream = event.get("stream")
        bucket = self._subscribers.get(stream) if stream else None
        if not bucket:
            return
        frame = {"type": "event", "event": event}
        for q in list(bucket):
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                # The subscriber fell behind. Evict the oldest queued frame to
                # guarantee the ``lagged`` sentinel lands (a full queue would
                # otherwise drop the sentinel too), then signal lag: the session
                # writer sends ``lagged`` and closes, and the client reconnects
                # from its last cursor and replays the gap by poll. Dropping the
                # oldest live frame is safe — the reconnect's backfill recovers
                # everything after the last frame the client actually received.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait({"type": "lagged"})
                except asyncio.QueueFull:
                    pass
