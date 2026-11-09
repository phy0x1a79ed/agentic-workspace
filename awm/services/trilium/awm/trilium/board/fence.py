"""The board writer's only door into the vault (fleet.md R106 item 2, queue.md R66).

Reads go anywhere. A write must land inside the writer's roots (``AWM_TRILIUM_WRITER_ROOTS``,
default tasks, Ops and the board page), with two exceptions that the per-note bot button needs:
a labelled marker note under any source note, and a clone of a source note under a card.
Removing a clone removes its branch and never the note.
"""

from __future__ import annotations

import os
from typing import Any

from awm.trilium import etapi
from awm.trilium.board import ids

ROOTS_ENV = "AWM_TRILIUM_WRITER_ROOTS"
MARKER_LABEL = "awmMarker"
#: How many notes one ancestry walk reads before it gives up and refuses.
_WALK_MAX = 256


class Refused(PermissionError):
    pass


def configured_roots() -> frozenset[str]:
    raw = os.environ.get(ROOTS_ENV, ",".join(ids.ROOTS))
    return frozenset(r.strip() for r in raw.split(",") if r.strip() and r.strip() != "root")


def _missing(exc: Exception) -> bool:
    return "-> 404" in str(exc)


class Fence:
    def __init__(self, client: etapi.Etapi | None = None,
                 roots: frozenset[str] | None = None) -> None:
        self.c = client or etapi.client()
        self.roots = configured_roots() if roots is None else roots
        if not self.roots:
            raise Refused("this vault names no writer subtree")

    # -- reads, anywhere ------------------------------------------------------------

    def note(self, note_id: str) -> dict[str, Any] | None:
        """The note with its attributes and no body, or None when the vault has none."""
        try:
            return self.c.note(note_id)
        except etapi.EtapiError as exc:
            if _missing(exc):
                return None
            raise

    def content(self, note_id: str) -> str:
        return self.c.note_content(note_id)

    def children(self, note_id: str) -> list[dict[str, Any]]:
        return self.c.children(note_id)

    def search(self, query: str, **params: Any) -> list[dict[str, Any]]:
        return list(self.c.search(query, **params).get("results") or [])

    # -- the fence --------------------------------------------------------------------

    def inside(self, note_id: str, parents: list[str] | None = None) -> bool:
        """Whether any branch of ``note_id`` leads up to one of the roots."""
        if note_id in self.roots:
            return True
        frontier = list(parents if parents is not None
                        else (self.note(note_id) or {}).get("parentNoteIds") or [])
        seen = {note_id}
        while frontier and len(seen) < _WALK_MAX:
            n = frontier.pop()
            if n in self.roots:
                return True
            if n in seen or n in ("root", "none", ""):
                continue
            seen.add(n)
            frontier.extend((self.note(n) or {}).get("parentNoteIds") or [])
        return False

    def _require(self, verb: str, *note_ids: str) -> None:
        for n in note_ids:
            if not self.inside(n):
                raise Refused(f"{verb} on {n} refused: the board writer acts only under "
                              f"{', '.join(sorted(self.roots))}")

    # -- writes inside the roots --------------------------------------------------------

    def create(self, parent: str, title: str, type_: str = "code", content: str = "", *,
               note_id: str | None = None, mime: str | None = None,
               labels: dict[str, str | None] | None = None) -> str:
        """Create a note. A root may be made wherever it goes; anything else needs a parent
        inside the roots."""
        if note_id not in self.roots:
            self._require("create", parent)
        made = self.c.create_note(parent_note_id=parent, title=title, type=type_,
                                  content=content, mime=mime, note_id=note_id)
        nid = made["note"]["noteId"]
        self._labels(nid, labels, None)
        return nid

    def update(self, note_id: str, *, title: str | None = None, type_: str | None = None,
               content: str | None = None, mime: str | None = None,
               labels: dict[str, str | None] | None = None) -> bool:
        """Change what differs. Returns whether anything did, so a replay writes no revision."""
        self._require("update", note_id)
        note = self.c.note(note_id)
        changed = False
        fields = {k: v for k, v in (("title", title), ("type", type_), ("mime", mime))
                  if v is not None and note.get(k) != v}
        if fields:
            self.c.patch_note(note_id, **fields)
            changed = True
        if content is not None and self.c.note_content(note_id) != content:
            self.c.set_content(note_id, content)
            changed = True
        return self._labels(note_id, labels, note.get("attributes") or []) or changed

    def _labels(self, note_id: str, labels: dict[str, str | None] | None,
                attrs: list[dict] | None) -> bool:
        if not labels:
            return False
        want = {k: v for k, v in labels.items() if v is not None}
        done = self.c.set_attributes(note_id=note_id, values=want, attributes=attrs)
        gone = [k for k, v in labels.items() if v is None]
        for name in gone:
            done += [name] * self.c.clear_attribute(note_id=note_id, name=name)
        return bool(done)

    def relation(self, note_id: str, name: str, target: str) -> None:
        self._require("relation", note_id)
        self.c.set_attribute(note_id=note_id, name=name, value=target, type="relation")

    def move(self, note_id: str, parent: str) -> None:
        """Put the note under this parent and nowhere else. A root stays put."""
        if note_id in self.roots:
            raise Refused(f"move on {note_id} refused: a writer root stays put")
        self._require("move", note_id, parent)
        self.c.move_note(note_id, parent)

    def nest_root(self, note_id: str, parent: str) -> None:
        """Put one root under another. Nothing else moves a root."""
        if note_id not in self.roots or parent not in self.roots:
            raise Refused(f"nest_root of {note_id} under {parent} refused: both must be roots")
        if (self.note(note_id) or {}).get("parentNoteIds") != [parent]:
            self.c.move_note(note_id, parent)

    def delete(self, note_id: str) -> None:
        if note_id in self.roots:
            raise Refused(f"delete on {note_id} refused: a writer root is never deleted")
        self._require("delete", note_id)
        try:
            self.c.delete_note(note_id)
        except etapi.EtapiError as exc:
            if not _missing(exc):
                raise

    # -- the two acts outside the roots (fleet.md R106 item 2) ----------------------------------

    def add_marker(self, source: str, task_id: str, title: str, content: str, *,
                   relation_to: str) -> str:
        """Our own labelled marker note under any note. One per task, found again by its id."""
        mid = ids.marker_note_id(task_id)
        if self.note(mid) is None:
            self.c.create_note(parent_note_id=source, title=title, type="text",
                               content=content, note_id=mid)
        else:
            self.c.put_branch(mid, source)
        self.c.set_attributes(note_id=mid, values={MARKER_LABEL: task_id, "readOnly": ""})
        self.c.set_attribute(note_id=mid, name="awmTask", value=relation_to, type="relation")
        existing = self.c.note(mid)
        if existing.get("title") != title:
            self.c.patch_note(mid, title=title)
        if self.c.note_content(mid) != content:
            self.c.set_content(mid, content)
        return mid

    def drop_marker(self, task_id: str) -> bool:
        """Delete the marker note, but only a note that carries our label."""
        mid = ids.marker_note_id(task_id)
        note = self.note(mid)
        if note is None:
            return False
        labels = {a["name"]: a.get("value") for a in note.get("attributes") or []
                  if a.get("type") == "label"}
        if labels.get(MARKER_LABEL) != task_id:
            raise Refused(f"delete on {mid} refused: the note carries no {MARKER_LABEL} label")
        self.c.delete_note(mid)
        return True

    def clone_under(self, source: str, card: str) -> bool:
        """Show the source note under the card too. The source must sit outside the roots."""
        self._require("clone", card)
        if self.inside(source):
            raise Refused(f"clone of {source} refused: it sits inside the writer's roots")
        if self.note(source) is None:
            return False
        self.c.put_branch(source, card)
        return True

    def unclone(self, source: str, card: str) -> bool:
        """Remove the card's branch to the source and nothing else. If that branch is the
        note's only one, the note stays: Trilium would delete it with its last branch."""
        self._require("unclone", card)
        note = self.note(source)
        if note is None:
            return False
        parents = note.get("parentNoteIds") or []
        if card not in parents:
            return False
        if len(parents) < 2:
            raise Refused(f"unclone of {source} refused: the card holds its only branch")
        self.c.delete_branch(self.c.branch_id(source, card))
        return True
