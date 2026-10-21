"""The board's access to Trilium, through the local gateway's `trilium` domain.

The board is a note carrying one marker label. Its children are the column
folders, one per state, and each card is a child of the folder for its state.
Each column folder holds one archive folder, and an archived card lives there,
still in the state of the column around it. In Done and Rejected that folder is
"Older (n)", shown as the last card of the column, which lists its cards newest
first by `cardStateAt`. Elsewhere it is a hidden `Archived` folder. A card
folded into another (`cardFolded` names its home) may sit under the home card's
note instead, and takes the home's state. Every archived card outside an Older
folder carries the `archived` label, which hides it. A card still sitting directly under the board is one
from before the folders existed. This module finds those notes and moves bytes
between them and plain dicts. It knows the `cardId` label because that is how a card is found, and
which folder is which state, and nothing else about what a card means.

Trilium's note verbs are operator-only and admit loopback, so the board has to
run on the host that runs Trilium. The transport is a plain callable
``call(fn, args) -> dict`` so a test can put a fake behind it.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from .states import STATES, TERMINAL, TITLES

log = logging.getLogger("awm.board.vault")

Transport = Callable[[str, dict], dict]

DEFAULT_BOARD_LABEL = "federationBoard"

#: A search over the board returns at most this many cards.
SEARCH_LIMIT = 10000

#: Labels of a card's chat. None of them may be `cardId` or `status`, or a
#: message would count as a card in the search and in the board view.
CHAT_FOLDER = "chatFolder"
CHAT_MESSAGE = "chatMsg"
MESSAGE_SEQ = "msgSeq"

#: Marks a column folder; its value is the state the folder stands for.
COLUMN_LABEL = "cardColumn"

#: Marks the `Archived` folder inside a column folder; its value is the state of that column.
ARCHIVE_FOLDER_LABEL = "cardArchiveFolder"

#: Names the card a folded reply was folded into: its home.
FOLDED = "cardFolded"

#: Hides a note, and the board view hides a folder that carries it.
ARCHIVED = "archived"

#: The title of the archive folder of a column that does not fold: it stays hidden.
ARCHIVE_TITLE = "Archived"

#: The title of the archive folder of Done and Rejected, before its count.
OLDER_TITLE = "Older"

#: When a card entered its state. Done, Rejected and their Older folders sort by it.
STATE_AT = "cardStateAt"

#: Below every ISO time, so an Older folder sorts after every card in its column.
OLDER_AT = "0000"

#: Newest first.
SORTED = {"sorted": STATE_AT, "sortDirection": "desc"}

#: The label that makes Trilium's board view take its columns from the board's child folders.
#: Without it the view groups cards by `#status` and shows no sections.
COLUMNS_FROM = "board:columnsFrom"

#: The labels the board note offers as promoted attributes, with their chip captions.
CHIPS = {"cardFrom": "From", "cardTo": "To", "project": "Project", "owner": "Owner"}

_LABEL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: How a read of a deleted note fails, through ETAPI and the gateway alike.
_MISSING = re.compile(r"\b404\b|NOTE_NOT_FOUND")


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
        self._chat_by_card: dict[str, str] = {}
        #: state to the id of its column folder, as last found. None until looked up.
        self._columns: dict[str, str] | None = None
        #: state to the id of the `Archived` folder inside its column folder. None until looked up.
        self._archives: dict[str, str] | None = None
        self._chips_defined = False
        self._older_ready = False
        #: Cards whose home is being read, to cut a loop of homes.
        self._homing: set[str] = set()

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

    def refresh(self) -> None:
        """Look the board note and its column folders up again."""
        self.board_id(refresh=True)
        self._columns = None
        self._archives = None

    # -- the column folders ----------------------------------------------------

    def columns(self) -> dict[str, str]:
        """State to the id of its column folder, for the folders that exist. Makes none."""
        if self._columns is None:
            board = self.board_id()
            found = self._call("note_search", query=f"#{COLUMN_LABEL}", ancestor=board, limit=50)
            columns: dict[str, str] = {}
            for note in sorted(found.get("results") or [], key=lambda n: n.get("noteId", "")):
                if board not in (note.get("parentNoteIds") or []):
                    continue
                state = next((a.get("value") for a in _own_attributes(note)
                              if a.get("name") == COLUMN_LABEL and a.get("type") == "label"), None)
                if state not in STATES:
                    continue
                if state in columns:
                    log.warning("two folders stand for state %s: using %s, ignoring %s",
                                state, columns[state], note["noteId"])
                    continue
                columns[state] = note["noteId"]
            self._columns = columns
        return self._columns

    def archives(self) -> dict[str, str]:
        """State to the id of its column's `Archived` folder, for the folders that exist. Makes none."""
        if self._archives is None:
            columns = self.columns()
            found = self._call("note_search", query=f"#{ARCHIVE_FOLDER_LABEL}",
                               ancestor=self.board_id(), limit=50, archived=True)
            by_column = {folder: state for state, folder in columns.items()}
            archives: dict[str, str] = {}
            for note in sorted(found.get("results") or [], key=lambda n: n.get("noteId", "")):
                state = next((by_column[p] for p in note.get("parentNoteIds") or []
                              if p in by_column), None)
                if state is None:
                    continue
                if state in archives:
                    log.warning("two archive folders in the %s column: using %s, ignoring %s",
                                state, archives[state], note["noteId"])
                    continue
                archives[state] = note["noteId"]
            self._archives = archives
        return self._archives

    def ensure_columns(self) -> dict[str, str]:
        """Make each missing column folder and its `Archived` folder, in board order, and the board's chip definitions. Idempotent."""
        columns = self.columns()
        if len(columns) < len(STATES):
            board = self.board_id()
            for state in STATES:
                if state in columns:
                    continue
                made = self._call("note_create", title=TITLES[state], content="", parent=board,
                                  type="text", labels={COLUMN_LABEL: state})
                if not made.get("note_id"):
                    raise VaultError(f"note_create returned no note id: {made!r}")
                columns[state] = made["note_id"]
        archives = self.archives()
        for state in STATES:
            if state in archives:
                continue
            if state in TERMINAL:
                title, labels = OLDER_TITLE, {ARCHIVE_FOLDER_LABEL: state, STATE_AT: OLDER_AT, **SORTED}
            else:
                title, labels = ARCHIVE_TITLE, {ARCHIVE_FOLDER_LABEL: state, ARCHIVED: ""}
            made = self._call("note_create", title=title, content="", parent=columns[state],
                              type="text", labels=labels)
            if not made.get("note_id"):
                raise VaultError(f"note_create returned no note id: {made!r}")
            archives[state] = made["note_id"]
        if not self._older_ready:
            for state, note_id, labels in self.older_changes():
                self.update(note_id, labels=labels)
                if ARCHIVED in labels:
                    self._call("attr_delete", note_id=note_id, name=ARCHIVED)
            self._older_ready = True
        if not self._chips_defined:
            self._define_board_view()
        return columns

    def older_changes(self) -> list[tuple[str, str, dict[str, str]]]:
        """What Done, Rejected and their archive folders lack to sort newest first and show the Older folder.

        Each entry is a state, a note and the labels it lacks. An ``archived``
        entry means the note carries that label and must lose it.
        """
        columns, archives = self.columns(), self.archives()
        out = []
        for state in TERMINAL:
            for note_id, want in ((columns.get(state), SORTED),
                                  (archives.get(state), {**SORTED, STATE_AT: OLDER_AT})):
                if note_id is None:
                    continue
                held = {a.get("name"): a.get("value") or "" for a in _own_attributes(
                    self._call("note_get", note_id=note_id, content=False)) if a.get("type") == "label"}
                lack = {k: v for k, v in want.items() if held.get(k) != v}
                if note_id == archives.get(state) and ARCHIVED in held:
                    lack[ARCHIVED] = ""
                if lack:
                    out.append((state, note_id, lack))
        return out

    def count_older(self) -> None:
        """Title each Older folder "Older (n)" by the cards in it."""
        for state in TERMINAL:
            folder = self.archives().get(state)
            if folder is None:
                continue
            title = f"{OLDER_TITLE} ({self._call('note_children', note_id=folder).get('count', 0)})"
            if self._call("note_get", note_id=folder, content=False).get("title") != title:
                self._call("note_update", note_id=folder, title=title)

    def _define_board_view(self) -> None:
        """Group the board view by its folders, and offer the chip labels as promoted attributes on every card."""
        board = self.board_id()
        held = {(a.get("name"), a.get("value")) for a in _own_attributes(
            self._call("note_get", note_id=board, content=False))}
        names = {name for name, _ in held}
        if (COLUMNS_FROM, "folders") not in held:
            self._call("attr_set", note_id=board, name=COLUMNS_FROM, value="folders")
        for label, caption in CHIPS.items():
            if f"label:{label}" not in names:
                self._call("attr_set", note_id=board, name=f"label:{label}",
                           value=f"promoted,single,text,alias={caption}", inheritable=True)
        self._chips_defined = True

    # -- records ---------------------------------------------------------------

    def _record(self, note: dict, content: str | None,
                homes: dict[str, dict | None] | None = None) -> dict | None:
        """A card record from a Trilium note, or None when the note is not a card.

        ``state`` is the state of the column folder the card sits in, or of the
        column around the `Archived` folder it sits in, or of its home card when
        it sits under that card's note, or None for a card still directly under
        the board, whose state is its `status` label. ``archived_in`` says
        whether the card sits in an `Archived` folder or under its home.
        ``home_note`` is the note of the home card it sits under, else None.
        ``homes`` memoizes home records across the notes of one search.
        """
        parents = note.get("parentNoteIds") or []
        by_folder = {folder: state for state, folder in self.columns().items()}
        state = next((by_folder[p] for p in parents if p in by_folder), None)
        in_archive = False
        if state is None:
            by_archive = {folder: state for state, folder in self.archives().items()}
            state = next((by_archive[p] for p in parents if p in by_archive), None)
            in_archive = state is not None
        labels: dict[str, str] = {}
        relations: dict[str, str] = {}
        for a in _own_attributes(note):
            target = labels if a.get("type") == "label" else relations
            target.setdefault(a.get("name"), a.get("value") or "")
        card_id = labels.get("cardId")
        if not card_id:
            return None
        home_note = None
        if state is None and labels.get(FOLDED) and self.board_id() not in parents:
            home = self._home_above(labels[FOLDED], card_id, parents, homes)
            if home:
                home_note, state, in_archive = home["note_id"], home["state"], True
        if state is None and home_note is None and self.board_id() not in parents:
            return None
        note_id = note["noteId"]
        self._note_by_card[card_id] = note_id
        self._card_by_note[note_id] = card_id
        return {"note_id": note_id, "title": note.get("title") or "",
                "content": content, "labels": labels, "relations": relations, "state": state,
                "archived_in": in_archive, "home_note": home_note}

    def _home_above(self, target: str, card_id: str, parents: list[str],
                    homes: dict[str, dict | None] | None) -> dict | None:
        """The card on the chain of homes starting at ``target`` whose note is one of ``parents``, if any."""
        seen = {card_id}
        while target and target not in seen:
            seen.add(target)
            home = self._home(target, card_id, homes)
            if home is None:
                return None
            if home["note_id"] in parents:
                return home
            target = home["labels"].get(FOLDED, "")
        return None

    def _home(self, home_id: str, card_id: str, homes: dict[str, dict | None] | None) -> dict | None:
        """The record of the card ``home_id``, for a card folded into it. None when it is gone or is the card itself."""
        if home_id == card_id:
            return None
        if homes is not None and home_id in homes:
            return homes[home_id]
        if card_id in self._homing:                         # a loop of homes: stop at the second visit
            return None
        self._homing.add(card_id)
        try:
            home = self.read(home_id, content=False)
        finally:
            self._homing.discard(card_id)
        if homes is not None:
            homes[home_id] = home
        return home

    def create(self, title: str, body: str, labels: dict[str, str],
               relations: dict[str, str], state: str = "proposed") -> dict:
        """Make a card note in the column for ``state``. The caller supplies ``cardId`` in the labels."""
        made = self._call(
            "note_create", title=title, content=body, parent=self.ensure_columns()[state],
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
                           ancestor=self.board_id(), limit=5, archived=True)
        notes = [n for n in found.get("results") or [] if self._record(n, None)]
        if not notes:
            return None
        if len(notes) > 1:
            raise VaultError(f"{len(notes)} notes carry #cardId={card_id}")
        return self._read_note(notes[0]["noteId"], content)

    def _read_note(self, note_id: str, content: bool) -> dict | None:
        note = self._call("note_get", note_id=note_id, content=content)
        return self._record(note, note.get("content") if content else None)

    def list(self, *, content: bool = False) -> list[dict]:
        """Every card under the board, except the archived ones. Bodies cost one read each, so they are opt-in."""
        found = self._call("note_search", query="#cardId",
                           ancestor=self.board_id(), limit=SEARCH_LIMIT)
        out = []
        for n in found.get("results") or []:
            rec = self._record(n, None)
            if rec is None or rec["archived_in"]:
                continue
            out.append(self._read_note(n["noteId"], True) if content else rec)
        return out

    def update(self, note_id: str, *, labels: dict[str, str] | None = None,
               content: str | None = None) -> None:
        args: dict[str, Any] = {"note_id": note_id}
        if labels:
            args["labels"] = labels
        if content is not None:
            args["content"] = content
        self._call("note_update", **args)

    def _move(self, rec: dict, parent: str) -> None:
        """Put the card under ``parent`` and out of the board's other notes.

        A parent outside the board stays, such as the Swarm board column a
        claimed card also sits in.
        """
        here = {self.board_id(), *self.columns().values(), *self.archives().values()}
        if rec.get("home_note"):
            here.add(rec["home_note"])
        self._call("note_place", note_id=rec["note_id"], parents=[parent], remove=sorted(here - {parent}))

    def place(self, rec: dict, state: str) -> None:
        """Move the card into the folder for ``state``, and drop the `status` label of the old grouping."""
        if rec.get("state") != state:
            self._move(rec, self.ensure_columns()[state])
        if "status" in rec["labels"]:
            self._call("attr_delete", note_id=rec["note_id"], name="status")

    def archive(self, rec: dict, state: str, labels: dict[str, str] | None = None,
                home: dict | None = None) -> None:
        """File the card in the archive folder of the column for ``state``.

        With a ``home`` record the card goes under the home card's note instead.
        A card in an Older folder shows there; any other archived card is
        labelled `archived`, which hides it. The labels go first and the move
        last, as every write does. A card already filed there is not moved
        again. Unarchiving is moving the card back out.
        """
        hide = home is not None or state not in TERMINAL
        extra = {**(labels or {}), **({ARCHIVED: ""} if hide else {})}
        if extra:
            self.update(rec["note_id"], labels=extra)
        if home is not None:
            self.file_under(rec, home)
        elif not (rec.get("archived_in") and rec.get("state") == state):
            self.ensure_columns()
            self._move(rec, self.archives()[state])
        if "status" in rec["labels"]:
            self._call("attr_delete", note_id=rec["note_id"], name="status")

    def file_under(self, rec: dict, home: dict) -> None:
        """Move the card under the note of its ``home`` card, unless it is there already."""
        if rec.get("home_note") != home["note_id"]:
            self._move(rec, home["note_id"])

    def unlabel_archived(self, note_id: str) -> None:
        """Take the `archived` label off a card, so it shows in its Older folder."""
        self._call("attr_delete", note_id=note_id, name=ARCHIVED)

    def list_archived(self) -> list[dict]:
        """Every card note labelled `archived` under the board, wherever it sits."""
        found = self._call("note_search", query=f"#cardId #{ARCHIVED}",
                           ancestor=self.board_id(), limit=SEARCH_LIMIT, archived=True)
        homes: dict[str, dict | None] = {}
        return [rec for n in found.get("results") or [] if (rec := self._record(n, None, homes))]

    # -- card chats ------------------------------------------------------------

    def create_child(self, parent: str, title: str, body: str,
                     labels: dict[str, str]) -> str:
        """Make a plain-text note under ``parent`` and return its id. It is never a card."""
        made = self._call("note_create", title=title, content=body, parent=parent,
                          type="code", mime="text/plain", labels=labels)
        note_id = made.get("note_id")
        if not note_id:
            raise VaultError(f"note_create returned no note id: {made!r}")
        return note_id

    def restore(self, note_id: str, title: str, body: str, labels: dict[str, str]) -> None:
        """Put a message note back as recorded. Trilium writes only what differs."""
        self._call("note_update", note_id=note_id, title=title, content=body, labels=labels)

    def ensure_chat(self, card_note: str) -> str:
        """The id of the card's `Chat` note, made on first use.

        It is a collection shown in the `chat` view, sorted by message sequence.
        ``chatFolder`` marks it, and the inheritable ``chatMsg`` marks every note
        under it, which is how a note a person adds in the GUI is found.
        """
        chat = self._chat_by_card.get(card_note)
        if chat:
            return chat
        found = self._call("note_search", query=f"#{CHAT_FOLDER}", ancestor=card_note, limit=5,
                           archived=True)
        hits = [n for n in found.get("results") or []
                if card_note in (n.get("parentNoteIds") or [])]
        if hits:
            chat = hits[0]["noteId"]
            marked = any(a.get("name") == CHAT_MESSAGE and a.get("isInheritable")
                         for a in _own_attributes(hits[0]))
        else:
            made = self._call("note_create", title="Chat", content="", parent=card_note,
                              type="book", labels={"viewType": "chat", "sorted": MESSAGE_SEQ,
                                                   CHAT_FOLDER: ""})
            chat = made.get("note_id")
            if not chat:
                raise VaultError(f"note_create returned no note id: {made!r}")
            marked = False
        if not marked:
            self._call("attr_set", note_id=chat, name=CHAT_MESSAGE, value="", inheritable=True)
        self._chat_by_card[card_note] = chat
        return chat

    def unsealed_messages(self) -> list[dict]:
        """Notes under some card's chat that carry no sequence yet: ones a person added in the GUI."""
        found = self._call("note_search",
                           query=f"#{CHAT_MESSAGE} #!{CHAT_FOLDER} #!{MESSAGE_SEQ}",
                           ancestor=self.board_id(), limit=SEARCH_LIMIT)
        return found.get("results") or []

    def card_note_of_chat(self, chat: str) -> str | None:
        """The card note a `Chat` note belongs to, or None when ``chat`` is not one."""
        for card_note, known in self._chat_by_card.items():
            if known == chat:
                return card_note
        try:
            note = self._call("note_get", note_id=chat, content=False)
        except VaultError:
            return None
        if not any(a.get("name") == CHAT_FOLDER for a in _own_attributes(note)):
            return None
        for parent in note.get("parentNoteIds") or []:
            if self.card_id_for_note(parent):
                self._chat_by_card[parent] = chat
                return parent
        return None

    def message_note(self, note_id: str) -> dict | None:
        """A message note with its body and own labels, or None when Trilium no longer has it.

        Any other failure raises, so a flaky call never reads as a deletion.
        """
        try:
            note = self._call("note_get", note_id=note_id, content=True)
        except VaultError as e:
            if _MISSING.search(str(e)):
                return None
            raise
        labels = {a.get("name"): a.get("value") or "" for a in _own_attributes(note)
                  if a.get("type") == "label"}
        return {"note_id": note_id, "title": note.get("title") or "",
                "content": note.get("content") or "", "labels": labels,
                "modified": note.get("utcDateModified") or ""}

    def card_id_for_note(self, note_id: str) -> str | None:
        """The card id of a note, for resolving a ``~replyTo`` target."""
        if note_id in self._card_by_note:
            return self._card_by_note[note_id]
        rec = self._read_note(note_id, False)
        return rec["labels"]["cardId"] if rec else None
