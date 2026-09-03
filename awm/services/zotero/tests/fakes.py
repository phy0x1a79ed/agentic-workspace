"""Stand-ins for the two things this service talks to.

Both are dictionaries. The Zotero end is a desktop application on another
machine that is sometimes asleep, and the vault end is a supervised Trilium
child — neither is a thing to reach for while testing whether a paper in three
collections becomes one note in three places.
"""

from __future__ import annotations

from typing import Any


class FakeVault:
    """The narrow interface `sync.apply` needs, backed by a dict.

    Deliberately the same shape as `awm.zotero.vault.Vault` and no wider: the
    point of that facade is that a mirror needs eight verbs, and a fake that
    offered more would let the sync grow a dependence the real one cannot meet.
    """

    def __init__(self) -> None:
        #: note id -> {"title", "content", "labels", "parents", "attachments"}
        self.notes: dict[str, dict] = {}
        self.next_id = 0
        self.calls: list[str] = []

    def _new(self, title: str, content: str, parent: str,
             labels: dict[str, str]) -> str:
        self.next_id += 1
        nid = f"n{self.next_id}"
        self.notes[nid] = {"title": title, "content": content,
                           "labels": dict(labels), "parents": [parent],
                           "attachments": {}}
        return nid

    # -- the interface -------------------------------------------------------

    def owned(self, root: str, label: str) -> dict[str, str]:
        self.calls.append("owned")
        return {n["labels"][label]: nid for nid, n in self.notes.items()
                if n["labels"].get(label) and self._under(nid, root)}

    def _under(self, note_id: str, root: str, guard: int = 0) -> bool:
        if note_id == root or guard > 50:
            return note_id == root
        return any(self._under(p, root, guard + 1)
                   for p in self.notes[note_id]["parents"])

    def read(self, note_id: str) -> dict:
        return dict(self.notes[note_id])

    def ensure(self, *, parent: str, title: str, content: str,
               type: str = "book") -> str:
        self.calls.append("ensure")
        for nid, n in self.notes.items():
            if n["title"] == title and parent in n["parents"]:
                return nid
        return self._new(title, content, parent, {})

    def create(self, *, parent: str, title: str, content: str = "",
               type: str = "text", labels: dict[str, str] | None = None) -> str:
        self.calls.append("create")
        return self._new(title, content, parent, labels or {})

    def update(self, note_id: str, *, title: str | None = None,
               content: str | None = None,
               labels: dict[str, str] | None = None) -> bool:
        self.calls.append("update")
        note, changed = self.notes[note_id], False
        if title is not None and note["title"] != title:
            note["title"], changed = title, True
        if content is not None and note["content"] != content:
            note["content"], changed = content, True
        for name, value in (labels or {}).items():
            if note["labels"].get(name) != value:
                note["labels"][name], changed = value, True
        return changed

    def set_label(self, note_id: str, name: str, value: str) -> bool:
        note = self.notes[note_id]
        if note["labels"].get(name) == value:
            return False
        note["labels"][name] = value
        return True

    def place(self, note_id: str, parents: list[str]) -> dict:
        self.calls.append("place")
        self.notes[note_id]["parents"] = sorted(set(parents))
        return {"note_id": note_id, "parents": sorted(set(parents))}

    def attach(self, note_id: str, path, *, title: str | None = None) -> bool:
        self.calls.append("attach")
        name = title or path.name
        blob = path.read_bytes()
        note = self.notes[note_id]
        if note["attachments"].get(name) == len(blob):
            return False
        note["attachments"][name] = len(blob)
        return True

    def delete(self, note_id: str) -> None:
        self.calls.append("delete")
        self.notes.pop(note_id, None)

    # -- helpers a test may use ---------------------------------------------

    def titles_under(self, root: str) -> list[str]:
        return sorted(n["title"] for nid, n in self.notes.items()
                      if root in n["parents"])


def item(key: str, *, library: str = "users/0", library_name: str = "My Library",
         title: str = "A paper", **extra: Any) -> dict:
    """One normalized item, as `bundle.normalize` would emit it."""
    out = {"key": key, "ref": f"{library}/{key}", "library": library,
           "library_name": library_name, "title": title, "version": 1,
           "item_type": "journalArticle"}
    out.update(extra)
    return out


def collection(key: str, name: str, *, library: str = "users/0",
               parent: str = "") -> dict:
    return {"key": key, "ref": f"{library}/{key}", "library": library,
            "name": name, "parent": parent}
