"""The task sweep's ticker.

Sleep, tick, never die on an exception, and hold a non-blocking flock for the
life of the process so a second copy of the service (an overlay, a dev sandbox,
a stray manual run) cannot sweep the same node twice. A tick asks cx which
sessions exist and hands the rows to the sweep. When cx gives no answer it does
nothing, because the sweep closes the tasks of sessions it cannot find.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import time
from typing import Any, Awaitable, Callable

from awm.reporting import config, sessions

log = logging.getLogger("awm.reporting.reconcile")

#: How long the same note is swallowed before it is logged again.
QUIET_S = 600.0


class Loop:
    def __init__(self, cx: Any = None, interval_s: float | None = None,
                 on_rows: Callable[[list[dict]], Awaitable[None]] | None = None) -> None:
        self.cx = cx or sessions.Cx()
        self._on_rows = on_rows
        self._interval_s = interval_s
        self._lock_fd: int | None = None
        self._last_tick: float | None = None
        self._notes: dict[str, float] = {}
        self._running = False

    async def run(self) -> None:
        """Tick, then sleep, forever. Never returns, never raises."""
        self._running = True
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a tick must never end the loop
                log.exception("reporting: reconcile tick failed")
            await asyncio.sleep(self._interval_s or config.reconcile_interval_s())

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "holds_lock": self._lock_fd is not None,
            "last_tick_s_ago": (None if self._last_tick is None
                                else round(time.time() - self._last_tick, 1)),
        }

    async def tick(self) -> None:
        why = config.task_refusal()
        if why is not None:
            self._note("refused", f"not reconciling: {why}")
            return
        if not await asyncio.to_thread(self._take_lock):
            return
        try:
            rows = await self.cx.list()
        except sessions.CxUnavailable as exc:
            self._note("cx", f"not reconciling: {exc}")
            return
        if self._on_rows is not None:
            try:
                await self._on_rows(rows)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("reporting: task sweep failed")
        self._last_tick = time.time()

    def _note(self, key: str, message: str) -> None:
        last = self._notes.get(key)
        if last is not None and time.time() - last < QUIET_S:
            return
        log.info("reporting: %s", message)
        self._notes[key] = time.time()

    def _take_lock(self) -> bool:
        """Hold the reconcile lock, taking it if this process does not have it.

        Held for the life of the process. The kernel drops it when the process
        dies, so a killed copy hands over with no cleanup pass.
        """
        if self._lock_fd is not None:
            return True
        path = config.state_dir() / "reconcile.lock"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            self._note("lock", f"cannot open the reconcile lock: {exc}")
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            self._note("lock", "another door holds the reconcile lock; not reconciling")
            return False
        self._lock_fd = fd
        log.info("reporting: holding the reconcile lock")
        return True
