"""The board writer: mirror the store's cards into Trilium on deneb (queue.md R61, R66; fleet.md R106).

It reads ``events`` after its mark and upserts one note per changed card, through the
``trilium`` tool. The board is write-only from here: the writer never reads a card's
section or tags back. It writes a note's body only when it creates the note, because a
Proposed note's body is Tony's. It never makes a Ledger or Older note.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from awm.operations.board import ROOT_ID, card_note_id, ensure_root, record_note_id
from awm.operations.store import Store
from awm.operations.trilium import TriliumTool

log = logging.getLogger(__name__)

HEARTBEAT_S = 60
MARK = "writer_mark"
POLL_S = 2.0
BODY_MAX = 100_000
OPS_LABEL = "swarmOps"
MARKDOWN = "text/x-markdown"
MANAGED = {"agentManaged": "", "readOnly": ""}


class Writer:
    def __init__(self, store: Store, api: TriliumTool, *, parent: str = "root",
                 clock=time.time) -> None:
        self.store, self.api, self.parent, self.clock = store, api, parent, clock
        self._root_ok = False
        self._beat = 0.0

    # -- the body check the store runs on spawn ------------------------------------

    def read_body(self, note_id: str) -> str:
        text = self.api.content(note_id)
        if len(text) > BODY_MAX:
            raise ValueError(f"the note body is over {BODY_MAX} characters")
        return text

    # -- the Ops book ----------------------------------------------------------------

    def post_ops(self, kind: str, node: str, title: str, body: str) -> str:
        """Rewrite the one managed note for this kind and node in the Ops book, such as a login password."""
        hits = self.api.search(f"#{OPS_LABEL}")
        if len(hits) != 1:
            raise ValueError(f"{len(hits)} notes carry #{OPS_LABEL}: the Ops book needs exactly one")
        ops, name = hits[0]["noteId"], f"{kind} - {node}"
        stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        content = f"Changed: {stamp}\n\n## {title}\n\n{body}\n"
        for kid in self.api.children(ops):
            if kid.get("title") == name and "agentManaged" in (kid.get("labels") or []):
                self.api.update(kid["note_id"], content=content, labels={"changed": stamp})
                return kid["note_id"]
        return self.api.create(ops, name, content, mime=MARKDOWN,
                               labels={**MANAGED, "changed": stamp})

    # -- one card ----------------------------------------------------------------------

    @staticmethod
    def _labels(card: dict[str, Any]) -> dict[str, str | None]:
        values = {"swarmCard": "", "taskId": card["task_id"], "section": card["section"],
                  "status": card["status"], "peer": card["peer"], "cardKind": card["kind"],
                  "updatedAt": int(card["updated"]),
                  "doneAt": int(card["done_at"]) if card["done_at"] else None}
        return {k: None if v is None else str(v) for k, v in values.items()}

    def upsert(self, card: dict[str, Any]) -> None:
        tid, nid = card["task_id"], card_note_id(card["task_id"])
        want = self._labels(card)
        labels = {k: v for k, v in want.items() if v is not None}
        note = self.api.note(nid)
        if note is None:
            if card["kind"] == "note":
                self.api.create_note(ROOT_ID, nid, card["title"], "code", "",
                                     mime="text/x-markdown", labels=labels)
            else:
                self.api.create_note(ROOT_ID, nid, card["title"], "text", "", labels=labels)
        else:
            self.api.update(nid, title=card["title"] if note.get("title") != card["title"] else None,
                            labels=labels)
            have = {a["name"] for a in note.get("attributes", [])
                    if a.get("type") == "label" and a.get("noteId", nid) == nid}
            for name in sorted(k for k, v in want.items() if v is None and k in have):
                self.api.label_delete(nid, name)
        record = json.dumps({"task_id": tid, "sessions": card.get("sessions", []),
                             "parent_task": card["parent_task"], "tags": card["tags"],
                             "reason": card["meta"].get("reason")}, sort_keys=True)
        rid = record_note_id(tid)
        if self.api.note(rid) is None:
            self.api.create_note(nid, rid, "record", "code", record, mime="application/json")
        else:
            self.api.update(rid, content=record)
        if card["kind"] == "note" and card["body_ref"] != nid:
            self.store.card_action("set_body_ref", tid, {"body_ref": nid})

    # -- the loop ------------------------------------------------------------------------

    def sync_once(self, limit: int = 200) -> int:
        """Apply the events after the mark. Returns how many it applied."""
        if not self._root_ok:
            ensure_root(self.api, self.parent)
            self._root_ok = True
        after = int(self.store.kv_get(MARK) or 0)
        events = self.store.events_since(after, limit)
        if not events:
            return 0
        seen: list[str] = []
        for ev in events:
            if ev["task_id"] not in seen:
                seen.append(ev["task_id"])
        for tid in seen:
            card = self.store.task_get(tid)
            if card["task_id"] != tid:  # folded into another card by task_join
                self.api.delete_note(card_note_id(tid))
            self.upsert(card)
        self.store.kv_set(MARK, str(events[-1]["id"]))
        return len(events)

    def heartbeat(self) -> None:
        now = self.clock()
        if now - self._beat < HEARTBEAT_S:
            return
        self.api.update(ROOT_ID, labels={"queueHeartbeat": str(int(now))})
        self._beat = now

    def step(self) -> int:
        n = self.sync_once()
        self.heartbeat()
        return n

    async def run_forever(self) -> None:
        delay = POLL_S
        while True:
            try:
                n = await asyncio.to_thread(self.step)
                delay = 0 if n >= 200 else POLL_S
            except Exception:
                log.exception("board writer step failed; retrying")
                delay = min(max(delay * 2, POLL_S), 60)
            await asyncio.sleep(delay)
