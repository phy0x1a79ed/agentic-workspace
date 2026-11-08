"""Fixtures for the board tests: a card in the shape operations pushes, and a fake vault."""

from __future__ import annotations

from typing import Any

from awm.trilium import etapi
from awm.trilium.board import ids
from awm.trilium.board.fence import Fence

from .fake_vault import FakeVault


def card(task_id: str = "t1", section: str = "working", **kw: Any) -> dict[str, Any]:
    base = {"task_id": task_id, "title": f"Card {task_id}", "kind": "task", "section": section,
            "status": None, "peer": "mizar", "tags": [], "meta": {}, "parent_task": None,
            "created": 1000.0, "updated": 2000.0, "done_at": None, "sessions": [],
            "approvals": [], "source_note": None}
    base.update(kw)
    return base


def make_vault(monkeypatch) -> tuple[FakeVault, Fence]:
    fake = FakeVault()
    monkeypatch.setattr(etapi.httpx, "request", fake.request)
    fence = Fence()
    fence.c.create_note(parent_note_id="root", title="AWM", type="book", content="",
                        note_id=ids.BOOK_ID)
    return fake, fence
