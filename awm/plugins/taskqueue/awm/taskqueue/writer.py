"""The board writer: mirror the store's cards into Trilium on deneb (queue.md R61, R66; fleet.md R106).

It reads ``events`` after its mark and upserts one note per changed card. The board is
write-only from here: the writer never reads a card's section or tags back from Trilium.
It writes a note's body only when it creates the note, because a Proposed note's body is
Tony's. It never makes a Ledger or Older note.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

from awm.taskqueue.board import card_note_id, ensure_root, record_note_id, ROOT_ID
from awm.taskqueue.etapi import Etapi
from awm.taskqueue.store import Store

log = logging.getLogger(__name__)

CARD_LABELS = ("swarmCard", "taskId", "section", "status", "peer", "cardKind", "updatedAt", "doneAt")
MANAGED = {("label", n) for n in CARD_LABELS}
MARK = "writer_mark"
HEARTBEAT_S = 60
POLL_S = 2.0
BODY_MAX = 100_000


class Writer:
    def __init__(self, store: Store, api: Etapi, *, parent: str = "root", clock=time.time) -> None:
        self.store, self.api, self.parent, self.clock = store, api, parent, clock
        self._root_ok = False
        self._beat = 0.0

    # -- the body check the store runs on spawn ------------------------------------

    def read_body(self, note_id: str) -> str:
        text = self.api.content(note_id)
        if len(text) > BODY_MAX:
            raise ValueError(f"the note body is over {BODY_MAX} characters")
        return text

    # -- one card ----------------------------------------------------------------------

    def _labels(self, card: dict[str, Any]) -> dict[tuple[str, str], str]:
        values = {"swarmCard": "", "taskId": card["task_id"], "section": card["section"],
                  "status": card["status"], "peer": card["peer"], "cardKind": card["kind"],
                  "updatedAt": int(card["updated"]),
                  "doneAt": int(card["done_at"]) if card["done_at"] else None}
        return {("label", k): str(v) for k, v in values.items() if v is not None}

    def upsert(self, card: dict[str, Any]) -> None:
        tid, nid = card["task_id"], card_note_id(card["task_id"])
        note = self.api.note(nid)
        if note is None:
            if card["kind"] == "note":
                self.api.create_note(ROOT_ID, nid, card["title"], "code", "", mime="text/x-markdown")
            else:
                self.api.create_note(ROOT_ID, nid, card["title"], "text", "")
            note = self.api.note(nid)
        elif note["title"] != card["title"]:
            self.api.patch_note(nid, title=card["title"])
        self.api.sync_attributes(note, self._labels(card), MANAGED)
        record = json.dumps({"task_id": tid, "sessions": card.get("sessions", []),
                             "parent_task": card["parent_task"], "tags": card["tags"]},
                            sort_keys=True)
        rid = record_note_id(tid)
        if self.api.note(rid) is None:
            self.api.create_note(nid, rid, "record", "code", record, mime="application/json")
        else:
            self.api.set_content(rid, record)
        if card["kind"] == "note" and card["body_ref"] != nid:
            self._set_body_ref(tid, nid)

    def _set_body_ref(self, task_id: str, note_id: str) -> None:
        ts = int(self.clock())
        args = {"body_ref": note_id}
        self.store.card_action("set_body_ref", task_id, args, ts,
                               self.store.action_mac("set_body_ref", task_id, args, ts))

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
        root = self.api.note(ROOT_ID) or ensure_root(self.api, self.parent)
        self.api.sync_attributes(root, {("label", "queueHeartbeat"): str(int(now))},
                                 {("label", "queueHeartbeat")})
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


def writer_from_env(store: Store) -> Writer | None:
    """The writer, wired into the store's spawn check. None when no Trilium is configured."""
    url, token = os.environ.get("AWM_TRILIUM_URL"), os.environ.get("AWM_TRILIUM_ETAPI_TOKEN")
    if not url or not token:
        log.warning("AWM_TRILIUM_URL or AWM_TRILIUM_ETAPI_TOKEN is unset: no board writer")
        return None
    writer = Writer(store, Etapi(url, token), parent=os.environ.get("AWM_TRILIUM_BOARD_PARENT", "root"))
    store.body_reader = writer.read_body
    return writer
