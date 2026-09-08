"""The mirrored library on disk: what `pull` writes and `apply` reads.

Two jobs sit either side of this module, and putting a file between them is
what makes the mirror portable. `pull` needs the machine the Zotero desktop is
on. `apply` needs the machine the vault is on. Today those are two different
machines, and on sirius they can never be the same one. A bundle is the thing
that travels: versioned by the commit that versions it, pinned by DVC like any
other data in this workspace.

It does not travel by merging a branch. Two hosts' vaults are separate
repositories with unrelated histories, the vault declares no DVC remote, and
`data/.gitignore` excludes this chunk, so nothing carries these bytes but the
`ship` verb — an rsync into the far node's own vault scope.

**Shape.**

```
data/zotero/
  library.json          the normalized items and collections, plus the version
  files/<key>/<name>    the stored attachments, exactly as Zotero filed them
```

`library.json` carries `versions`, one `Last-Modified-Version` per library.
Those numbers are the sync cursor, and keeping them *in the bundle* rather than
in a service database is deliberate: a node that receives the bundle knows what
it holds without being told, and a node that loses its service state has not
lost its place.

**One bundle, several libraries.** Zotero's personal library and each shared
group are separate libraries with separate version counters, and a key is only
unique within one — two libraries may both hold an item `ABCD1234`. So an item
is identified here by `<library>/<key>`, and the stored files are filed under
the same compound name. Flattening them onto the bare key looks harmless and
would silently make one paper overwrite another.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

#: The chunk, relative to the vault scope. `data/` is what DVC pins in this
#: workspace, and the whole point is that the bytes live in the shared cache
#: rather than in git.
CHUNK = "data/zotero"

LIBRARY_JSON = "library.json"
FILES_DIR = "files"

#: Item types that are neither a reference nor a file: Zotero's own notes and
#: annotations, which belong to the item they hang off rather than to the
#: bibliography.
NOT_A_REFERENCE = {"attachment", "note", "annotation"}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def creators(data: dict) -> list[str]:
    """Author names as a person writes them, not as Zotero stores them.

    An institutional author has a `name` and no surname, which is why this is
    not a one-line join: dropping it would silently lose the author of every
    standards document and government report in the library.
    """
    out = []
    for c in data.get("creators") or []:
        if c.get("name"):
            out.append(c["name"])
            continue
        parts = [c.get("lastName") or "", c.get("firstName") or ""]
        joined = ", ".join(p for p in parts if p)
        if joined:
            out.append(joined)
    return out


def year(data: dict) -> str:
    """The four digits out of a date Zotero does not normalize.

    `parsedDate` is there when Zotero managed it and absent when it did not, so
    the raw `date` field is scanned as the fallback — it holds everything from
    `2019` to `Submitted 3 March 2019`.
    """
    parsed = data.get("parsedDate") or ""
    if len(parsed) >= 4 and parsed[:4].isdigit():
        return parsed[:4]
    raw = str(data.get("date") or "")
    for i in range(len(raw) - 3):
        chunk = raw[i:i + 4]
        if chunk.isdigit() and chunk.startswith(("19", "20")):
            return chunk
    return ""


@dataclass
class Item:
    """One reference, with whatever hangs off it flattened in."""

    key: str
    library: str
    library_name: str
    version: int
    item_type: str
    title: str
    creators: list[str] = field(default_factory=list)
    year: str = ""
    doi: str = ""
    url: str = ""
    publication: str = ""
    abstract: str = ""
    tags: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    #: attachment key -> filename, for the ones whose bytes are on disk.
    files: dict[str, str] = field(default_factory=dict)
    #: Zotero's own child notes, as HTML.
    notes: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        """`<library>/<key>`: unique across libraries, where a bare key is
        not."""
        return f"{self.library}/{self.key}"

    def as_json(self) -> dict:
        out = {k: v for k, v in self.__dict__.items() if v not in ("", [], {})}
        out["ref"] = self.ref
        return out


def normalize(items: Iterable[dict], collections: Iterable[dict],
              stored: dict[str, str], *, library: str = "users/0",
              library_name: str = "My Library") -> dict[str, Any]:
    """Fold Zotero's flat item list into references with their files attached.

    Zotero returns references, attachments and notes as siblings, related only
    by `parentItem`. The vault wants one note per reference with its PDF on it,
    so the children are folded into their parents here — once, in the bundle,
    rather than by every consumer.

    An attachment whose bytes are not in `stored` is dropped rather than
    recorded: `filename` is set on an attachment Zotero has only ever seen the
    metadata for, and a bundle promising a file it does not hold makes `apply`
    fail on data rather than on a mistake.
    """
    refs: dict[str, Item] = {}
    children: list[dict] = []

    for raw in items:
        data = raw.get("data") or {}
        key, kind = data.get("key"), data.get("itemType")
        if not key or not kind:
            continue
        if kind in NOT_A_REFERENCE:
            children.append(data)
            continue
        refs[key] = Item(
            key=key, library=library, library_name=library_name,
            version=int(raw.get("version") or 0), item_type=kind,
            title=(data.get("title") or "").strip() or "(untitled)",
            creators=creators(data), year=year(data),
            doi=(data.get("DOI") or "").strip(),
            url=(data.get("url") or "").strip(),
            publication=(data.get("publicationTitle")
                         or data.get("bookTitle")
                         or data.get("proceedingsTitle") or "").strip(),
            abstract=(data.get("abstractNote") or "").strip(),
            tags=[t["tag"] for t in data.get("tags") or [] if t.get("tag")],
            collections=list(data.get("collections") or []))

    for data in children:
        parent = refs.get(data.get("parentItem") or "")
        if parent is None:
            continue
        if data.get("itemType") == "attachment":
            name = stored.get(data["key"])
            if name:
                parent.files[data["key"]] = name
        elif data.get("itemType") == "note" and data.get("note"):
            parent.notes.append(data["note"])

    tree = [{"key": c["data"]["key"],
             "ref": f"{library}/{c['data']['key']}",
             "library": library,
             "name": c["data"].get("name") or "(unnamed)",
             "parent": (f"{library}/{c['data']['parentCollection']}"
                        if c["data"].get("parentCollection") else "")}
            for c in collections if (c.get("data") or {}).get("key")]

    for item in refs.values():
        item.collections = [f"{library}/{k}" for k in item.collections]

    return {"collections": tree, "items": [i.as_json() for i in refs.values()]}


def merge(parts: list[dict[str, Any]], versions: dict[str, int]) -> dict[str, Any]:
    """One library payload per library, folded into the bundle's shape.

    Ordered by `ref`, because Zotero does not answer in a stable order and two
    reads of an unchanged library came back as two different files. Sorting
    keys alone does not fix that — these are lists. What depends on it: the
    bundle's digest, which is how a node that only applies decides it has
    nothing to do. An order-sensitive digest makes every pull look like a
    changed library and re-walks the whole mirror to prove it was not.
    """
    return {"pulled": _stamp(), "versions": dict(versions),
            "collections": sorted((c for p in parts for c in p["collections"]),
                                  key=lambda c: c["ref"]),
            "items": sorted((i for p in parts for i in p["items"]),
                            key=lambda i: i["ref"])}


class Bundle:
    """The mirror as it sits in the vault scope."""

    def __init__(self, scope: Path) -> None:
        self.scope = Path(scope)
        self.root = self.scope / CHUNK
        self.library_json = self.root / LIBRARY_JSON
        self.files = self.root / FILES_DIR

    @property
    def exists(self) -> bool:
        return self.library_json.is_file()

    def read(self) -> dict[str, Any]:
        if not self.exists:
            return {"versions": {}, "items": [], "collections": []}
        return json.loads(self.library_json.read_text("utf-8"))

    @property
    def versions(self) -> dict[str, int]:
        """Where the mirror is, per library, which is also where the next pull
        starts."""
        return {k: int(v) for k, v in (self.read().get("versions") or {}).items()}

    @property
    def digest(self) -> str:
        """A fingerprint of the library this bundle holds.

        Over the library's content and not over the file, because `pulled` is a
        timestamp that moves whenever somebody looks at Zotero. Digesting the
        file would make a node re-apply the whole mirror — thousands of round
        trips — to prove that nothing had changed.
        """
        library = self.read()
        blob = json.dumps({k: library.get(k)
                           for k in ("versions", "collections", "items")},
                          sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]

    def write(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Replace `library.json`, never write into it.

        Once a pull has been pinned, this file is a read-only hardlink into the
        shared DVC cache. Writing in place fails outright with `Permission
        denied` — and if the mode ever allowed it, the bytes would land inside
        the cache object itself and corrupt it for every other scope and every
        commit that pins it. Renaming a new file over the link breaks the link
        and leaves the cached object alone.

        It is also what makes the file safe to `rsync` while a pass is running:
        a reader sees the whole old bundle or the whole new one.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        # Sorted and indented so the diff is the diff in the library, not in
        # whatever order the API happened to answer in.
        blob = json.dumps(payload, indent=2, sort_keys=True,
                          ensure_ascii=False) + "\n"
        staged = self.library_json.with_name(self.library_json.name + ".new")
        staged.write_text(blob, "utf-8")
        os.replace(staged, self.library_json)
        return payload

    def file_for(self, ref: str, name: str) -> Path:
        """Where a stored file lives in the bundle.

        Under `<library>/<key>/`, mirroring how an item is identified: Zotero
        files everything under one flat `storage/<key>/` and relies on keys not
        colliding, which holds inside a library and not between them.
        """
        return self.files / ref / name

    def prune_files(self, keep: set[str]) -> int:
        """Drop stored files for attachments the library no longer has.

        The mirror is a mirror. Keeping the bytes of a paper somebody deleted
        would make the DVC cache grow forever and the bundle stop describing
        the library it is named after.

        Walked top-down against the wanted paths rather than at a fixed depth,
        because a ref is `<library>/<key>` and a library id is itself two
        segments (`users/0`, `groups/5284390`). Assuming one directory level
        per library made `users/0` look like an unwanted key and deleted every
        file under it — right after they were fetched, so the pull reported
        copying them and the disk held none.
        """
        if not self.files.is_dir():
            return 0
        wanted = {self.files / ref for ref in keep}
        prefixes = {parent for ref in wanted for parent in ref.parents}
        gone = 0

        def walk(directory: Path) -> None:
            nonlocal gone
            for child in sorted(directory.iterdir()):
                if not child.is_dir():
                    continue
                if child in wanted:
                    continue
                if child in prefixes:
                    walk(child)
                    continue
                shutil.rmtree(child, ignore_errors=True)
                gone += 1

        walk(self.files)
        for directory in sorted(self.files.rglob("*"), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        return gone

    def stats(self) -> dict[str, Any]:
        library = self.read()
        items = library.get("items") or []
        return {
            "versions": library.get("versions") or {},
            "pulled": library.get("pulled"),
            "items": len(items),
            "libraries": sorted({i.get("library_name", "") for i in items}),
            "collections": len(library.get("collections") or []),
            "with_files": len([i for i in items if i.get("files")]),
            "files": sum(1 for f in self.files.rglob("*") if f.is_file())
                     if self.files.is_dir() else 0,
            "bytes": sum(f.stat().st_size for f in self.files.rglob("*")
                         if f.is_file()) if self.files.is_dir() else 0,
            "path": str(self.root),
        }
