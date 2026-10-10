"""Waking the representative when cards arrive.

The route is the one `reflection` injects through: a line typed into the
session's PTY over the Claude Code daemon socket (`awm.claudedaemon`). The door
holds the representative's job id, so it addresses the session straight from
the roster instead of resolving a caller.

The line never carries card text. Card titles and bodies come from other
swarms, and the representative reads them through `door list` as data. A
burst of arrivals becomes one line: "N new cards, run door list".
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from awm.representative.store import Queue

log = logging.getLogger("awm.representative.notify")

QUIET_S = 600.0


def wake_text(new: int, urgent: int, waiting: int = 0) -> str:
    """One line with counts only. ``waiting`` cards were announced before and are still queued."""
    parts = []
    if new:
        noun = "card" if new == 1 else "cards"
        parts.append(f"{new} new {noun}" + (f" ({urgent} urgent)" if urgent else ""))
    if waiting:
        parts.append(f"{waiting} still waiting")
    return ", ".join(parts) + ", run door list"


def _send_line(job: str, text: str) -> None:
    from awm.claudedaemon.job import send_line

    send_line(job, text)


class Notifier:
    """Tells the live representative how many cards it has not been told about.

    A card that stays queued longer than ``reannounce_s`` is counted again, as
    "still waiting", so a representative that lost the first line still hears.
    """

    def __init__(self, queue: Queue, find_job: Callable[[], Awaitable[str | None]], *,
                 send: Callable[[str, str], None] = _send_line,
                 batch_s: float = 5.0, retry_s: float = 30.0,
                 reannounce_s: float | None = None) -> None:
        self.queue = queue
        self._find_job = find_job
        self._send = send
        self._batch_s = batch_s
        self._retry_s = retry_s
        self._reannounce_s = reannounce_s
        self._event = asyncio.Event()
        self._last_note: tuple[str, float] | None = None
        self.last_sent: str | None = None

    def poke(self) -> None:
        """A card was queued. Safe to call from any code running on the event loop."""
        self._event.set()

    async def flush(self) -> bool:
        """Send one line for every card not yet announced. True if nothing is left to send."""
        pending = self.queue.unannounced(self._reannounce_s)
        if not pending:
            return True
        job = await self._find_job()
        if job is None:
            self._note("no live representative to wake")
            return False
        fresh = [(c, p) for c, p, new in pending if new]
        text = wake_text(len(fresh), sum(1 for _, p in fresh if p == "urgent"),
                         len(pending) - len(fresh))
        try:
            await asyncio.to_thread(self._send, job, text)
        except Exception as exc:  # noqa: BLE001 — a closed PTY or a modal is a retry, not a crash
            self._note(f"could not wake {job}: {exc}")
            return False
        self.queue.mark_announced([c for c, _, _ in pending])
        self.last_sent = text
        log.info("door: woke the representative (%s): %s", job, text)
        return True

    async def run(self) -> None:
        """Wake on arrivals, and retry leftovers every retry interval. Never returns."""
        while True:
            try:
                await asyncio.wait_for(self._event.wait(), timeout=self._retry_s)
                await asyncio.sleep(self._batch_s)
            except (TimeoutError, asyncio.TimeoutError):
                pass
            self._event.clear()
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("door: notifier failed")

    def _note(self, message: str) -> None:
        last = self._last_note
        if last and last[0] == message and time.time() - last[1] < QUIET_S:
            return
        log.info("door: %s", message)
        self._last_note = (message, time.time())
