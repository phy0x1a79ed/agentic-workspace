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
        #: (verb, note id) per call. Keyed by note because half of what this
        #: change is worth is "exactly one note was touched", which a bare list
        #: of verb names cannot say.
        self.calls: list[tuple[str, str]] = []
        #: Notes Trilium would hide from a search. The real vault facade always
        #: asks to see them; a test flips `sees_archived` off to show what the
        #: mirror does when it cannot.
        self.archived: set[str] = set()
        self.sees_archived = True

    def _hidden(self, note_id: str, guard: int = 0) -> bool:
        """Archived, or under something archived — Trilium inherits the flag."""
        if self.sees_archived:
            return False
        if note_id in self.archived:
            return True
        if guard > 50 or note_id not in self.notes:
            return False
        return any(self._hidden(p, guard + 1)
                   for p in self.notes[note_id]["parents"])

    @property
    def verbs(self) -> list[str]:
        """Just the verb names, for a test that only asks whether something
        happened at all."""
        return [verb for verb, _ in self.calls]

    def touched(self, *verbs: str) -> set[str]:
        """Which notes these verbs were called on."""
        wanted = set(verbs)
        return {nid for verb, nid in self.calls if verb in wanted and nid}

    def _new(self, title: str, content: str, parent: str,
             labels: dict[str, str]) -> str:
        self.next_id += 1
        nid = f"n{self.next_id}"
        self.notes[nid] = {"title": title, "content": content,
                           "labels": dict(labels), "parents": [parent],
                           "attachments": {}}
        return nid

    # -- the interface -------------------------------------------------------

    def scan(self, root: str | None, label: str) -> dict[str, list[dict]]:
        self.calls.append(("scan", ""))
        out: dict[str, list[dict]] = {}
        for nid, n in self.notes.items():
            value = n["labels"].get(label)
            if value and (root is None or self._under(nid, root)) \
                    and not self._hidden(nid):
                out.setdefault(value, []).append({
                    "note_id": nid,
                    "title": n["title"],
                    "labels": dict(n["labels"]),
                    "parents": list(n["parents"]),
                })
        return out

    def owned_all(self, root: str | None, label: str) -> dict[str, list[str]]:
        return {value: [n["note_id"] for n in notes]
                for value, notes in self.scan(root, label).items()}

    def labelled(self, label: str) -> list[dict[str, Any]]:
        self.calls.append(("labelled", ""))
        return sorted(
            ({"note_id": nid, "title": n["title"], "labels": dict(n["labels"]),
              "parents": list(n["parents"])}
             for nid, n in self.notes.items()
             if label in n["labels"] and not self._hidden(nid)),
            key=lambda h: h["note_id"])

    def owned(self, root: str, label: str) -> dict[str, str]:
        return {k: v[0] for k, v in self.owned_all(root, label).items()}

    def _under(self, note_id: str, root: str, guard: int = 0) -> bool:
        if note_id == root or guard > 50:
            return note_id == root
        # `root` itself is not a note here, so a walk that reaches it stops.
        if note_id not in self.notes:
            return False
        return any(self._under(p, root, guard + 1)
                   for p in self.notes[note_id]["parents"])

    def read(self, note_id: str) -> dict:
        return dict(self.notes[note_id])

    def ensure(self, *, parent: str, title: str, content: str,
               type: str = "book") -> str:
        self.calls.append(("ensure", ""))
        for nid, n in self.notes.items():
            if n["title"] == title and parent in n["parents"]:
                return nid
        return self._new(title, content, parent, {})

    def create(self, *, parent: str, title: str, content: str = "",
               type: str = "text", labels: dict[str, str] | None = None) -> str:
        self.calls.append(("create", ""))
        return self._new(title, content, parent, labels or {})

    def update(self, note_id: str, *, title: str | None = None,
               content: str | None = None,
               labels: dict[str, str] | None = None) -> bool:
        self.calls.append(("update", note_id))
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
        self.calls.append(("set_label", note_id))
        note = self.notes[note_id]
        if note["labels"].get(name) == value:
            return False
        note["labels"][name] = value
        return True

    def place(self, note_id: str, parents: list[str]) -> dict:
        self.calls.append(("place", note_id))
        self.notes[note_id]["parents"] = sorted(set(parents))
        return {"note_id": note_id, "parents": sorted(set(parents))}

    def attach(self, note_id: str, path, *, title: str | None = None) -> bool:
        self.calls.append(("attach", note_id))
        name = title or path.name
        blob = path.read_bytes()
        note = self.notes[note_id]
        if note["attachments"].get(name) == len(blob):
            return False
        note["attachments"][name] = len(blob)
        return True

    def delete(self, note_id: str) -> None:
        self.calls.append(("delete", note_id))
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


