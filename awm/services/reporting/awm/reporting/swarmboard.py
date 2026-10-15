"""The Swarm board in Trilium, through the local gateway's ``trilium`` domain.

The ledger is the source of truth and the board only displays it. A card is a
note under a column book note. A move adds the new column branch and removes
only the old one, so a clone on Tony's own board survives. A card the service
made is a text note labelled ``#agentManaged`` and ``#readOnly`` whose body is
the record. A card claimed from Tony's own note keeps its body as his and gets
the record in a ``Record`` child; the ledger notes are Markdown children.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from awm import gatewayclient

from awm.reporting.ledger import COLUMNS

log = logging.getLogger("awm.reporting.swarmboard")

BOARD_LABEL = "swarmBoard"
OPS_LABEL = "swarmOps"
LEDGER = "Ledger"
MARKDOWN = "text/x-markdown"
HTML = "text/html"
RECORD = "Record"
CHANGED = "changed"
MANAGED = {"agentManaged": "", "readOnly": ""}
Call = Callable[..., Awaitable[dict]]


class BoardError(RuntimeError):
    """Trilium could not be reached, refused a call, or the board is not there."""


@dataclass(frozen=True)
class Layout:
    board: str
    ledger: str
    columns: dict[str, str]


def local_stamp(utc: str | None = None) -> str:
    """Now, or the Trilium UTC time ``utc``, as ISO 8601 in this node's zone with its UTC offset."""
    when = datetime.now(timezone.utc)
    for form in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            when = datetime.strptime((utc or "").strip().replace("Z", "+0000"), form)
            break
        except ValueError:
            continue
    return when.astimezone().isoformat(timespec="seconds")


def ops_body(stamp: str, title: str | None, body: str) -> str:
    """An Ops notice: the change time on top, then the notice. ``title`` None keeps ``body`` as it is."""
    if title is not None:
        return f"Changed: {stamp}\n\n## {title}\n\n{body}\n"
    lines = body.splitlines()
    if lines and lines[0].startswith("Changed: "):
        lines = lines[1:]
    return f"Changed: {stamp}\n\n" + "\n".join(lines).lstrip("\n") + "\n"


async def _gateway(fn: str, **args: Any) -> dict:
    return await gatewayclient.call("trilium", fn, args, timeout=60.0)


