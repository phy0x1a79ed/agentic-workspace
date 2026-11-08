"""The one-time move from the first board to the second (operations port plan, card migration).

Before the new writer first writes, trilium snapshots the vault, sends operations every
``#swarmCard`` and every Ops notice in one ``board_import``, and only on success reshapes the
vault: Proposed, Done and Swarm triage folders empty into History and go. A mismatch files a
``request_human`` that names the missing and extra ids, and the vault stays as it is.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from awm.trilium.board import ids
from awm.trilium.board.fence import Fence
from awm.trilium.board.install import ensure_tasks
from awm.trilium.board.remote import Remote

log = logging.getLogger("awm.trilium.board.migrate")

LAYOUT_LABEL = "boardLayout"
LAYOUT = "2"
NOTE_PAGE = 100_000


class ImportRefused(RuntimeError):
    """operations did not accept the import; the vault is untouched."""


def _labels(note: dict[str, Any]) -> dict[str, str]:
    return {a["name"]: a.get("value", "") for a in note.get("attributes") or []
            if a.get("type") == "label" and a.get("noteId", note.get("noteId")) == note.get("noteId")}


def layout_done(fence: Fence) -> bool:
    tasks = fence.note(ids.TASKS_ID)
    return bool(tasks) and _labels(tasks).get(LAYOUT_LABEL) == LAYOUT


def collect_cards(fence: Fence) -> list[dict[str, Any]]:
    """Every card note, archived ones included. A note-kind card carries its body."""
    cards = []
    for hit in fence.search("#swarmCard", ancestorNoteId=ids.TASKS_ID, limit=NOTE_PAGE,
                            includeArchivedNotes="true", fastSearch="true"):
        lab = _labels(hit)
        if not lab.get("taskId"):
            continue
        card = {"task_id": lab["taskId"], "section": lab.get("section", ""),
                "kind": lab.get("cardKind", "task"), "title": hit.get("title", "")}
        if card["kind"] == "note":
            card["body"] = fence.content(hit["noteId"])
        cards.append(card)
    return sorted(cards, key=lambda c: c["task_id"])


def collect_notices(fence: Fence) -> list[dict[str, Any]]:
    """The Ops notices the first board kept as managed notes: ``<kind> - <node>``."""
    fixed = {ids.ROSTER_ID, ids.PORTALS_ID, ids.OPS_PAGE_ID}
    out = []
    ops = fence.note(ids.OPS_ID)
    if ops is None:
        return out
    for kid in fence.children(ids.OPS_ID):
        if kid["noteId"] in fixed or "agentManaged" not in _labels(kid):
            continue
        kind, _, node = (kid.get("title") or "").partition(" - ")
        out.append({"kind": kind, "node": node, "title": kind,
                    "body": fence.content(kid["noteId"])})
    return out


def import_cards(fence: Fence, remote: Remote, snapshot: Callable[[str], Any],
                 accept_gap: bool = False) -> dict[str, Any]:
    cards, notices = collect_cards(fence), collect_notices(fence)
    snapshot("pre-board-import")
    out = remote.board_import(cards, notices, accept_gap)
    if out.get("ok") is False:
        missing, extra = out.get("missing") or [], out.get("extra") or []
        remote.request_human(
            "board_import did not match the vault. Missing from the vault: "
            f"{', '.join(missing) or 'none'}. Extra in the vault: {', '.join(extra) or 'none'}. "
            "Fix the vault, or accept the gap.")
        raise ImportRefused(f"missing {missing}, extra {extra}")
    return {"cards": len(cards), "notices": len(notices)}


def relayout(fence: Fence) -> dict[str, int]:
    """Empty the first board's folders into History and remove them."""
    ensure_tasks(fence)
    moved = 0
    for _, folder in ids.LEGACY_COLUMNS:
        if fence.note(folder) is None:
            continue
        for kid in fence.children(folder):
            lab = _labels(kid)
            fence.move(kid["noteId"], ids.COLUMN_IDS["history"])
            if "swarmCard" in lab:
                at = lab.get("doneAt") or lab.get("updatedAt")
                fence.update(kid["noteId"], labels={"section": "history", "historyAt": at,
                                                    "doneAt": None})
            moved += 1
        if not fence.children(folder):
            fence.delete(folder)
    fence.update(ids.TASKS_ID, labels={LAYOUT_LABEL: LAYOUT})
    return {"moved": moved}


def migrate(fence: Fence, remote: Remote, snapshot: Callable[[str], Any]) -> dict[str, Any]:
    if layout_done(fence):
        return {"skipped": True}
    ensure_tasks(fence)
    imported = import_cards(fence, remote, snapshot)
    return {**imported, **relayout(fence)}