class FakeLibrary:
    """Zotero's own service, as much of it as the mirror can tell apart.

    Built to be adversarial rather than convenient, because the two failures
    worth catching are invisible to a helpful fake. It answers newest first, so
    a reader that folds children into parents by response order produces a
    different record on two reads of the same state. And it can be changed
    between two pages of one walk, which is how a paper edited during a whole
    read gets pushed out of the window and then retired for being absent.

    Versions rise the way the service's do: one counter per library, bumped on
    every write, stamped onto whatever was written.
    """

    def __init__(self, libraries: dict[str, str] | None = None) -> None:
        #: library id -> display name
        self.libraries = dict(libraries or {"users/0": "My Library"})
        self.version = {lib: 0 for lib in self.libraries}
        #: library -> key -> raw item, as the service would return it
        self.items_by: dict[str, dict[str, dict]] = {
            lib: {} for lib in self.libraries}
        self.collections_by: dict[str, dict[str, dict]] = {
            lib: {} for lib in self.libraries}
        #: library -> {"items": [...keys], "collections": [...keys]}
        self.gone: dict[str, dict[str, list[str]]] = {
            lib: {"items": [], "collections": []} for lib in self.libraries}
        #: Keys the trash holds. Invisible to every route, exactly as they are
        #: in the real service — which is the whole reason a version can move
        #: with nothing to show for it.
        self.trashed: dict[str, set[str]] = {lib: set() for lib in self.libraries}
        self.calls: list[tuple[str, str, Any]] = []
        #: Called once, before the second page of the next walk.
        self.mid_walk = None

    # -- writing, as a person using Zotero would ----------------------------

    def put(self, key: str, *, library: str = "users/0", parent: str = "",
            item_type: str = "journalArticle", **data: Any) -> dict:
        self.version[library] += 1
        body = {"key": key, "itemType": item_type, **data}
        if parent:
            body["parentItem"] = parent
        raw = {"key": key, "version": self.version[library], "data": body}
        self.items_by[library][key] = raw
        return raw

    def note(self, key: str, html: str, *, library: str = "users/0",
             parent: str = "") -> dict:
        """A child note, which is its own item and carries its own version.

        Adding one to a paper does not move that paper's version, so a delta
        carries the note and not its parent. That is the case a merge has to
        get right.
        """
        return self.put(key, library=library, parent=parent,
                        item_type="note", note=html)

    def put_collection(self, key: str, name: str, *, library: str = "users/0",
                       parent: str = "") -> dict:
        self.version[library] += 1
        raw = {"key": key, "version": self.version[library],
               "data": {"key": key, "name": name, "parentCollection": parent}}
        self.collections_by[library][key] = raw
        return raw

    def trash(self, key: str, *, library: str = "users/0") -> None:
        """What a person does when they remove a paper.

        The version moves and every route stops mentioning the item. Nothing in
        a since-window read or in `deleted` can see it, which is why only a
        whole read retracts it.
        """
        self.version[library] += 1
        self.items_by[library].pop(key, None)
        self.trashed[library].add(key)

    def erase(self, key: str, *, library: str = "users/0") -> None:
        """A permanent removal, which `deleted` does report."""
        self.version[library] += 1
        self.items_by[library].pop(key, None)
        self.gone[library]["items"].append(key)

    def rename_library(self, library: str, name: str) -> None:
        self.libraries[library] = name

    # -- reading, as `source` does ------------------------------------------

    def _window(self, rows: list[dict], since: int | None) -> list[dict]:
        kept = [r for r in rows if since is None or r["version"] > since]
        # Newest first, which is what the service does and what makes an
        # order-dependent reader produce two answers for one state.
        return sorted(kept, key=lambda r: (-r["version"], r["key"]))

    def items(self, library: str | None = None, since: int | None = None):
        library = library or "users/0"
        self.calls.append(("items", library, since))
        rows = self._window(list(self.items_by[library].values()), since)
        if self.mid_walk is not None and len(rows) > 1:
            hook, self.mid_walk = self.mid_walk, None
            hook()
            raise _sheared()
        return _window(rows, self.version[library])

    def collections(self, library: str | None = None, since: int | None = None):
        library = library or "users/0"
        self.calls.append(("collections", library, since))
        rows = self._window(list(self.collections_by[library].values()), since)
        return _window(rows, self.version[library])

    def deleted(self, library: str | None = None, since: int | None = None):
        library = library or "users/0"
        self.calls.append(("deleted", library, since))
        return _window(list(self.gone[library]["items"]),
                       self.version[library])

    def deleted_collections(self, library: str | None = None,
                            since: int | None = None) -> list[str]:
        library = library or "users/0"
        self.calls.append(("deleted_collections", library, since))
        return list(self.gone[library]["collections"])

    def library_version(self, library: str | None = None) -> int:
        library = library or "users/0"
        self.calls.append(("library_version", library, None))
        return self.version[library]

    def all_libraries(self) -> list[dict]:
        self.calls.append(("libraries", "", None))
        return [{"id": lib, "name": name}
                for lib, name in self.libraries.items()]

    def install(self, monkeypatch, module) -> "FakeLibrary":
        """Put this in front of `source` for the module under test."""
        monkeypatch.setattr(module.source, "items", self.items)
        monkeypatch.setattr(module.source, "collections", self.collections)
        monkeypatch.setattr(module.source, "deleted", self.deleted)
        monkeypatch.setattr(module.source, "deleted_collections",
                            self.deleted_collections)
        monkeypatch.setattr(module.source, "library_version",
                            self.library_version)
        monkeypatch.setattr(module.source, "libraries", self.all_libraries)
        return self

    def asked(self, verb: str) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] == verb]


def _window(records, version):
    from awm.zotero.source import Window
    return Window(records=list(records), version=version)


def _sheared():
    from awm.zotero.source import Sheared
    return Sheared("the library moved while it was being read")
