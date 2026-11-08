"""The board writer: mirror the store's cards into Trilium on deneb (queue.md R61, R66, R67; fleet.md R106).

It reads ``events`` after its mark and upserts one note per changed card, through the
``trilium`` tool. A card sits in the folder of its section under ``tasks`` and moves when the
section changes. The board is write-only from here: the writer never reads a card's section
or tags back. It rewrites a card's body on each update, except a Proposed note's body, which
is Tony's and which the writer writes only when it creates the note. It never makes a Ledger
or Older note.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from awm.operations.board import (BOOK_ID, COLUMN_IDS, OPS_ID, ROOT_ID, card_note_id,
                                  ensure_ops, ensure_root, ensure_tasks, messages_note_id,
                                  record_note_id)
from awm.operations.store import Store
from awm.operations.trilium import TriliumTool

log = logging.getLogger(__name__)

HEARTBEAT_S = 60
MARK = "writer_mark"
POLL_S = 2.0
BODY_MAX = 100_000
MARKDOWN = "text/x-markdown"
MANAGED = {"agentManaged": "", "readOnly": ""}


class Writer:
    def __init__(self, store: Store, api: TriliumTool, *, parent: str = BOOK_ID,
                 portals: dict[str, str] | None = None, clock=time.time) -> None:
        self.store, self.api, self.parent, self.clock = store, api, parent, clock
        self.portals = dict(portals or {})
        self._root_ok = False
        self._beat = 0.0

    # -- the body check the store runs on spawn ------------------------------------

    def read_body(self, note_id: str) -> str:
        text = self.api.content(note_id)
        if len(text) > BODY_MAX:
            raise ValueError(f"the note body is over {BODY_MAX} characters")
        return text

    def _ensure_root(self) -> None:
        if not self._root_ok:
            ensure_root(self.api, self.parent)
            ensure_tasks(self.api, self.parent)
            ensure_ops(self.api, self.portals, self.parent)
            self._root_ok = True

    # -- the Ops book ----------------------------------------------------------------

    def post_ops(self, kind: str, node: str, title: str, body: str) -> str:
        """Rewrite the one managed note for this kind and node in the Ops book."""
        self._ensure_root()
        ops, name = OPS_ID, f"{kind} - {node}"
        stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        content = f"Changed: {stamp}\n\n## {title}\n\n{body}\n"
        for kid in self.api.children(ops):
            if kid.get("title") == name and "agentManaged" in (kid.get("labels") or []):
                self.api.update(kid["note_id"], content=content, labels={"changed": stamp})
                return kid["note_id"]
        return self.api.create_note(ops, name, "code", content, mime=MARKDOWN,
                                    labels={**MANAGED, "changed": stamp})

    # -- one card ----------------------------------------------------------------------

    @staticmethod
    def _tonys(card: dict[str, Any]) -> bool:
        """Whether the note's body is Tony's: a Proposed note, and the task spawned from it."""
        return card["kind"] == "note" or card["body_ref"] == card_note_id(card["task_id"])

    @classmethod
    def _labels(cls, card: dict[str, Any]) -> dict[str, str | None]:
        values = {"swarmCard": "", "readOnly": None if cls._tonys(card) else "",
                  "taskId": card["task_id"], "section": card["section"],
                  "status": card["status"], "peer": card["peer"], "cardKind": card["kind"],
                  "updatedAt": int(card["updated"]),
                  "doneAt": int(card["done_at"]) if card["done_at"] else None}
        return {k: None if v is None else str(v) for k, v in values.items()}

    @staticmethod
    def _body(card: dict[str, Any]) -> str:
        """The card's current state as HTML, so the note reads fresh in the vault."""
        e = html.escape
        meta = card["meta"]
        head = " · ".join(x for x in (card["section"].replace("_", " "), card["status"],
                                      card["peer"]) if x)
        parts = [f"<h3>{e(head)}</h3>",
                 f"<p>Task {e(card['task_id'])}, updated "
                 f"{datetime.fromtimestamp(card['updated'], timezone.utc).isoformat(timespec='seconds')}"
                 "</p>"]
        if meta.get("reason"):
            parts.append(f"<h4>Reason</h4><p>{e(str(meta['reason']))}</p>")
        todos = meta.get("todos") or []
        if todos:
            rows = "".join(f"<li>[{'x' if t.get('status') == 'completed' else ' '}] "
                           f"{e(str(t.get('subject') or t.get('content') or ''))}</li>"
                           for t in todos)
            parts.append(f"<h4>Todos</h4><ul>{rows}</ul>")
        sessions = card.get("sessions") or []
        if sessions:
            rows = "".join(f"<li>{e(str(s.get('name') or s.get('session_id')))} "
                           f"({e(str(s.get('role') or ''))}, "
                           f"{'active' if s.get('active') else 'ended'})</li>" for s in sessions)
            parts.append(f"<h4>Sessions</h4><ul>{rows}</ul>")
        return "".join(parts)

    def _column(self, card: dict[str, Any]) -> str:
        return COLUMN_IDS.get(card["section"], COLUMN_IDS["working"])

    def upsert(self, card: dict[str, Any]) -> None:
        tid, nid = card["task_id"], card_note_id(card["task_id"])
        want, column = self._labels(card), self._column(card)
        labels = {k: v for k, v in want.items() if v is not None}
        body = None if self._tonys(card) else self._body(card)
        note = self.api.note(nid)
        if note is None:
            if card["kind"] == "note":
                self.api.create_note(column, card["title"], "code", note_id=nid,
                                     mime="text/x-markdown", labels=labels)
            else:
                self.api.create_note(column, card["title"], "text", body or "", note_id=nid,
                                     labels=labels)
        else:
            self.api.update(nid, title=card["title"] if note.get("title") != card["title"] else None,
                            content=body, labels=labels)
            if note.get("parentNoteIds") != [column]:
                self.api.move(nid, column)
            have = {a["name"] for a in note.get("attributes", [])
                    if a.get("type") == "label" and a.get("noteId", nid) == nid}
            for name in sorted(k for k, v in want.items() if v is None and k in have):
                self.api.label_delete(nid, name)
        record = json.dumps({"task_id": tid, "sessions": card.get("sessions", []),
                             "parent_task": card["parent_task"], "tags": card["tags"],
                             "reason": card["meta"].get("reason"),
                             "plan": card["meta"].get("plan"),
                             "todos": card["meta"].get("todos")}, sort_keys=True)
        rid = record_note_id(tid)
        if self.api.note(rid) is None:
            self.api.create_note(nid, "record", "code", record, note_id=rid,
                                 mime="application/json")
        else:
            self.api.update(rid, content=record)
        self._messages(tid, nid)
        if card["kind"] == "note" and card["body_ref"] != nid:
            self.store.card_action("set_body_ref", tid, {"body_ref": nid})

    def _messages(self, tid: str, nid: str) -> None:
        """The card's messages as one Markdown child, oldest first."""
        messages = self.store.task_messages(tid)
        if not messages:
            return
        text = "\n\n".join(
            f"**{m['author'] or 'unknown'}** on {m['node'] or '?'}, "
            f"{datetime.fromtimestamp(m['ts'], timezone.utc).isoformat(timespec='seconds')}"
            f" (#{m['seq']})\n\n{m['body']}" for m in messages)
        mid = messages_note_id(tid)
        if self.api.note(mid) is None:
            self.api.create_note(nid, "messages", "code", text, note_id=mid, mime=MARKDOWN)
        else:
            self.api.update(mid, content=text)

    # -- the loop ------------------------------------------------------------------------

    def sync_once(self, limit: int = 200) -> int:
        """Apply the events after the mark. Returns how many it applied."""
        self._ensure_root()
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
