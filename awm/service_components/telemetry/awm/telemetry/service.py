"""``Telemetry`` — the facade a host service wires in.

Bundles the durable :class:`TelemetryStore` and the live :class:`TelemetryBus`
behind one object so a service's adapter is a few lines:

    from awm.telemetry import Telemetry

    tel = Telemetry("orchestrator")

    # publish (append + live fan-out) wherever state changes:
    tel.emit(f"task:{task_id}", "state_changed", "", {"from": a, "to": b})

    # poll op (manifest function):
    def telemetry_poll(args):
        return {"events": tel.read_after(args["stream"], args.get("after_seq"))}

    # live WS session (drop into the adapter's session_handlers):
    ServiceAdapter(..., session_handlers={"telemetry": tel.session_handler})

The ``session_handler`` is duck-typed against the gatewayclient
``SessionContext`` (``ctx.init`` + ``await ctx.open_bridge()``) so this module
keeps its no-gateway-imports rule — any service's ``hub_adapter`` can use it.
The live session replays the backfill from the init cursor, then pushes events;
the client dedupes the overlap by ``id`` and reconnects on ``lagged``.
"""

from __future__ import annotations

import asyncio
import json

from awm.telemetry.bus import TelemetryBus
from awm.telemetry.store import TelemetryStore

# Live subscriber queue depth before the bus drops to a ``lagged`` sentinel.
_QUEUE_MAXSIZE = 256


class Telemetry:
    """Durable store + live bus for one service's telemetry streams."""

    def __init__(self, service: str, *, queue_maxsize: int = _QUEUE_MAXSIZE) -> None:
        self.service = service
        self.store = TelemetryStore(service)
        self.bus = TelemetryBus()
        self._queue_maxsize = queue_maxsize

    # -- publish ------------------------------------------------------------

    def emit(self, stream: str, kind: str, body: str = "",
             meta: dict | None = None) -> dict:
        """Append one event AND fan it out live. Returns the wire event.

        The single publish primitive: the durable append assigns ``seq`` + mints
        ``id``, then the same event is pushed to live subscribers. A late
        subscriber catches it via the backfill; an attached one via the bus —
        either way it sees the event exactly once (dedup by ``id``)."""
        ev = self.store.append(stream, kind, body, meta)
        self.bus.publish(ev)
        return ev

    # -- poll read ----------------------------------------------------------

    def read_after(self, stream: str, after_seq: int | None = None, *,
                   after_id: str | None = None,
                   limit: int | None = None) -> list[dict]:
        """Direct poll sync: events after the cursor (see store.read_after)."""
        return self.store.read_after(
            stream, after_seq, after_id=after_id, limit=limit)

    # -- live WS session ----------------------------------------------------

    async def session_handler(self, ctx) -> None:
        """Drive a direct WS session: backfill from cursor → live push.

        ``ctx.init`` is ``{stream, after_seq?}``. On open: attach to the bus,
        replay ``read_after(stream, after_seq)`` as one ``{"type":"backfill",
        "events":[...]}`` frame, then stream ``{"type":"event","event":{...}}``
        frames live. On bus backpressure a ``{"type":"lagged"}`` sentinel is sent
        and the socket closes (the client reconnects with its last cursor)."""
        init = ctx.init or {}
        stream = init.get("stream")
        if not stream:
            try:
                bridge = await ctx.open_bridge()
                await bridge.send(json.dumps(
                    {"type": "error", "message": "init requires 'stream'"}))
                await bridge.close()
            except Exception:  # noqa: BLE001
                pass
            return

        after_seq = init.get("after_seq")
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_maxsize)
        await self.bus.attach_live(stream, queue)
        bridge = await ctx.open_bridge()
        try:
            backfill = self.store.read_after(stream, after_seq)
            await bridge.send(json.dumps({"type": "backfill", "events": backfill}))
            while True:
                ev = await queue.get()
                if isinstance(ev, dict) and ev.get("type") == "lagged":
                    try:
                        await bridge.send(json.dumps({"type": "lagged"}))
                    except Exception:  # noqa: BLE001
                        pass
                    break
                try:
                    await bridge.send(json.dumps(ev))
                except Exception:  # noqa: BLE001
                    break
        finally:
            await self.bus.detach_live(stream, queue)
            try:
                await bridge.close()
            except Exception:  # noqa: BLE001
                pass