class SwarmBoard:
    def __init__(self, call: Call | None = None) -> None:
        self._raw = call or _gateway
        self._layout: Layout | None = None
        self._awm: str | None = None

    async def _call(self, fn: str, **args: Any) -> dict:
        try:
            out = await self._raw(fn, **args)
        except Exception as exc:  # noqa: BLE001 — every failure is the same to a caller
            self._layout = None
            raise BoardError(f"trilium {fn} failed: {exc}") from exc
        return out if isinstance(out, dict) else {}

    async def _children(self, note_id: str) -> list[dict]:
        return (await self._call("note_children", note_id=note_id)).get("children") or []

    async def _child(self, parent: str, title: str, **create: Any) -> str:
        for kid in await self._children(parent):
            if kid.get("title") == title:
                return kid["note_id"]
        made = await self._call("note_create", title=title, parent=parent, **create)
        return made["note_id"]

    async def layout(self) -> Layout:
        """The board, its columns and its Ledger note. Refuses when the board is not made."""
        if self._layout is not None:
            return self._layout
        hits = (await self._call("note_search", query=f"#{BOARD_LABEL}", limit=5)).get("results") or []
        if len(hits) != 1:
            raise BoardError(f"{len(hits)} notes carry #{BOARD_LABEL}; run operations board_ensure")
        board = hits[0]["noteId"]
        kids = {k["title"]: k["note_id"] for k in await self._children(board)}
        missing = [t for t in (*COLUMNS.values(), LEDGER) if t not in kids]
        if missing:
            raise BoardError(f"the Swarm board lacks {', '.join(missing)}; run operations board_ensure")
        self._layout = Layout(board, kids[LEDGER], {t: kids[t] for t in COLUMNS.values()})
        return self._layout

    async def ensure(self, parent: str | None = None) -> dict:
        """Make the board, its columns and its Ledger note if they are missing."""
        hits = (await self._call("note_search", query=f"#{BOARD_LABEL}", limit=5)).get("results") or []
        if len(hits) > 1:
            raise BoardError(f"{len(hits)} notes carry #{BOARD_LABEL}")
        if hits:
            board = hits[0]["noteId"]
        else:
            board = (await self._call(
                "note_create", title="Swarm board", parent=parent or await self._awm_note(),
                type="book", labels={BOARD_LABEL: "", "board:columnsFrom": "folders"},
                relations={"template": "_template_board"}))["note_id"]
        for title in COLUMNS.values():
            await self._child(board, title, type="book")
        await self._child(board, LEDGER, type="book")
        ops = await self._find_ops()
        if ops is None:
            ops = await self._child(parent or await self._awm_note(), "Ops", type="book")
            await self._call("note_update", note_id=ops, labels={OPS_LABEL: ""})
        self._layout = None
        lay = await self.layout()
        return {"board": lay.board, "ledger": lay.ledger, "columns": lay.columns, "ops": ops,
                "created": not hits}

    async def _find_ops(self) -> str | None:
        """The one Ops book, found by label so that two nodes never each make their own."""
        hits = (await self._call("note_search", query=f"#{OPS_LABEL}", limit=5)).get("results") or []
        if len(hits) > 1:
            raise BoardError(f"{len(hits)} notes carry #{OPS_LABEL}")
        return hits[0]["noteId"] if hits else None

    async def _awm_note(self) -> str:
        if self._awm:
            return self._awm
        hits = (await self._call("note_search", query="note.title = AWM", limit=10)).get("results") or []
        roots = [h["noteId"] for h in hits if "root" in (h.get("parentNoteIds") or [])]
        if len(roots) != 1:
            raise BoardError(f"{len(roots)} AWM notes sit under the root; pass parent")
        self._awm = roots[0]
        return self._awm

    async def move(self, note_id: str, column: str | None = None, *, into: str | None = None) -> bool:
        """Show the card under ``column`` (or the note ``into``) and under no other swarm column.

        True if it moved. Other parents, such as a clone on Tony's board, stay.
        """
        lay = await self.layout()
        if note_id in {lay.board, lay.ledger, *lay.columns.values()}:
            raise BoardError(f"{note_id} is part of the board, not a card")
        note = await self._call("note_get", note_id=note_id, content=False)
        parents = set(note.get("parentNoteIds") or [])
        target = into or lay.columns[column]
        stale = (parents & set(lay.columns.values())) - {target}
        if target in parents and not stale:
            return False
        await self._call("note_place", note_id=note_id, parents=[target], remove=sorted(stale))
        return True

    async def create_card(self, title: str, column: str) -> str:
        """A managed text note in ``column``, for a task that has no card yet."""
        lay = await self.layout()
        made = await self._call("note_create", title=title, parent=lay.columns[column], labels=MANAGED)
        return made["note_id"]

    async def snapshot(self) -> dict[str, str]:
        """Every card on the board and the column it is in. Any failed read raises."""
        lay = await self.layout()
        return {kid["note_id"]: name for name, col in lay.columns.items()
                for kid in await self._children(col)}

    async def upsert_markdown(self, parent: str, title: str, content: str,
                              labels: dict[str, str] | None = None) -> str:
        """The child of ``parent`` called ``title``, made or rewritten as a managed Markdown note."""
        for kid in await self._children(parent):
            if kid.get("title") == title and "agentManaged" in (kid.get("labels") or []):
                await self._call("note_update", note_id=kid["note_id"], content=content,
                                 **({"labels": labels} if labels else {}))
                return kid["note_id"]
        made = await self._call("note_create", title=title, content=content, parent=parent,
                                type="code", mime=MARKDOWN, labels={**MANAGED, **(labels or {})})
        return made["note_id"]

    async def write_card(self, card: str, html: str, title: str | None = None) -> None:
        """Rewrite the body, and the title when given, of a card the service made."""
        fields: dict[str, Any] = {"content": html, "labels": MANAGED}
        if title:
            fields["title"] = title
        await self._call("note_update", note_id=card, **fields)

    async def write_record(self, card: str, html: str) -> str:
        """The managed HTML ``Record`` child of a claimed card, made or rewritten."""
        for kid in await self._records(card):
            await self._call("note_update", note_id=kid["note_id"], content=html, type="text", mime=HTML,
                             labels=MANAGED)
            return kid["note_id"]
        made = await self._call("note_create", title=RECORD, content=html, parent=card, type="text",
                                labels=MANAGED)
        return made["note_id"]

    async def _records(self, card: str) -> list[dict]:
        return [kid for kid in await self._children(card)
                if kid.get("title") == RECORD and "agentManaged" in (kid.get("labels") or [])]

    async def drop_records(self, card: str, *, dry_run: bool = False) -> list[str]:
        """Delete the managed ``Record`` children of a card the service made. Returns their ids."""
        ids = [kid["note_id"] for kid in await self._records(card)]
        if not dry_run:
            for note_id in ids:
                await self._call("note_delete", note_id=note_id)
        return ids

    async def write_ledger(self, node: str, content: str) -> str:
        return await self.upsert_markdown((await self.layout()).ledger, node, content)

    async def title_of(self, note_id: str) -> str:
        return (await self._call("note_get", note_id=note_id, content=False)).get("title") or ""

    async def post_ops(self, kind: str, node: str, title: str, body: str) -> str:
        """Upsert the one managed note for this kind and node in the private Ops book under AWM."""
        ops = await self._ops()
        stamp = local_stamp()
        return await self.upsert_markdown(ops, f"{kind} - {node}", ops_body(stamp, title, body),
                                          labels={CHANGED: stamp})

    async def _ops(self) -> str:
        ops = await self._find_ops()
        if ops is None:
            raise BoardError(f"no note carries #{OPS_LABEL}; run operations board_ensure")
        return ops

    async def stamp_ops(self, *, dry_run: bool = False) -> list[dict]:
        """Give each Ops notice with no change stamp one from its Trilium modified time."""
        out = []
        for kid in await self._children(await self._ops()):
            if CHANGED in (kid.get("labels") or []):
                continue
            note = await self._call("note_get", note_id=kid["note_id"])
            stamp = local_stamp(note.get("utcDateModified"))
            out.append({"note_id": kid["note_id"], "title": kid.get("title"), "changed": stamp})
            if not dry_run:
                await self._call("note_update", note_id=kid["note_id"], labels={CHANGED: stamp},
                                 content=ops_body(stamp, None, note.get("content") or ""))
        return out
