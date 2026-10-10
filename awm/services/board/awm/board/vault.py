"""The board's access to Trilium, through the local gateway's `trilium` domain.

The board is a note carrying one marker label, and each card is a direct child
of it. This module finds those notes and moves bytes between them and plain
dicts. It knows the `cardId` label because that is how a card is found, and
nothing else about what a card means.

Trilium's note verbs are operator-only and admit loopback, so the board has to
run on the host that runs Trilium. The transport is a plain callable
``call(fn, args) -> dict`` so a test can put a fake behind it.
"""

from __future__ import annotations

import re
from typing import Any, Callable

Transport = Callable[[str, dict], dict]

DEFAULT_BOARD_LABEL = "federationBoard"

#: A search over the board returns at most this many cards.
SEARCH_LIMIT = 10000

_LABEL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class VaultError(RuntimeError):
    """Trilium could not be reached, refused a call, or held an unusable board."""


class NoBoard(VaultError):
    """No note carries the board label."""


class AmbiguousBoard(VaultError):
    """More than one note carries the board label. The board refuses to guess."""


def gateway_transport() -> Transport:
    """The production transport: the trilium domain on the local gateway."""
    from awm.gatewayclient import call_sync

    def call(fn: str, args: dict) -> dict:
        return call_sync("trilium", fn, args)

    return call


def _own_attributes(note: dict) -> list[dict]:
    """Attributes the note owns. Trilium also lists inherited ones, which are not the card's."""
    note_id = note.get("noteId")
    return [a for a in note.get("attributes") or []
            if a.get("noteId") in (None, note_id)]


class Vault:
    """Card notes under the board note."""

    def __init__(self, call: Transport | None = None, *,
                 board_label: str = DEFAULT_BOARD_LABEL):
        if not _LABEL_NAME.match(board_label):
            raise ValueError(f"board label must be a bare label name: {board_label!r}")
        self._call_fn = call
        self.board_label = board_label
        self._board_id: str | None = None
        self._note_by_card: dict[str, str] = {}
        self._card_by_note: dict[str, str] = {}

    # -- transport -------------------------------------------------------------

    def _call(self, fn: str, **args: Any) -> dict:
        if self._call_fn is None:
            self._call_fn = gateway_transport()
        try:
            out = self._call_fn(fn, args)
        except VaultError:
            raise
        except Exception as e:
            raise VaultError(f"trilium {fn} failed: {e}") from e
        return out if isinstance(out, dict) else {}

    # -- the board note --------------------------------------------------------

    def board_id(self, *, refresh: bool = False) -> str:
        """The id of the one note carrying the board label. Zero or several refuse."""
        if self._board_id and not refresh:
            return self._board_id
        found = self._call("note_search", query=f"#{self.board_label}", limit=5)
        hits = found.get("results") or []
        if not hits:
            raise NoBoard(f"no note carries #{self.board_label}")
        if len(hits) > 1:
            ids = ", ".join(h.get("noteId", "?") for h in hits)
            raise AmbiguousBoard(f"{len(hits)} notes carry #{self.board_label}: {ids}")
        self._board_id = hits[0]["noteId"]
        return self._board_id

    # -- records ---------------------------------------------------------------

    def _record(self, note: dict, content: str | None) -> dict | None:
        """A card record from a Trilium note, or None when the note is not a card."""
        if self.board_id() not in (note.get("parentNoteIds") or []):
            return None
        labels: dict[str, str] = {}
        relations: dict[str, str] = {}
        for a in _own_attributes(note):
            target = labels if a.get("type") == "label" else relations
            target.setdefault(a.get("name"), a.get("value") or "")
        card_id = labels.get("cardId")
        if not card_id:
            return None
        note_id = note["noteId"]
        self._note_by_card[card_id] = note_id
        self._card_by_note[note_id] = card_id
        return {"note_id": note_id, "title": note.get("title") or "",
                "content": content, "labels": labels, "relations": relations}

    def create(self, title: str, body: str, labels: dict[str, str],
               relations: dict[str, str]) -> dict:
        """Make a card note under the board. The caller supplies ``cardId`` in the labels."""
        made = self._call(
            "note_create", title=title, content=body, parent=self.board_id(),
            type="code", mime="text/plain", labels=labels, relations=relations)
        note_id = made.get("note_id")
        if not note_id:
            raise VaultError(f"note_create returned no note id: {made!r}")
        note = self._call("note_get", note_id=note_id, content=True)
        rec = self._record(note, note.get("content") or "")
        if rec is None:
            raise VaultError(f"created note {note_id} did not come back as a card")
        return rec

    def read(self, card_id: str, *, content: bool = True) -> dict | None:
        """One card's current record, read fresh from the vault, or None when absent."""
        if not re.fullmatch(r"[0-9a-f]{32}", card_id or ""):
            return None
        note_id = self._note_by_card.get(card_id)
        if note_id:
            try:
                rec = self._read_note(note_id, content)
            except VaultError:
                rec = None  # deleted or moved: fall through to a search, which says which
            if rec and rec["labels"].get("cardId") == card_id:
                return rec
            self._note_by_card.pop(card_id, None)
        found = self._call("note_search", query=f"#cardId={card_id}",
                           ancestor=self.board_id(), limit=5)
        notes = [n for n in found.get("results") or []
                 if self.board_id() in (n.get("parentNoteIds") or [])]
        if not notes:
            return None
        if len(notes) > 1:
            raise VaultError(f"{len(notes)} notes carry #cardId={card_id}")
        return self._read_note(notes[0]["noteId"], content)

    def _read_note(self, note_id: str, content: bool) -> dict | None:
        note = self._call("note_get", note_id=note_id, content=content)
        return self._record(note, note.get("content") if content else None)

    def list(self, *, content: bool = False) -> list[dict]:
        """Every card under the board. Bodies cost one read each, so they are opt-in."""
        found = self._call("note_search", query="#cardId",
                           ancestor=self.board_id(), limit=SEARCH_LIMIT)
        out = []
        for n in found.get("results") or []:
            if content:
                rec = self._read_note(n["noteId"], True)
            else:
                rec = self._record(n, None)
            if rec:
                out.append(rec)
        return out

    def update(self, note_id: str, *, labels: dict[str, str] | None = None,
               content: str | None = None) -> None:
        args: dict[str, Any] = {"note_id": note_id}
        if labels:
            args["labels"] = labels
        if content is not None:
            args["content"] = content
        self._call("note_update", **args)

    def card_id_for_note(self, note_id: str) -> str | None:
        """The card id of a note, for resolving a ``~replyTo`` target."""
        if note_id in self._card_by_note:
            return self._card_by_note[note_id]
        rec = self._read_note(note_id, False)
        return rec["labels"]["cardId"] if rec else None
