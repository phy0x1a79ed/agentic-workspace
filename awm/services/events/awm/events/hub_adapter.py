"""Hub adapter for the events service.

Boots the events service as a gateway-registered process: stands up its own DB,
runs the shared :class:`awm.gatewayclient.ServiceAdapter` loop (register → ready
→ serve → reconnect), and — as a sibling task — the :class:`Scheduler` cadence
loop that emits ``schedule.tick`` for every persisted schedule.

The three schedule functions are exposed over the control WS and projected into
the gateway catalog as ``events_<fn>`` tools. ``schedule.tick{game}`` is declared
as an emitter and fanned out on ``/svc/events/emit/schedule.tick``; the ``agents``
service subscribes to it to wake a bot for the game.

Run via ``run.sh`` (which the hub spawns and respawns):
    python -m awm.events.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.events import dao
from awm.events.dao import EventsDAO
from awm.events.scheduler import Scheduler

log = logging.getLogger("awm.events.hub_adapter")

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "schedule",
            "tool": "events_schedule",
            "description": (
                "Schedule recurring schedule.tick emissions for a game. "
                "cron is a 5-field cron (min hour dom month dow) or an "
                "'@every <n>s|m|h' interval. Re-scheduling overwrites the cron."
            ),
            "params": [
                {"name": "game", "type": "string", "required": True},
                {"name": "cron", "type": "string", "required": True},
            ],
        },
        {
            "name": "unschedule",
            "tool": "events_unschedule",
            "description": "Remove a game's schedule (stops its ticks).",
            "params": [
                {"name": "game", "type": "string", "required": True},
            ],
        },
        {
            "name": "list",
            "tool": "events_list",
            "description": "List all active game schedules.",
        },
    ],
    "emitters": [
        {
            "topic": "schedule.tick",
            "description": "Fired (jittered) when a game's schedule is due; "
                           "payload {game}.",
        },
    ],
    "sessions": [],
}


def _make_handlers(events: EventsDAO) -> dict[str, Any]:
    return {
        "schedule": lambda args: events.schedule(args["game"], args["cron"]),
        "unschedule": lambda args: {
            "removed": events.unschedule(args["game"])},
        "list": lambda args: {"schedules": events.list_schedules()},
    }


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    events = EventsDAO()
    adapter = ServiceAdapter(
        "events", API_MANIFEST, _make_handlers(events), on_start=dao.init,
    )
    scheduler = Scheduler(adapter, dao=events)
    # The adapter's connection loop and the cadence loop run concurrently; the
    # scheduler emits through the adapter's live control WS (no-op while down).
    await asyncio.gather(adapter.run(), scheduler.run())


if __name__ == "__main__":
    asyncio.run(main())
