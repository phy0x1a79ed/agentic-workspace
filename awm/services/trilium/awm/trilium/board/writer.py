"""The board writer: mirror operations' cards into the vault (queue.md R61, R63 item 3, R66).

It runs inside the trilium service on the node that holds the vault. operations pushes each page
of changed cards with the mark the page follows, and the writer upserts one note per card. A
card sits in the folder of its section under ``tasks`` and moves when the section changes. The
board is write-only from here: the writer never reads a card's section or tags back.

A card started from a note (``source_note``) gets a clone of that note under it and a marker
note under the source that links back; the marker goes when the task ends (fleet.md R106 item 2).
"""

from __future__ import annotations

import logging
from typing import Any

from awm.trilium.board import ids, render
from awm.trilium.board.fence import Fence, Refused

log = logging.getLogger("awm.trilium.board.writer")

MARK = "boardMark"


class Writer:
    def __init__(self, fence: Fence) -> None:
        self.fence = fence

    # -- the mark -----------------------------------------------------------------

    def mark(self) -> int:
        tasks = self.fence.note(ids.TASKS_ID) or {}
        for a in tasks.get("attributes") or []:
            if a.get("type") == "label" and a.get("name") == MARK and a.get("noteId") == ids.TASKS_ID:
                return int(a.get("value") or 0)
        return 0

    def set_mark(self, value: int) -> None:
        self.fence.update(ids.TASKS_ID, labels={MARK: str(int(value))})

    def set_pushed(self, ts: float) -> None:
        """When alcor last pushed a card, for the board's header."""
        self.fence.update(ids.ROOT_ID, labels={"queueChangedAt": str(int(ts))})

    # -- one card -------------------------------------------------------------------

    def upsert(self, card: dict[str, Any]) -> None:
        tid = card["task_id"]
        nid = ids.card_note_id(tid)
        if card.get("folded_into"):
            self._forget(tid, nid)
            return
        section = render.section_of(card)
        column = ids.COLUMN_IDS[section]
        labels = render.card_labels(card)
        note = self.fence.note(nid)
        title = render.text(card.get("title")) or tid
        if note is None:
            self.fence.create(column, title, "text", self._initial_body(card), note_id=nid,
                              labels=labels)
        else:
            self.fence.update(nid, title=title, content=self._body(card, nid), labels=labels)
            if note.get("parentNoteIds") != [column]:
                self.fence.move(nid, column)
        self._record(card, nid)
        self._messages(card, nid)
        self._source(card, nid, ended=section == "history" and card.get("section") != "stale")

    @staticmethod
    def _initial_body(card: dict[str, Any]) -> str:
        body = card.get("body")
        if card.get("kind") == "note" and body and render.text(body).strip() not in ("null", "None"):
            return render.text(body)
        return render.card_body(card)

    def _body(self, card: dict[str, Any], nid: str) -> str | None:
        """The body to write, or None to leave Tony's own text alone."""
        if card.get("kind") != "note":
            return render.card_body(card)
        if render.text(self.fence.content(nid)).strip() in ("", "null", "None"):
            return self._initial_body(card)
        return None

    def _record(self, card: dict[str, Any], nid: str) -> None:
        rid = ids.record_note_id(card["task_id"])
        record = render.card_record(card)
        if self.fence.note(rid) is None:
            self.fence.create(nid, "record", "code", record, note_id=rid, mime="application/json")
        else:
            self.fence.update(rid, content=record)

    def _messages(self, card: dict[str, Any], nid: str) -> None:
        """The card's messages as one Markdown child, when the feed carries them."""
        messages = card.get("messages") or []
        if not messages:
            return
        text = "\n\n".join(
            f"**{render.text(m.get('author')) or 'unknown'}** on {render.text(m.get('node')) or '?'}, "
            f"{render.iso(m.get('ts'))} (#{render.text(m.get('seq'))})\n\n{render.text(m.get('body'))}"
            for m in messages)
        mid = ids.messages_note_id(card["task_id"])
        if self.fence.note(mid) is None:
            self.fence.create(nid, "messages", "code", text, note_id=mid, mime=ids.MARKDOWN)
        else:
            self.fence.update(mid, content=text)

    def _source(self, card: dict[str, Any], nid: str, *, ended: bool) -> None:
        src = card.get("source_note")
        if not src:
            return
        tid = card["task_id"]
        try:
            if ended:
                self.fence.drop_marker(tid)
                return
            if self.fence.clone_under(src, nid):
                self.fence.add_marker(src, tid, render.marker_title(card),
                                      render.marker_html(card), relation_to=nid)
        except Refused:
            log.exception("task %s: the source note %s was not linked", tid, src)

    def _forget(self, tid: str, nid: str) -> None:
        """A card folded into another by task_join: its note, its marker and its clone go."""
        self.fence.delete(nid)
        try:
            self.fence.drop_marker(tid)
        except Refused:
            log.exception("task %s: marker not removed", tid)

    # -- the push --------------------------------------------------------------------

    def apply(self, base: int | None, cards: list[dict[str, Any]], mark: int | None) -> dict[str, Any]:
        """Apply one page that follows mark ``base``. A page that follows any other mark changes
        nothing, and the answer's ``mark`` says where the next page starts. No ``base`` only asks."""
        current = self.mark()
        if base is None or int(base) != current:
            return {"mark": current, "applied": 0}
        applied = 0
        for card in cards:
            try:
                self.upsert(card)
                applied += 1
            except Exception:  # noqa: BLE001 - one card never stops the rest
                log.exception("card %s was not written", card.get("task_id"))
        target = int(mark if mark is not None else current)
        if target > current:
            self.set_mark(target)
        return {"mark": max(target, current), "applied": applied}
