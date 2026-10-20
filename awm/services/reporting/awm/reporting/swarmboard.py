"""The Swarm board in Trilium, through the local gateway's ``trilium`` domain.

The ledger is the source of truth and the board only displays it. A card is a
note under a column book note. A move adds the new column branch and removes
only the old one, so a clone on Tony's own board survives. A card the service
made is a text note labelled ``#agentManaged`` and ``#readOnly`` whose body is
the record. A card claimed from Tony's own note keeps its body as his and gets
the record in a ``Record`` child; the ledger notes are Markdown children.

A card carries its scope's tags in ``swarmTags``, comma-joined, and the board's
"By tag" book holds one saved search per tag, matching the whole tag only.

Done and Rejected list their cards newest first, by the ``cardStateAt`` label
each card gets on entering a column. Each ends in one "Older (n)" book, labelled
``#swarmOlder``, that holds the cards finished more than two weeks ago.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from awm import gatewayclient

from awm.reporting.ledger import COLUMNS, DONE, REJECTED

log = logging.getLogger("awm.reporting.swarmboard")

BOARD_LABEL = "swarmBoard"
OPS_LABEL = "swarmOps"
LEDGER = "Ledger"
MARKDOWN = "text/x-markdown"
HTML = "text/html"
RECORD = "Record"
CHANGED = "changed"
MANAGED = {"agentManaged": "", "readOnly": ""}
ARCHIVED = "archived"
OLDER = "Older"
OLDER_LABEL = "swarmOlder"
STATE_AT = "cardStateAt"
#: Below every ISO time, so the Older book sorts after every card in its column.
OLDER_AT = "0000"
SORTED = {"sorted": STATE_AT, "sortDirection": "desc"}
TAGS_LABEL = "swarmTags"
BY_TAG = "By tag"
TAG_SEARCH_LABEL = "swarmTagSearch"
#: The labels a card carries, offered as promoted chips on the board root.
CHIPS = {"cardFrom": "From", "cardTo": "To", "project": "Project", "owner": "Owner",
         TAGS_LABEL: "Tags"}
#: Columns a board made before they existed lacks. The layout makes them on first use.
LATE_COLUMNS = (COLUMNS[DONE], COLUMNS[REJECTED])
#: The columns that sort newest first and fold their old cards into an Older book.
FOLDING = (COLUMNS[DONE], COLUMNS[REJECTED])
Call = Callable[..., Awaitable[dict]]


def tag_search(tag: str) -> str:
    """A Trilium search for the cards whose ``swarmTags`` hold ``tag`` whole, so "lab" misses "collab"."""
    return f"#{TAGS_LABEL} %= '(^|,){tag.replace('.', '[.]')}(,|$)'"


class BoardError(RuntimeError):
    """Trilium could not be reached, refused a call, or the board is not there."""


@dataclass(frozen=True)
class Layout:
    board: str
    ledger: str
    columns: dict[str, str]
    #: Column title -> its Older book, for the columns that have one yet.
    older: dict[str, str] = field(default_factory=dict)

    def parts(self) -> set[str]:
        return {self.board, self.ledger, *self.columns.values(), *self.older.values()}

    def homes(self) -> set[str]:
        """Every note a card can sit in: the columns and the Older books."""
        return {*self.columns.values(), *self.older.values()}


def state_stamp() -> str:
    """Now as a ``cardStateAt`` value, in the federation board's form so both boards sort alike."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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


def _iso(utc: str | None) -> str:
    """A Trilium UTC time as a ``cardStateAt`` value. A time it cannot read is now."""
    try:
        when = datetime.strptime((utc or "").strip().replace("Z", "+0000"), "%Y-%m-%d %H:%M:%S.%f%z")
    except ValueError:
        return state_stamp()
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _own_labels(note: dict, note_id: str) -> set[str]:
    return {a.get("name") for a in note.get("attributes") or []
            if a.get("type", "label") == "label" and a.get("noteId") in (None, note_id)}


async def _gateway(fn: str, **args: Any) -> dict:
    return await gatewayclient.call("trilium", fn, args, timeout=60.0)


