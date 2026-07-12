"""In-process act bus — live fan-out of an agent's transcript acts.

A scope IS NOT this. Raw agent acts (message / partial / tool_use / tool_result
/ status) belong to the *agent* and are persisted to ``agent_transcript`` in
``agents.db``. This module is the live side of that record: as ``_reader_loop``
persists each act it also publishes it here, so a WS ``agent_subscribe`` session
can stream new acts after replaying the cursored backfill.

Each published event carries the act's ``agent_transcript.id`` (a uuid) as
``id`` so a browser de-dupes the backfill/live overlap on it (the same dedupe
key end-to-end as agentcore's ``AgentEvent.id``).

Backpressure: ``partial`` acts are high volume. Mirrors the scopes channel
``lagged`` pattern (``awm/scopes/channel.py``): on a full subscriber queue we
enqueue a single ``{"type": "lagged"}`` sentinel and let the session writer
close 1011 — the browser reconnects with its last cursor and replays the gap.
"""

from __future__ import annotations

import asyncio


# scope (the unit slug) -> set of subscriber queues
_subscribers: dict[str, set[asyncio.Queue]] = {}
_subscribers_lock = asyncio.Lock()


async def attach_live(scope: str, queue: asyncio.Queue) -> None:
    async with _subscribers_lock:
        _subscribers.setdefault(scope, set()).add(queue)


async def detach_live(scope: str, queue: asyncio.Queue) -> None:
    async with _subscribers_lock:
        bucket = _subscribers.get(scope)
        if bucket is None:
            return
        bucket.discard(queue)
        if not bucket:
            _subscribers.pop(scope, None)


def publish_act(scope: str, act: dict) -> None:
    """Fan one persisted act out to every live subscriber of ``scope``.

    ``act`` is the wire shape (``{id, kind, body, meta, ts}``). On a full
    subscriber queue we enqueue a ``lagged`` sentinel (drop newest non-fitting
    rather than block the reader loop) — the writer closes the WS and the
    browser reconnects with its cursor.
    """
    bucket = _subscribers.get(scope)
    if not bucket:
        return
    event = {"type": "act", "act": act}
    for q in list(bucket):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            try:
                q.put_nowait({"type": "lagged"})
            except asyncio.QueueFull:
                pass
