"""The cadence engine — a background asyncio loop that turns persisted schedules
into ``schedule.tick`` emissions.

The :class:`ServiceAdapter` has no background-task hook (``on_start`` is one-shot
and ``run`` just gathers its connection loops), so the scheduler runs as a
sibling task: ``asyncio.gather(adapter.run(), Scheduler(adapter).run())``. It
holds the adapter only to call :meth:`ServiceAdapter.emit`, which is a safe
no-op while the control WS is down — a tick due during a reconnect is simply
dropped (emit is best-effort live signalling, never durable delivery).

Design notes:

* **Reconcile by polling.** Schedules are re-read from SQLite each iteration
  (bounded by ``POLL_MAX_S``), so a ``schedule`` / ``unschedule`` mutation —
  which arrives on a worker thread via the adapter dispatch — is picked up
  within one poll without any cross-thread signalling.
* **No drift, no catch-up storm.** Each game's next-due is recomputed from the
  schedule spec at fire time, not from "now + jitter". If the loop wakes late
  and several intervals have elapsed, the game fires once and skips ahead.
* **Jitter decoupled.** Each due game fires in its own task that sleeps a bounded
  random jitter before emitting, so jitter on one game never delays another.
"""

from __future__ import annotations

import asyncio
import logging
import random
from time import time

from awm.events import cron
from awm.events.dao import EventsDAO

log = logging.getLogger("awm.events.scheduler")

TOPIC = "schedule.tick"
POLL_MAX_S = 2.0          # cap on the inter-iteration sleep (reconcile latency)
DEFAULT_JITTER_S = 2.0    # max random delay applied before each emit


class Scheduler:
    """Drives schedule.tick emissions for every persisted schedule."""

    def __init__(self, adapter, *, jitter_s: float = DEFAULT_JITTER_S,
                 dao: EventsDAO | None = None) -> None:
        self.adapter = adapter
        self.jitter_s = jitter_s
        self.dao = dao or EventsDAO()
        # game -> next due epoch second. Rebuilt incrementally each poll.
        self._next_due: dict[str, float] = {}
        # game -> the cron spec we last computed next_due from (detect changes).
        self._specs: dict[str, str] = {}

    # -- one reconcile + fire pass (unit-testable, no sleeping) -------------

    def _reconcile(self, now: float) -> list[str]:
        """Sync in-memory state to the DB and return the games due at ``now``.

        New schedules get a next-due computed from ``now``; changed specs are
        recomputed; removed schedules are dropped. A game whose next-due has
        arrived is returned and its next-due advanced so the same pass (and the
        next) won't refire it.
        """
        rows = {r["game"]: r["cron"] for r in self.dao.list_schedules()}

        # Drop schedules that no longer exist.
        for gone in set(self._next_due) - set(rows):
            self._next_due.pop(gone, None)
            self._specs.pop(gone, None)

        # Add new / recompute changed.
        for game, spec in rows.items():
            if game not in self._next_due or self._specs.get(game) != spec:
                try:
                    self._next_due[game] = cron.next_due(spec, after=now)
                    self._specs[game] = spec
                except cron.CronError as exc:
                    log.warning("events: bad cron for %s (%r): %s",
                                game, spec, exc)
                    self._next_due.pop(game, None)
                    self._specs.pop(game, None)

        # Collect + advance the due ones.
        due: list[str] = []
        for game, when in list(self._next_due.items()):
            if when <= now:
                due.append(game)
                try:
                    self._next_due[game] = cron.next_due(self._specs[game], after=now)
                except cron.CronError:
                    self._next_due.pop(game, None)
                    self._specs.pop(game, None)
        return due

    def _sleep_for(self, now: float) -> float:
        """Seconds to sleep until the soonest next-due, capped to POLL_MAX_S."""
        if not self._next_due:
            return POLL_MAX_S
        soonest = min(self._next_due.values())
        return max(0.0, min(POLL_MAX_S, soonest - now))

    # -- emit ---------------------------------------------------------------

    async def _fire(self, game: str) -> None:
        if self.jitter_s > 0:
            await asyncio.sleep(random.uniform(0, self.jitter_s))
        log.info("events: schedule.tick game=%s", game)
        await self.adapter.emit(TOPIC, {"game": game})

    # -- main loop ----------------------------------------------------------

    async def run(self) -> None:
        log.info("events: scheduler loop started")
        while True:
            now = time()
            try:
                due = await asyncio.to_thread(self._reconcile, now)
            except Exception as exc:  # never let a DB hiccup kill the loop
                log.warning("events: reconcile failed: %s", exc)
                due = []
            for game in due:
                asyncio.create_task(self._fire(game))
            await asyncio.sleep(self._sleep_for(time()))
