"""Run the board writer and the Ops renderer from events (no timer, no poll).

Two subscriptions on alcor feed it: ``operations/board`` drains card changes and
``telemetry/roster`` re-renders Ops. Each stream opens with a synthetic ``connected`` item, so a
subscriber that was away reads the current state once, as the contract says.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, AsyncIterator, Callable

from awm import gatewayclient
from awm.trilium.board import ids, install, migrate
from awm.trilium.board.fence import Fence
from awm.trilium.board.ops import OpsRenderer
from awm.trilium.board.remote import Remote, peer
from awm.trilium.board.writer import Writer

log = logging.getLogger("awm.trilium.board.service")

ENABLE_ENV = "AWM_TRILIUM_BOARD"


def enabled() -> bool:
    """Exactly one node writes the board (INSTALL.md): the one that sets this to ``writer``."""
    return os.environ.get(ENABLE_ENV, "").strip().lower() == "writer"


class Board:
    def __init__(self, fence: Fence, remote: Remote, snapshot: Callable[[str], Any],
                 clock: Callable[[], float] = time.time) -> None:
        self.fence, self.remote, self.clock = fence, remote, clock
        self.writer = Writer(fence, remote)
        self.ops = OpsRenderer(fence, remote)
        self.snapshot = snapshot
        self.started = False
        self._lock = threading.Lock()

    def start(self) -> dict[str, Any]:
        """Install the pages, then migrate the first board once. Runs again until it succeeds."""
        with self._lock:
            out = {"install": install.install(self.fence)}
            out["migrate"] = migrate.migrate(self.fence, self.remote, self.snapshot)
            self.started = True
            return out

    def _ready(self) -> None:
        if not self.started:
            self.start()

    def on_board(self, event: Any) -> int:
        self._ready()
        return self.writer.drain()

    def on_roster(self, event: Any) -> dict[str, Any]:
        self._ready()
        return self.ops.render()

    def live(self, up: bool) -> None:
        try:
            self.writer.set_live(up, self.clock())
        except Exception:  # noqa: BLE001 - a header label never stops the stream
            log.exception("could not record the queue link")


def _stream(topic_service: str, topic: str, board: Board, track: bool):
    """A stream factory: one ``connected`` item, then the topic's events."""
    async def stream() -> AsyncIterator[Any]:
        async def connected() -> None:
            if track:
                await asyncio.to_thread(board.live, True)
        try:
            await connected()
            yield {"event": "connected"}
            async for ev in gatewayclient.subscribe(topic_service, topic, peer=peer()):
                yield ev
        finally:
            if track:
                await asyncio.to_thread(board.live, False)
    return stream


def subscriptions(board: Board) -> list[gatewayclient.SupervisedSubscription]:
    async def on_board(ev: Any) -> None:
        await asyncio.to_thread(board.on_board, ev)

    async def on_roster(ev: Any) -> None:
        await asyncio.to_thread(board.on_roster, ev)

    return [
        gatewayclient.SupervisedSubscription(
            "trilium:board", _stream("operations", "board", board, True), on_board),
        gatewayclient.SupervisedSubscription(
            "trilium:roster", _stream("telemetry", "roster", board, False), on_roster),
    ]


async def run(board: Board) -> None:
    """Run both subscriptions. A failed handler is counted by the subscription and the next
    event retries, so a start that could not finish (alcor down) completes on the next event."""
    await asyncio.gather(*(s.run() for s in subscriptions(board)))


def build(snapshot: Callable[[str], Any]) -> Board:
    return Board(Fence(), Remote(), snapshot)
