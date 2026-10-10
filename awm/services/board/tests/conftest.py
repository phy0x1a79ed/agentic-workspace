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
_QUERY = re.compile(r'^#(\w+)(?:="?([^"]*)"?)?$')


class FakeTrilium:
    """Just enough of the `trilium` domain: notes, labels and relations, a label search.

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
        self._lock = threading.RLock()

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
                               "parentNoteIds": [parent] if parent else ["root"]}
        for name, value in (labels or {}).items():
            self.set_attr(note_id, name, value)
        return note_id

    def set_attr(self, note_id: str, name: str, value: str, type: str = "label") -> None:
        for a in self.attrs.values():
            if a["noteId"] == note_id and a["name"] == name and a["type"] == type:
                a["value"] = value
                return
        self.attrs[uuid.uuid4().hex[:12]] = {"noteId": note_id, "name": name,
                                             "value": value, "type": type}

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

    def gui_move(self, card_id: str, column: str) -> None:
        """What dragging a card to another column does: one label write, no lock."""
        with self._lock:
            self.set_attr(self.card_note(card_id), "status", column)

    def all_text(self) -> str:
        """Everything the vault holds, for asserting that something never reached it."""
        parts = [f"{n['title']} {n['content']}" for n in self.notes.values()]
        parts += [f"{a['name']}={a['value']}" for a in self.attrs.values()]
        return "\n".join(parts)

    # -- the verbs -------------------------------------------------------------

    def _pojo(self, note_id: str) -> dict:
        n = self.notes[note_id]
        attributes = [{"attributeId": k, **a} for k, a in self.attrs.items()
                      if a["noteId"] == note_id]
        return {"noteId": note_id, "title": n["title"], "type": n["type"],
                "mime": n["mime"], "parentNoteIds": list(n["parentNoteIds"]),
                "attributes": attributes}

    def _descends(self, note_id: str, ancestor: str) -> bool:
        return any(p == ancestor or self._descends(p, ancestor)
                   for p in self.notes[note_id]["parentNoteIds"] if p in self.notes)

    def _h_note_search(self, query, ancestor=None, limit=50, **_):
        m = _QUERY.match(query)
        if not m:
            raise RuntimeError(f"fake trilium cannot parse {query!r}")
        name, value = m.group(1), m.group(2)
        hits = []
        for note_id in self.notes:
            if ancestor and not self._descends(note_id, ancestor):
                continue
            for a in self.attrs.values():
                if (a["noteId"] == note_id and a["name"] == name and a["type"] == "label"
                        and (value is None or a["value"] == value)):
                    hits.append(self._pojo(note_id))
                    break
        return {"results": hits[:limit]}

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
        for name, value in (labels or {}).items():
            self.set_attr(note_id, name, str(value))
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