class SwarmBoard:
    def __init__(self, call: Call | None = None) -> None:
        self._raw = call or _gateway
        self._layout: Layout | None = None
        self._awm: str | None = None
        self._chips = False
        self._tag_searches: set[str] = set()

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
        for title in [t for t in missing if t in LATE_COLUMNS]:
            kids[title] = (await self._call("note_create", title=title, parent=board, type="book"))["note_id"]
            missing.remove(title)
        if missing:
            raise BoardError(f"the Swarm board lacks {', '.join(missing)}; run operations board_ensure")
        columns = {t: kids[t] for t in COLUMNS.values()}
        older = {}
        for title in FOLDING:
            found = [k["note_id"] for k in await self._children(columns[title])
                     if OLDER_LABEL in (k.get("labels") or [])]
            if found:
                older[title] = found[0]
        layout = Layout(board, kids[LEDGER], columns, older)
        # Until board_ensure makes the Older books, every read looks for them again.
        self._layout = layout if len(older) == len(FOLDING) else None
        if not self._chips:
            cached = self._layout
            await self._define_chips(board)
            self._layout = cached
        return layout

    async def _define_chips(self, board: str) -> None:
        """Offer From, To, Project, Owner and Tags as promoted chips on every card under the board.

        A failure here only costs the chips, so it is logged and tried again on
        the next layout read.
        """
        try:
            note = await self._call("note_get", note_id=board, content=False)
            held = {a.get("name") for a in note.get("attributes") or []
                    if a.get("noteId") in (None, board)}
            for label, caption in CHIPS.items():
                if f"label:{label}" not in held:
                    await self._call("attr_set", note_id=board, name=f"label:{label}",
                                     value=f"promoted,single,text,alias={caption}",
                                     inheritable=True)
        except BoardError as exc:
            log.warning("reporting: could not define the card chips: %s", exc)
            return
        self._chips = True

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
        await self._child(board, BY_TAG, type="book")
        self._chips = False
        self._layout = None
        folded = await self._ensure_older(await self.layout())
        ops = await self._find_ops()
        if ops is None:
            ops = await self._child(parent or await self._awm_note(), "Ops", type="book")
            await self._call("note_update", note_id=ops, labels={OPS_LABEL: ""})
        self._layout = None
        lay = await self.layout()
        return {"board": lay.board, "ledger": lay.ledger, "columns": lay.columns, "older": lay.older,
                "ops": ops, "created": not hits, **folded}

    async def _ensure_older(self, lay: Layout) -> dict:
        """Sort Done and Rejected, give each its Older book, and convert the cards labelled ``#archived``.

        An archived card moves into its column's Older book and loses the label.
        A card with no ``cardStateAt`` gets one from its Trilium modified time.
        """
        folded = stamped = 0
        for title in FOLDING:
            column = lay.columns[title]
            await self._call("note_update", note_id=column, labels=SORTED)
            older = lay.older.get(title) or (await self._call(
                "note_create", title=OLDER, parent=column, type="book",
                labels={OLDER_LABEL: "", STATE_AT: OLDER_AT, **SORTED}))["note_id"]
            for kid in await self._children(column):
                labels = kid.get("labels") or []
                if kid["note_id"] == older or OLDER_LABEL in labels:
                    continue
                if STATE_AT not in labels:
                    note = await self._call("note_get", note_id=kid["note_id"], content=False)
                    await self._call("note_update", note_id=kid["note_id"],
                                     labels={STATE_AT: _iso(note.get("utcDateModified"))})
                    stamped += 1
                if ARCHIVED in labels:
                    await self._call("note_place", note_id=kid["note_id"], parents=[older], remove=[column])
                    await self._call("attr_delete", note_id=kid["note_id"], name=ARCHIVED)
                    folded += 1
        return {"folded": folded, "stamped": stamped}

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

    async def move(self, note_id: str, column: str | None = None, *, into: str | None = None,
                   stamp: Literal["move", "fill"] | None = None) -> bool:
        """Show the card under ``column`` (or the note ``into``) and under no other swarm column.

        True if it moved. Other parents, such as a clone on Tony's board, stay.
        ``stamp`` "move" sets ``cardStateAt`` to now on a move, and "fill" only on a card that lacks it.
        """
        lay = await self.layout()
        if note_id in lay.parts():
            raise BoardError(f"{note_id} is part of the board, not a card")
        note = await self._call("note_get", note_id=note_id, content=False)
        parents = set(note.get("parentNoteIds") or [])
        target = into or lay.columns[column]
        stale = (parents & lay.homes()) - {target}
        moved = target not in parents or bool(stale)
        if moved:
            await self._call("note_place", note_id=note_id, parents=[target], remove=sorted(stale))
        if stamp and ((moved and stamp == "move") or STATE_AT not in _own_labels(note, note_id)):
            await self.stamp(note_id)
        return moved

    async def stamp(self, note_id: str) -> None:
        """Date the card's entry into the column it is in now."""
        await self._call("note_update", note_id=note_id, labels={STATE_AT: state_stamp()})

    async def create_card(self, title: str, column: str) -> str:
        """A managed text note in ``column``, for a task that has no card yet."""
        lay = await self.layout()
        made = await self._call("note_create", title=title, parent=lay.columns[column], labels=MANAGED)
        return made["note_id"]

    async def carry_labels(self, note_id: str, labels: dict[str, str | None]) -> bool:
        """Set the labels a card should show as chips, where the note lacks them. True if any was written."""
        note = await self._call("note_get", note_id=note_id, content=False)
        have = {a.get("name"): a.get("value") or "" for a in note.get("attributes") or []
                if a.get("type", "label") == "label" and a.get("noteId") in (None, note_id)}
        missing = {k: v for k, v in labels.items() if v and have.get(k) != v}
        if missing:
            await self._call("note_update", note_id=note_id, labels=missing)
        return bool(missing)

    async def fold(self, note_id: str, column: str) -> bool:
        """Move a finished card into its column's Older book. False while the column has none."""
        older = (await self.layout()).older.get(column)
        if older is None:
            return False
        await self.move(note_id, into=older)
        return True

    async def count_older(self) -> None:
        """Title each Older book "Older (n)" by the cards in it."""
        for older in (await self.layout()).older.values():
            kids = await self._children(older)
            title = f"{OLDER} ({len(kids)})"
            if await self.title_of(older) != title:
                await self._call("note_update", note_id=older, title=title)

    async def snapshot(self) -> dict[str, str]:
        """Every card on the board and the column it is in. Any failed read raises.

        A card folded into an Older book is not on the board.
        """
        lay = await self.layout()
        return {kid["note_id"]: name for name, col in lay.columns.items()
                for kid in await self._children(col) if OLDER_LABEL not in (kid.get("labels") or [])}

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

    async def write_card(self, card: str, html: str, title: str | None = None,
                         labels: dict[str, str] | None = None) -> None:
        """Rewrite the body, and the title when given, of a card the service made."""
        fields: dict[str, Any] = {"content": html, "labels": {**MANAGED, **(labels or {})}}
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

    async def label(self, note_id: str, labels: dict[str, str]) -> None:
        await self._call("note_update", note_id=note_id, labels=labels)

    async def ensure_tag_searches(self, tags: set[str]) -> list[str]:
        """Give each tag a saved search in the board's "By tag" book. Returns the tags given one now.

        Every node adds the tags its own cards carry and none removes one, since
        another node's cards may still carry it.
        """
        if tags <= self._tag_searches:
            return []
        lay = await self.layout()
        book = await self._child(lay.board, BY_TAG, type="book")
        have = {kid.get("title") for kid in await self._children(book)}
        made = sorted(tags - have)
        for tag in made:
            await self._call("note_create", title=tag, parent=book, type="search", content="",
                             labels={"searchString": tag_search(tag), TAG_SEARCH_LABEL: tag},
                             relations={"ancestor": lay.board})
        self._tag_searches |= tags | have
        return made

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
        """Give each managed Ops notice with no change stamp one from its Trilium modified time.

        Tony's own notes in the Ops book are left alone.
        """
        out = []
        for kid in await self._children(await self._ops()):
            labels = kid.get("labels") or []
            if CHANGED in labels or "agentManaged" not in labels:
                continue
            note = await self._call("note_get", note_id=kid["note_id"])
            stamp = local_stamp(note.get("utcDateModified"))
            out.append({"note_id": kid["note_id"], "title": kid.get("title"), "changed": stamp})
            if not dry_run:
                await self._call("note_update", note_id=kid["note_id"], labels={CHANGED: stamp},
                                 content=ops_body(stamp, None, note.get("content") or ""))
        return out
