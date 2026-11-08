"""The verbs the board reads on alcor, through the local gateway (fleet.md R105, R87 item 3)."""

from __future__ import annotations

import os
from typing import Any, Callable

from awm import gatewayclient


def peer() -> str:
    return os.environ.get("AWM_TRILIUM_ALCOR_PEER", "alcor").strip() or "alcor"


def rows(out: Any, key: str) -> list[dict[str, Any]]:
    """The list under ``key``, or the answer itself when the verb returns a bare list."""
    if isinstance(out, dict):
        out = out.get(key)
    return [r for r in out or [] if isinstance(r, dict)]


class Remote:
    """``operations`` and ``telemetry`` on alcor. ``call`` is the seam tests replace."""

    def __init__(self, node: str | None = None,
                 call: Callable[..., Any] | None = None) -> None:
        self.node = node or peer()
        self._call = call or gatewayclient.call_sync

    def _verb(self, tool: str, verb: str, **args: Any) -> Any:
        return self._call(tool, verb, {k: v for k, v in args.items() if v is not None},
                          peer=self.node, timeout=30)

    def board_changes(self, after: int, limit: int = 200) -> dict[str, Any]:
        return self._verb("operations", "board_changes", after=int(after), limit=int(limit)) or {}

    def board_import(self, cards: list[dict], ops_notes: list[dict],
                     accept_gap: bool = False) -> dict[str, Any]:
        return self._verb("operations", "board_import", cards=cards, ops_notes=ops_notes,
                          accept_gap=accept_gap or None) or {}

    def request_human(self, reason: str) -> Any:
        return self._verb("operations", "request_human", kind="blocked", reason=reason)

    def ops_list(self) -> list[dict[str, Any]]:
        return rows(self._verb("operations", "ops_list"), "notes")

    def nodes(self) -> list[dict[str, Any]]:
        return rows(self._verb("telemetry", "nodes"), "nodes")

    def services(self) -> list[dict[str, Any]]:
        return rows(self._verb("telemetry", "services"), "services")

    def seats(self) -> list[dict[str, Any]]:
        return rows(self._verb("telemetry", "seats"), "seats")
