"""A supervised, self-healing subscription over a gateway emitter topic.

``subscribe`` / ``subscribe_peer`` / ``subscribe_maybe_peer`` each deliver
*one connection's worth* of events and then return — their docstrings say so
explicitly, and forbid an inner reconnect loop, because a mid-stream
credential rotation must surface as a reconnect the caller controls. Every
long-lived consumer therefore has to write its own reconnect loop, and until
now three of them (ssh, 2fa, gamebot) each hand-rolled a byte-identical copy
with the same four defects. This module is that loop, written once.

What it corrects over the hand-rolled version:

* **Backoff resets on a connection that lasted, not on a received event.**
  The old loop reset inside the iteration body, so a topic that is silent for
  days — ``social/command``, which carries the operator's ``/approve`` — kept
  ratcheting its backoff to the 30 s cap even while connecting perfectly.
* **Handler exceptions are isolated.** One malformed event used to abort the
  whole stream; now it increments a counter and the stream continues.
* **Repeated failure escalates to ERROR** naming the subscription, so a
  permanently broken wire eventually shouts instead of merely warning.
* **An idle deadline forces a re-subscribe.** This is what actually bounds
  deafness: it heals every staleness class, including ones nobody has
  modelled, without needing to diagnose the cause. It re-establishes the
  *event subscription* only — a cheap, idempotent listener with no side
  effects anywhere — so it is safe to make aggressive.

Re-subscribe is deliberately **break-before-make**: the old stream is closed
before the replacement opens. Overlapping subscriptions would deliver each
event twice, and at least one consumer (ssh's ``/approve`` recovery window)
is one-shot by design — a duplicate would re-open a consumed window and
authorise a second multi-factor push, which is precisely the hazard the
one-shot exists to prevent.

Every health accessor is non-raising: health reporting has to work precisely
when things are broken.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from typing import Any, AsyncIterator, Callable

log = logging.getLogger("awm.gatewayclient.subscription")

__all__ = ["SupervisedSubscription", "spawn_supervised"]

# Consecutive failed connections before a WARNING becomes an ERROR. Five at
# the 2 s→30 s backoff is roughly a minute of sustained failure — long enough
# that a service restart on the far end doesn't page, short enough that a
# genuinely broken wire is loud well before anyone needs it.
_ERROR_AFTER = 5


def spawn_supervised(
    name: str,
    factory: Callable[[], Any],
    *,
    respawn_delay: float = 5.0,
) -> asyncio.Task:
    """Spawn a long-lived background task that is noticed if it dies.

    Services used to do ``self._x_task = asyncio.create_task(self._x())`` and
    then never read ``_x_task`` anywhere — so a task that raised on its very
    first line left the service running, apparently healthy, with that whole
    capability silently absent. This wrapper logs at ERROR and respawns.

    ``factory`` returns a fresh coroutine each time; cancellation propagates.
    """
    async def _supervise() -> None:
        while True:
            try:
                await factory()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the whole point
                log.error("supervised task %s died — respawning in %.0fs",
                          name, respawn_delay, exc_info=True)
            else:
                log.error("supervised task %s returned unexpectedly — "
                          "respawning in %.0fs", name, respawn_delay)
            await asyncio.sleep(respawn_delay)

    return asyncio.create_task(_supervise(), name=f"supervised:{name}")


class SupervisedSubscription:
    """A named, self-healing consumer of one emitter topic.

    ``stream_factory`` is a zero-argument callable returning a fresh async
    iterator of events — typically ``lambda: subscribe_maybe_peer(...)``.
    Taking a factory rather than importing the subscribe functions keeps this
    module free of a circular import and makes it testable against fake
    streams with no monkeypatching. Read any peer selector *inside* the
    closure so a reconnect picks up an environment change without a restart.

    ``handler`` is called once per event and may be sync or async.

    ``intercept``, if given, is called before ``handler`` and may claim an
    event by returning true — used for synthetic self-test probes that must
    never reach the domain handler.
    """

    def __init__(
        self,
        name: str,
        stream_factory: Callable[[], AsyncIterator[Any]],
        handler: Callable[[Any], Any],
        *,
        idle_timeout: float = 300.0,
        idle_jitter: float = 0.25,
        stability_seconds: float = 30.0,
        backoff_initial: float = 2.0,
        backoff_max: float = 30.0,
        backoff_factor: float = 1.5,
        intercept: Callable[[Any], bool] | None = None,
    ) -> None:
        self.name = name
        self._factory = stream_factory
        self._handler = handler
        self._intercept = intercept
        self._idle_timeout = idle_timeout
        self._idle_jitter = idle_jitter
        self._stability = stability_seconds
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._backoff_factor = backoff_factor

        self.connected = False
        self.connects = 0
        self.reconnects = 0
        self.events = 0
        self.handler_errors = 0
        self.consecutive_failures = 0
        self.last_connect_ts: float | None = None
        self.last_event_ts: float | None = None
        self.last_error: str | None = None

    # -- health -------------------------------------------------------------

    @property
    def healthy(self) -> bool:
        """Connected, with no run of failures behind us.

        Deliberately *not* a function of when the last event arrived: a
        recovery topic is legitimately silent for weeks, and the idle deadline
        already replaces a socket that has gone quiet.
        """
        return bool(self.connected) and self.consecutive_failures == 0

    def health(self) -> dict[str, Any]:
        """A status-block dict. Never raises — callers report health when
        things are broken, which is the worst possible time to throw."""
        try:
            now = time.time()
            return {
                "name": self.name,
                "connected": self.connected,
                "healthy": self.healthy,
                "connects": self.connects,
                "reconnects": self.reconnects,
                "events": self.events,
                "handler_errors": self.handler_errors,
                "consecutive_failures": self.consecutive_failures,
                "connected_at": self.last_connect_ts,
                "last_event_at": self.last_event_ts,
                "last_event_age_s": (
                    round(now - self.last_event_ts, 1)
                    if self.last_event_ts else None
                ),
                "last_error": self.last_error,
                "idle_timeout_s": self._idle_timeout,
            }
        except Exception as exc:  # noqa: BLE001 — health must not throw
            return {"name": self.name, "healthy": False,
                    "error": f"health unavailable: {exc}"}

    # -- the loop -----------------------------------------------------------

    def _idle_deadline(self) -> float:
        """The idle timeout, jittered. Un-jittered, every subscription in the
        fleet would re-subscribe on the same period and sync-storm the
        gateway."""
        spread = self._idle_timeout * self._idle_jitter
        jittered = self._idle_timeout + random.uniform(-spread, spread)
        # Floor relative to the configured timeout, never an absolute one — an
        # absolute floor silently overrides a caller who asked for a short
        # deadline.
        return max(self._idle_timeout * 0.5, jittered)

    async def run(self) -> None:
        """Consume forever, reconnecting as needed. Cancel to stop."""
        log.info("subscription %s: starting", self.name)
        backoff = self._backoff_initial
        while True:
            started = time.monotonic()
            try:
                await self._one_connection()
                # A clean return means the far end closed the stream — the
                # gateway now does exactly this when the emitting service goes
                # down, which is the signal the old loop never received.
                self.last_error = "stream closed by peer"
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as exc:  # noqa: BLE001 — never let the task die
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self.connected = False

            lasted = time.monotonic() - started
            if lasted >= self._stability:
                # The connection itself was fine; whatever ended it is not a
                # reason to back off. Resetting here (rather than on a received
                # event) is what keeps a rarely-used topic from ratcheting to
                # the cap.
                backoff = self._backoff_initial
                self.consecutive_failures = 0
                log.warning("subscription %s dropped after %.0fs (retrying "
                            "in %.0fs): %s",
                            self.name, lasted, backoff, self.last_error)
            else:
                self.consecutive_failures += 1
                level = (log.error if self.consecutive_failures >= _ERROR_AFTER
                         else log.warning)
                level("subscription %s failed after %.1fs "
                      "(%d consecutive, retrying in %.0fs): %s",
                      self.name, lasted, self.consecutive_failures,
                      backoff, self.last_error)

            await asyncio.sleep(backoff)
            backoff = min(backoff * self._backoff_factor, self._backoff_max)

    async def _one_connection(self) -> None:
        """One stream, consumed until it ends or goes idle past the deadline.

        The idle branch closes the current stream *before* returning, so the
        caller's reconnect is break-before-make.
        """
        gen = self._factory()
        self.connects += 1
        if self.connects > 1:
            self.reconnects += 1
        self.connected = True
        self.last_connect_ts = time.time()
        log.info("subscription %s: connected (attempt #%d)",
                 self.name, self.connects)
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(anext(gen),
                                                timeout=self._idle_deadline())
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError:
                    log.warning(
                        "subscription %s: no event for %.0fs — re-subscribing "
                        "(the socket looked fine, which is the failure mode)",
                        self.name, self._idle_timeout)
                    return
                self.events += 1
                self.last_event_ts = time.time()
                await self._dispatch(ev)
        finally:
            # Break before make: the replacement stream must not overlap this
            # one, or a one-shot event gets delivered twice.
            try:
                await gen.aclose()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass

    async def _dispatch(self, ev: Any) -> None:
        """Run one event through intercept + handler, isolating failures.

        A handler that raises must not end the stream — under the old loop a
        single malformed event took the whole subscription down and the
        service went deaf until it was restarted.
        """
        try:
            if self._intercept is not None and self._intercept(ev):
                return
            result = self._handler(ev)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self.handler_errors += 1
            log.warning("subscription %s: handler raised on one event "
                        "(continuing)", self.name, exc_info=True)
