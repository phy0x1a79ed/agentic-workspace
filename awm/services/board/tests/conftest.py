"""Shared fixtures: a fake Trilium behind the vault's transport, and a board on top of it."""

from __future__ import annotations

import re
import threading
import time
import uuid

import pytest

from awm.board.cards import Board
from awm.board.events import Events
from awm.board.parties import Parties
from awm.board.vault import Vault

BOARD_LABEL = "federationBoard"
_TERM = re.compile(r'^#(!?)(\w+)(?:="?([^"]*)"?)?$')


class FakeTrilium:
    """Just enough of the `trilium` domain: notes, labels and relations, a label search.

    Labels inherit as Trilium's do: an inheritable label applies to the note's
    whole subtree, and a search matches it on every note it applies to.

    It models what the vault calls and raises on anything else, so a test cannot
    pass because the fake quietly agreed. `hooks[fn]` runs before that verb, which
    is how a test lands a GUI drag at an exact point inside a claim.
    """

    def __init__(self) -> None:
        self.notes: dict[str, dict] = {}
        self.attrs: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.hooks: dict[str, list] = {}
        self.latency = 0.0
        #: One-shot faults for `note_update`. Trilium writes labels one call at a
        #: time, so a call can die partway (`fail_after_labels` = how many labels
        #: land first) or land fully and still report a failure (`fail_after_apply`).
        self.fail_after_labels: int | None = None
        self.fail_after_apply = False
        #: One-shot fault for `note_move`: "before" refuses it, "after" lands it and still reports a failure.
        self.fail_move: str | None = None
        self._lock = threading.RLock()
        self.clock = 1_000_000.0

    # -- the transport the vault is given -------------------------------------

    def __call__(self, fn: str, args: dict) -> dict:
        self.calls.append((fn, dict(args)))
        for hook in list(self.hooks.get(fn, [])):
            hook(args)
        handler = getattr(self, f"_h_{fn}", None)
        if handler is None:
            raise RuntimeError(f"fake trilium does not model {fn}")
        with self._lock:
            return handler(**args)

    # -- helpers a test uses directly ------------------------------------------

    def add_note(self, title: str, parent: str | None = None, content: str = "",
                 labels: dict | None = None) -> str:
        note_id = uuid.uuid4().hex[:12]
        self.notes[note_id] = {"noteId": note_id, "title": title, "content": content,
                               "type": "text", "mime": "text/html",
                               "parentNoteIds": [parent] if parent else ["root"],
                               "modified": self.clock}
        for name, value in (labels or {}).items():
            self.set_attr(note_id, name, value)
        return note_id

    def set_attr(self, note_id: str, name: str, value: str, type: str = "label",
                 inheritable: bool = False) -> None:
        for a in self.attrs.values():
            if a["noteId"] == note_id and a["name"] == name and a["type"] == type:
                a["value"] = value
                a["isInheritable"] = inheritable
                return
        self.attrs[uuid.uuid4().hex[:12]] = {"noteId": note_id, "name": name,
                                             "value": value, "type": type,
                                             "isInheritable": inheritable}

    def touch(self, note_id: str) -> None:
        self.notes[note_id]["modified"] = self.clock

    def children(self, parent: str) -> list[str]:
        return [n for n, note in self.notes.items() if parent in note["parentNoteIds"]]

    def label(self, note_id: str, name: str) -> str | None:
        for a in self.attrs.values():
            if a["noteId"] == note_id and a["name"] == name and a["type"] == "label":
                return a["value"]
        return None

    def card_note(self, card_id: str) -> str:
        for a in self.attrs.values():
            if a["name"] == "cardId" and a["value"] == card_id:
                return a["noteId"]
        raise KeyError(card_id)

    def column(self, state: str) -> str:
        """The column folder for ``state``."""
        for a in self.attrs.values():
            if a["name"] == "cardColumn" and a["value"] == state:
                return a["noteId"]
        raise KeyError(state)

    def state_of(self, card_id: str) -> str | None:
        """The state of the folder the card sits in, or of the column around its `Archived` folder.

        None when it sits elsewhere.
        """
        (parent,) = self.notes[self.card_note(card_id)]["parentNoteIds"]
        if self.label(parent, "cardId") is not None:                 # under its home card
            return self.state_of(self.label(parent, "cardId"))
        if self.label(parent, "cardArchiveFolder") is not None:
            (parent,) = self.notes[parent]["parentNoteIds"]
        for a in self.attrs.values():
            if a["noteId"] == parent and a["name"] == "cardColumn":
                return a["value"]
        return None

    def archive_folder(self, state: str) -> str:
        """The `Archived` folder inside the column folder for ``state``."""
        (found,) = [n for n in self.children(self.column(state))
                    if self.label(n, "cardArchiveFolder") is not None]
        return found

    def is_filed_archived(self, card_id: str) -> bool:
        """Whether the card sits in an `Archived` folder."""
        (parent,) = self.notes[self.card_note(card_id)]["parentNoteIds"]
        return self.label(parent, "cardArchiveFolder") is not None

    def archives_of_all(self) -> list[str]:
        """Every `Archived` folder's note id."""
        return [a["noteId"] for a in self.attrs.values() if a["name"] == "cardArchiveFolder"]

    def chat_of(self, card_id: str) -> str:
        """The card's `Chat` note."""
        (chat,) = [n for n in self.children(self.card_note(card_id))
                   if self.label(n, "chatFolder") is not None]
        return chat

    def is_under(self, card_id: str, home_id: str) -> bool:
        """Whether the card's note is a child of the home card's note."""
        return self.notes[self.card_note(card_id)]["parentNoteIds"] == [self.card_note(home_id)]

    def gui_move(self, card_id: str, state: str) -> None:
        """What dragging a card to another column folder does: one branch change, no lock."""
        with self._lock:
            self.notes[self.card_note(card_id)]["parentNoteIds"] = [self.column(state)]

    def columns(self) -> list[str]:
        """The states of the column folders, in tree order."""
        board = next(a["noteId"] for a in self.attrs.values() if a["name"] == BOARD_LABEL)
        out = []
        for n in self.children(board):
            value = self.label(n, "cardColumn")
            if value is not None:
                out.append(value)
        return out

    def all_text(self) -> str:
        """Everything the vault holds, for asserting that something never reached it."""
        parts = [f"{n['title']} {n['content']}" for n in self.notes.values()]
        parts += [f"{a['name']}={a['value']}" for a in self.attrs.values()]
        return "\n".join(parts)

    # -- the verbs -------------------------------------------------------------

    def _ancestors(self, note_id: str) -> set[str]:
        out: set[str] = set()
        for p in self.notes[note_id]["parentNoteIds"]:
            if p in self.notes and p not in out:
                out |= {p} | self._ancestors(p)
        return out

    def _effective(self, note_id: str) -> list[tuple[str, dict]]:
        """The note's own attributes, then the inheritable ones of its ancestors."""
        above = self._ancestors(note_id)
        return [(k, a) for k, a in self.attrs.items()
                if a["noteId"] == note_id or (a["noteId"] in above and a.get("isInheritable"))]

    def _pojo(self, note_id: str) -> dict:
        n = self.notes[note_id]
        attributes = [{"attributeId": k, **a} for k, a in self._effective(note_id)]
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(n["modified"])) + ".000Z"
        return {"noteId": note_id, "title": n["title"], "type": n["type"],
                "mime": n["mime"], "parentNoteIds": list(n["parentNoteIds"]),
                "utcDateModified": stamp, "attributes": attributes}

    def _descends(self, note_id: str, ancestor: str) -> bool:
        return any(p == ancestor or self._descends(p, ancestor)
                   for p in self.notes[note_id]["parentNoteIds"] if p in self.notes)

    def _h_note_search(self, query, ancestor=None, limit=50, archived=False, **_):
        terms = []
        for word in query.split():
            m = _TERM.match(word)
            if not m:
                raise RuntimeError(f"fake trilium cannot parse {query!r}")
            terms.append((m.group(1) == "!", m.group(2), m.group(3)))
        hits = []
        for note_id in self.notes:
            if ancestor and not self._descends(note_id, ancestor):
                continue
            labels = [a for _, a in self._effective(note_id) if a["type"] == "label"]
            if not archived and any(a["name"] == "archived" for a in labels):
                continue
            if all(negated != any(a["name"] == name and (value is None or a["value"] == value)
                                  for a in labels)
                   for negated, name, value in terms):
                hits.append(self._pojo(note_id))
        return {"results": hits[:limit]}

    def _h_attr_set(self, note_id, name, value="", type="label", inheritable=False):
        if note_id not in self.notes:
            raise RuntimeError(f"404 no note {note_id}")
        self.set_attr(note_id, name, str(value), type, bool(inheritable))
        return {"note_id": note_id}

    def _h_attr_delete(self, note_id, name, type="label"):
        if note_id not in self.notes:
            raise RuntimeError(f"404 no note {note_id}")
        gone = [k for k, a in self.attrs.items()
                if a["noteId"] == note_id and a["name"] == name and a["type"] == type]
        for k in gone:
            del self.attrs[k]
        return {"note_id": note_id, "name": name, "removed": len(gone)}

    def _h_note_move(self, note_id, parent):
        if note_id not in self.notes or parent not in self.notes:
            raise RuntimeError(f"404 no note {note_id} or {parent}")
        if self.fail_move == "before":
            self.fail_move = None
            raise RuntimeError("fault: the move was refused")
        self.notes[note_id]["parentNoteIds"] = [parent]
        self.touch(note_id)
        if self.fail_move == "after":
            self.fail_move = None
            raise RuntimeError("fault: the move landed and the reply was lost")
        return {"note_id": note_id, "parent": parent}

    def _h_note_place(self, note_id, parents, remove=None):
        if note_id not in self.notes or any(p not in self.notes for p in parents):
            raise RuntimeError(f"404 no note {note_id} or one of {parents}")
        if self.fail_move == "before":
            self.fail_move = None
            raise RuntimeError("fault: the move was refused")
        current = self.notes[note_id]["parentNoteIds"]
        drop = set(current) - set(parents) if remove is None else set(remove) - set(parents)
        self.notes[note_id]["parentNoteIds"] = [p for p in current if p not in drop] + \
            [p for p in parents if p not in current]
        self.touch(note_id)
        if self.fail_move == "after":
            self.fail_move = None
            raise RuntimeError("fault: the move landed and the reply was lost")
        return {"note_id": note_id, "parents": self.notes[note_id]["parentNoteIds"]}

    def _h_note_children(self, note_id):
        kids = self.children(note_id)
        return {"parent": note_id, "count": len(kids), "children": [
            {"note_id": k, "title": self.notes[k]["title"]} for k in kids]}

    def _h_note_create(self, title, content="", parent="root", type="text", mime=None,
                       labels=None, relations=None):
        note_id = self.add_note(title, parent, content)
        self.notes[note_id].update(type=type, mime=mime or "text/html")
        for name, value in (labels or {}).items():
            self.set_attr(note_id, name, str(value))
        for name, target in (relations or {}).items():
            self.set_attr(note_id, name, target, type="relation")
        return {"note_id": note_id, "created": True}

    def _h_note_get(self, note_id, content=True):
        if note_id not in self.notes:
            raise RuntimeError(f"404 no note {note_id}")
        time.sleep(self.latency)
        out = self._pojo(note_id)
        if content:
            out["content"] = self.notes[note_id]["content"]
        return out

    def _h_note_update(self, note_id, title=None, content=None, labels=None, **_):
        if note_id not in self.notes:
            raise RuntimeError(f"404 no note {note_id}")
        if title is not None:
            self.notes[note_id]["title"] = title
        if content is not None:
            self.notes[note_id]["content"] = content
        self.touch(note_id)
        written = 0
        for name, value in (labels or {}).items():
            self.set_attr(note_id, name, str(value))
            written += 1
            if self.fail_after_labels is not None and written >= self.fail_after_labels:
                self.fail_after_labels = None
                raise RuntimeError("fault: the call died after a label write")
        if self.fail_after_apply:
            self.fail_after_apply = False
            raise RuntimeError("fault: the call timed out after the write landed")
        return {"note_id": note_id, "changed": {}}


@pytest.fixture
def trilium():
    return FakeTrilium()


@pytest.fixture
def board_note(trilium):
    return trilium.add_note("Federation board", labels={BOARD_LABEL: ""})


@pytest.fixture
def vault(trilium, board_note):
    return Vault(trilium, board_label=BOARD_LABEL)


@pytest.fixture
def events(tmp_path):
    return Events(tmp_path / "events.db")


@pytest.fixture
def parties(tmp_path):
    return Parties(tmp_path / "parties.db")


@pytest.fixture
def board(vault, events, tmp_path):
    b = Board(vault, events, lock_path=tmp_path / "board.lock")
    yield b
    b.close()


@pytest.fixture
def make_party(parties):
    def make(swarm, principal="op", relation="domestic"):
        row, _token = parties.add(swarm, principal, relation)
        return row
    return make


@pytest.fixture
def tony(make_party):
    return make_party("tony", "agent")


@pytest.fixture
def collins(make_party):
    return make_party("collins", "john", "sovereign")
