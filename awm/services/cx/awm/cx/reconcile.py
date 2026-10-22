"""Keeping one session warm, on one rule.

The rule is that a session is replaced when it stops being claimable, and age
is just one of the reasons for that. There is no separate rotation schedule and
no stored pointer to "the current spare" — the pointer is whichever claimable
session is newest, and a tick that finds fewer claimable sessions than it wants
seeds one.

Order within a tick is seed, then remove. That is what makes "create the new
one, move the pointer, delete the old one" true without anything having to
sequence it: an aged session has already stopped counting as claimable, so its
replacement is seeded first, and only then does removal collect it.

Two guards keep a second copy of this service from seeding against the same
home directory. The overlay flag covers `awm dev shadow`. It is not enough on
its own, because shadowing a service whose base is not yet registered brings it
up as a *base* with the flag gone — exactly the situation while a new service
is being built — and a dev sandbox bootstraps its own copy of every service
against the same home. So the tick also takes a non-blocking exclusive lock,
which covers the overlay, the sandbox, a stray manual run, and a predecessor
that has not finished dying, with one mechanism.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
from typing import Any

from awm.cx import config, lifecycle, pool, remove, seed, sessions

log = logging.getLogger("awm.cx.reconcile")

#: How long the same refusal is swallowed before it is logged again. A tick is
#: seconds; a node with no daemon would otherwise write a line a second forever.
QUIET_S = 600.0


class Loop:
    """The reconcile loop, and everything `status` reports about it."""

    def __init__(self) -> None:
        self._lock_fd: int | None = None
        self._last_tick: float | None = None
        self._last_refusal: tuple[str, float] | None = None
        self._kick = asyncio.Event()
        self._running = False

    # -- what the adapter calls -------------------------------------------

    def held_back(self) -> str | None:
        """Why this process must not run the loop at all, or None if it may."""
        if os.environ.get("AWM_SERVICE_OVERLAY"):
            return "running as an overlay; the base owns the pool"
        return None

    def enabled(self) -> str | None:
        """Why this process must not tend the warm pool, or None if it may.

        Switching the pool off leaves the lineage bookkeeping running: a start
        that outlives its launch is adopted only here, and without it cx refuses
        to stop or resume that job.
        """
        if not config.loop_enabled():
            return "switched off (AWM_CX_LOOP=0)"
        return self.held_back()

    async def run(self) -> None:
        """Sleep, then work, forever. Never returns; never raises."""
        self._running = True
        while True:
            await self._wait(config.tick_s())
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — a tick must never end the loop
                log.exception("cx: reconcile tick failed")

    def kick(self) -> None:
        """Run the next tick now. Called after a claim, so the replacement is
        seeded while the caller is still starting up rather than up to a tick
        later."""
        self._kick.set()

    def status(self) -> dict[str, Any]:
        return {
            "loop": self.held_back() or ("running" if self._running else "not started"),
            "seeding_loop": self.enabled() or "on",
            "holds_lock": self._lock_fd is not None,
            "last_tick": (None if self._last_tick is None
                          else round(time.time() - self._last_tick, 1)),
        }

    # -- one tick ----------------------------------------------------------

    async def tick(self) -> None:
        if self.held_back() is not None or not self._take_lock():
            return
        if self.enabled() is None:
            await self._tend_pool()
        for step, message in (
                (lifecycle.adopt_pending, "adopted %s for a start that outlived its launch"),
                (lifecycle.prune_pins, "unpinned %s, whose job is gone"),
                (lifecycle.prune_lineage, "dropped the lineage of %s, which was removed")):
            try:
                for job in step():
                    log.info("cx: " + message, job)
            except Exception:  # noqa: BLE001 — one step must not skip the others
                log.exception("cx: %s failed", step.__name__)
        self._last_tick = time.time()

    async def _tend_pool(self) -> None:
        try:
            version = sessions.binary_version()
            have = len(pool.warm(sessions.load(), version=version))
            if have < config.want():
                await self._seed()
        except Exception:  # noqa: BLE001 — the bookkeeping below must still run
            log.exception("cx: seeding failed")
        try:
            # Under the pool lock so a claim in flight cannot have its session
            # deleted out from under it between the move and the reply.
            async with pool.LOCK:
                done = await remove.apply()
        except Exception:  # noqa: BLE001
            log.exception("cx: removal failed")
            return
        for item in done:
            log.info("cx: %s %s (%s)", "removed" if item["removed"] else "kept",
                     item["session"], item["why"])

    async def _seed(self) -> None:
        try:
            await seed.seed_one()
        except seed.Refused as exc:
            self._note_refusal(str(exc))
        except TimeoutError as exc:
            log.warning("cx: %s", exc)

    def _note_refusal(self, reason: str) -> None:
        last = self._last_refusal
        if last and last[0] == reason and time.time() - last[1] < QUIET_S:
            return
        log.info("cx: not seeding — %s", reason)
        self._last_refusal = (reason, time.time())

    # -- mutual exclusion --------------------------------------------------

    def _take_lock(self) -> bool:
        """Hold the pool's lock, taking it if this process does not have it.

        Held for the life of the process rather than per tick. The kernel drops
        it when the process dies, so a copy that was killed mid-tick hands the
        pool over with no cleanup pass and nothing to time out.
        """
        if self._lock_fd is not None:
            return True
        path = config.state_dir() / "reconcile.lock"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            log.warning("cx: cannot open the reconcile lock: %s", exc)
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        log.info("cx: holding the reconcile lock")
        return True

    # -- sleeping ----------------------------------------------------------

    async def _wait(self, seconds: float) -> None:
        """Sleep, cut short by a kick."""
        try:
            await asyncio.wait_for(self._kick.wait(), timeout=seconds)
        except (TimeoutError, asyncio.TimeoutError):
            return
        finally:
            self._kick.clear()
