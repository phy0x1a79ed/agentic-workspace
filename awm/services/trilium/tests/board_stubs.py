"""A stub of alcor's side for the board tests: the operations verbs and telemetry's, in memory.

The real verbs are built by port-ops and the telemetry plugin. This stub holds the shapes that
``docs/trilium-design/CONTRACT.md`` says trilium reads, so a test fails when the writer reads
something else.
"""

from __future__ import annotations

from typing import Any

from awm.trilium import etapi
from awm.trilium.board import ids
from awm.trilium.board.fence import Fence
from awm.trilium.board.remote import Remote

from .fake_vault import FakeVault


def card(task_id: str = "t1", section: str = "working", **kw: Any) -> dict[str, Any]:
    base = {"task_id": task_id, "title": f"Card {task_id}", "kind": "task", "section": section,
            "status": None, "peer": "mizar", "tags": [], "meta": {}, "parent_task": None,
            "created": 1000.0, "updated": 2000.0, "done_at": None, "sessions": [],
            "approvals": [], "source_note": None}
    base.update(kw)
    return base


class Alcor:
    """operations and telemetry as alcor serves them."""

    def __init__(self) -> None:
        self.events: list[tuple[int, dict[str, Any]]] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.nodes: list[dict] = []
        self.services: list[dict] = []
        self.seats: list[dict] = []
        self.notices: list[dict] = []
        self.import_answer: dict[str, Any] = {"ok": True}
        self.imports: list[dict] = []
        self.humans: list[str] = []

    def emit(self, c: dict[str, Any]) -> int:
        n = len(self.events) + 1
        self.events.append((n, c))
        return n

    def call(self, tool: str, verb: str, args: dict | None = None, *, peer: str | None = None,
             timeout: float = 30, **_: Any) -> Any:
        args = args or {}
        self.calls.append((tool, verb, args))
        assert peer == "alcor", "the board reads alcor, never the local node"
        if (tool, verb) == ("operations", "board_changes"):
            after, limit = args["after"], args["limit"]
            fresh = [(n, c) for n, c in self.events if n > after]
            batch = fresh[:limit]
            latest: dict[Any, dict] = {}
            for _, c in batch:
                latest[c.get("task_id") or id(c)] = c
            return {"cards": list(latest.values()), "mark": batch[-1][0] if batch else after,
                    "more": len(fresh) > limit}
        if (tool, verb) == ("operations", "board_import"):
            self.imports.append(args)
            return self.import_answer
        if (tool, verb) == ("operations", "request_human"):
            self.humans.append(args["reason"])
            return {"ok": True}
        if (tool, verb) == ("operations", "ops_list"):
            return {"notes": self.notices}
        if tool == "telemetry":
            return {"nodes": self.nodes, "services": self.services,
                    "seats": self.seats}[verb]
        raise AssertionError(f"unexpected {tool} {verb}")


def make_vault(monkeypatch) -> tuple[FakeVault, Fence]:
    fake = FakeVault()
    monkeypatch.setattr(etapi.httpx, "request", fake.request)
    fence = Fence()
    fence.c.create_note(parent_note_id="root", title="AWM", type="book", content="",
                        note_id=ids.BOOK_ID)
    return fake, fence


def remote(alcor: Alcor) -> Remote:
    return Remote("alcor", alcor.call)
