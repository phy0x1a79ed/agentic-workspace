"""The board writer and the Ops renderer, fed by alcor's pushes (fleet.md R79).

deneb makes no outbound call. operations on alcor calls ``board_apply`` at each card event and
telemetry calls ``ops_render`` at each roster change, through the door alcor already uses.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from awm.trilium.board import install
from awm.trilium.board.fence import Fence
from awm.trilium.board.ops import OpsRenderer
from awm.trilium.board.writer import Writer


class Board:
    def __init__(self, fence: Fence, clock: Callable[[], float] = time.time) -> None:
        self.fence, self.clock = fence, clock
        self.writer = Writer(fence)
        self.ops = OpsRenderer(fence)
        self.installed = False
        self._lock = threading.Lock()

    def _ready(self) -> None:
        with self._lock:
            if not self.installed:
                install.install(self.fence)
                self.installed = True

    def apply(self, args: dict[str, Any]) -> dict[str, Any]:
        self._ready()
        out = self.writer.apply(args.get("base"), args.get("cards") or [], args.get("mark"))
        if out["applied"]:
            self.writer.set_pushed(self.clock())
        return out

    def render(self, args: dict[str, Any]) -> dict[str, Any]:
        self._ready()
        self.ops.render(args)
        return {"ok": True}
