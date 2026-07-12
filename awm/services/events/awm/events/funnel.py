"""The hook→wake funnel — realm/effector events normalized into agent wakes.

The events service's second half (the Scheduler being the first): a set of
never-die consumer loops over other services' emitters that filter to the
wake-worthy kinds, debounce per game, and re-emit one normalized ``agent.wake``
``{game, reason, source}`` — the single topic the agents service's gamebot
listener consumes. Adding a wake source (a browser realm, an effector) is one
row in :data:`SOURCES`.

Runs as a sibling task next to the adapter + scheduler
(``asyncio.gather(adapter.run(), scheduler.run(), Funnel(adapter).run())``) and
emits through the adapter's live control WS — a safe no-op while it is down
(same best-effort contract as the scheduler's ticks). While a source service is
absent from the gateway the subscribe just fails and retries with backoff, so
the funnel is inert until the realm shows up.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from time import monotonic

log = logging.getLogger("awm.events.funnel")

WAKE_TOPIC = "agent.wake"
DEBOUNCE_S = 60.0


@dataclass(frozen=True)
class WakeSource:
    """One emitter to watch: which service/topic, which payload kinds wake,
    and which game the wake is attributed to (one realm session per game)."""

    service: str
    topic: str
    game: str
    kinds: frozenset[str] = field(
        default_factory=lambda: frozenset({"died", "error"}))


# The wake-source table. rlm-factorio fires its bare `factorio` topic with
# {session_id, kind, data} (dotted rlm.factorio.<kind> is a projection —
# the WS topic is the emitter name); a browser game is one more row.
SOURCES: list[WakeSource] = [
    WakeSource(service="rlm-factorio", topic="factorio", game="factorio"),
]


class Funnel:
    """Drives the wake-source consumer loops and the per-game debounce."""

    def __init__(self, adapter, *, sources: list[WakeSource] | None = None,
                 debounce_s: float = DEBOUNCE_S) -> None:
        self.adapter = adapter
        self.sources = SOURCES if sources is None else sources
        self.debounce_s = debounce_s
        # game -> monotonic time of the last emitted wake.
        self._last_wake: dict[str, float] = {}

    # -- one event (unit-testable, no sockets) -------------------------------

    def _classify(self, src: WakeSource, ev: object) -> str | None:
        """Return the wake reason for one raw event, or None to drop it."""
        if not isinstance(ev, dict):
            return None
        kind = str(ev.get("kind") or "").strip()
        if kind not in src.kinds:
            return None
        return kind

    def _debounced(self, game: str, now: float | None = None) -> bool:
        """True (and stamped) if a wake for ``game`` may fire now."""
        t = monotonic() if now is None else now
        last = self._last_wake.get(game)
        if last is not None and (t - last) < self.debounce_s:
            return False
        self._last_wake[game] = t
        return True

    async def _handle(self, src: WakeSource, ev: object) -> bool:
        reason = self._classify(src, ev)
        if reason is None:
            return False
        if not self._debounced(src.game):
            log.debug("funnel: %s wake for %s debounced", reason, src.game)
            return False
        log.info("funnel: %s/%s kind=%s → agent.wake game=%s",
                 src.service, src.topic, reason, src.game)
        await self.adapter.emit(WAKE_TOPIC, {
            "game": src.game, "reason": reason,
            "source": f"{src.service}/{src.topic}",
        })
        return True

    # -- consumer loops -------------------------------------------------------

    async def _listen(self, src: WakeSource) -> None:
        """One never-die consumer loop (reconnect/backoff — the 2fa envelope)."""
        from awm import gatewayclient

        log.info("funnel: subscribing to %s/%s (game=%s)",
                 src.service, src.topic, src.game)
        backoff = 2.0
        while True:
            try:
                async for ev in gatewayclient.subscribe(src.service, src.topic):
                    backoff = 2.0  # connected and receiving
                    await self._handle(src, ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never let the loop die
                log.debug("funnel: %s/%s subscription dropped (retrying): %s",
                          src.service, src.topic, exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)

    async def run(self) -> None:
        if not self.sources:
            return
        log.info("funnel: %d wake source(s)", len(self.sources))
        await asyncio.gather(*(self._listen(s) for s in self.sources))
