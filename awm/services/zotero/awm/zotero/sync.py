"""Pull the library into the bundle, and apply the bundle to the vault.

Two halves that never call each other, because they need two different
machines: `pull` needs to reach the Zotero desktop, `apply` needs to reach the
vault. On this node both are here; on sirius only the second can be, and the
bundle is what crosses between them.

**Both halves are idempotent, and the first one is usually free.** Zotero's
library carries a version that rises on any change, so a tick whose version has
not moved costs one HTTP request and stops. That is what makes "periodic" cheap
enough to actually be periodic.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import html
import logging
import os
from pathlib import Path
from typing import Any

from awm.config import autocommit

from awm.zotero import bundle as bundle_mod
from awm.zotero import source

log = logging.getLogger("awm.zotero.sync")

#: The scope holding the vault, and so the bundle. The same default the trilium
#: service resolves, spelled again rather than imported: these are separate
#: dists, and a cross-dist import is what the per-service layout exists to stop.
VAULT_SCOPE = Path(os.environ.get("TRILIUM_VAULT_SCOPE")
                   or (Path(os.environ.get("AWM_WORKSPACE",
                                           Path.home() / "agentic_workspace"))
                       / "projects" / "vault" / "main"))

#: Where the library lands in the vault. One note, and everything under it.
LIBRARY_NOTE = os.environ.get("ZOTERO_LIBRARY_NOTE", "Library")

#: The label carrying a Zotero key. It is what makes a note the mirror's to
#: rewrite — a note without one was written by a person and is never touched.
KEY_LABEL = "zoteroKey"
COLLECTION_LABEL = "zoteroCollection"
LIBRARY_LABEL = "zoteroLibraryId"


def bundle(scope: Path | None = None) -> bundle_mod.Bundle:
    return bundle_mod.Bundle(scope or VAULT_SCOPE)


class Busy(RuntimeError):
    """Another sync holds the lock."""


@contextlib.contextmanager
def exclusive(scope: Path | None = None):
    """Hold the mirror's lock for the length of a pull or an apply.

    Two applies running at once each read "what is already in the vault" before
    the other has written it, so both decide the same paper is new and both
    create it. That is not hypothetical: it happened here, and it left 216
    doubled papers in the vault with nothing reporting a problem — every call
    succeeded.

    A file lock rather than an in-process one, because the two callers are two
    processes: the service's timer, and somebody typing `awm zotero sync`. It
    is released by the kernel when the holder dies, so a killed sync does not
    wedge the next one.
    """
    b = bundle(scope)
    b.root.mkdir(parents=True, exist_ok=True)
    path = b.root.parent / ".zotero-sync.lock"
    handle = path.open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise Busy(
                "another zotero sync is running. Two at once each decide the "
                "same paper is new and both create it.") from e
        yield b
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


# -- pull --------------------------------------------------------------------


def pull(scope: Path | None = None, *, force: bool = False,
         commit: bool = True) -> dict[str, Any]:
    """Bring the bundle up to every library: the personal one and each group.

    `force` re-reads a library whose version has not moved. It exists for the
    case the version cannot see: a file finishing its download in Zotero
    changes nothing about the item that records it, so bytes can appear without
    the version rising.

    A library that has not moved is skipped and its previous contents are kept,
    so the ordinary tick reads only what changed — but the write is always the
    whole bundle, because a partial file would describe a library state that
    never existed.
    """
    with exclusive(scope) as b:
        return _pull(b, force=force, commit=commit)


def _pull(b: bundle_mod.Bundle, *, force: bool, commit: bool) -> dict[str, Any]:
    had = b.versions
    previous = b.read()
    out: dict[str, Any] = {"had": had, "path": str(b.root)}

    libraries = source.libraries()
    now = {lib["id"]: source.library_version(lib["id"]) for lib in libraries}
    moved = [lib for lib in libraries if force or now[lib["id"]] != had.get(lib["id"])]
    if not moved:
        out.update({"versions": now, "changed": False,
                    "detail": "no library has moved"})
        return out

    stored = source.stored_files()
    by_library = {lib["id"]: lib["name"] for lib in libraries}
    kept = _kept(previous, {lib["id"] for lib in moved})
    parts = [kept] + [
        bundle_mod.normalize(source.items(lib["id"]),
                             source.collections(lib["id"]),
                             stored, library=lib["id"],
                             library_name=lib["name"])
        for lib in moved]
    payload = b.write(bundle_mod.merge(parts, now))

    wanted = {f"{item['library']}/{key}"
              for item in payload["items"]
              for key in item.get("files", {})}
    missing = sorted(
        (f"{item['library']}/{key}", key, name)
        for item in payload["items"]
        for key, name in item.get("files", {}).items()
        if not b.file_for(f"{item['library']}/{key}", name).is_file())
    fetched = _fetch(b, missing)
    dropped = b.prune_files(wanted)

    out.update({"versions": now, "changed": True,
                "read": [lib["name"] for lib in moved],
                "libraries": {by_library[k]: v for k, v in now.items()},
                "items": len(payload["items"]),
                "collections": len(payload["collections"]),
                "files_fetched": fetched, "files_dropped": dropped})
    if commit:
        out["git"] = _commit(b, "zotero: library at "
                                + ", ".join(f"{by_library[k]} {v}"
                                            for k, v in sorted(now.items())))
    return out


def run(vault, scope: Path | None = None, *, force: bool = False,
        parent: str = "root", commit: bool = True) -> dict[str, Any]:
    """Pull then apply, holding the lock across both.

    Not `pull()` followed by `apply()`: each takes the lock and drops it, so a
    second sync starting in between would apply the bundle this one has just
    rewritten and both would decide the same papers were new. One lock, one
    pass.

    An unchanged library is the ordinary answer, and re-applying an unchanged
    bundle would be thousands of round trips proving nothing — so the apply
    runs only when the pull moved something.
    """
    with exclusive(scope) as b:
        pulled = _pull(b, force=force, commit=commit)
        out: dict[str, Any] = {"pull": pulled}
        if pulled.get("changed"):
            out["apply"] = _apply(vault, b, parent=parent)
        return out


def _kept(previous: dict[str, Any], reread: set[str]) -> dict[str, Any]:
    """What the last bundle said about the libraries this pass did not read.

    A record with no `library` at all is dropped rather than kept. It comes
    from a bundle written before the mirror spanned several libraries, and
    nothing can say which library it belonged to — carrying it forward would
    leave an item in the bundle that no pull can ever refresh or retire.
    """
    def mine(record: dict) -> bool:
        library = record.get("library")
        return bool(library) and library not in reread

    return {
        "collections": [c for c in previous.get("collections") or [] if mine(c)],
        "items": [i for i in previous.get("items") or [] if mine(i)],
    }


def _fetch(b: bundle_mod.Bundle, missing: list[tuple[str, str, str]]) -> int:
    """Copy the stored files the bundle is short of, one library at a time.

    Grouped because the copy is one archive stream per call and Zotero files
    everything under a single flat `storage/`, so the source keys are bare
    while the destination is nested by library.
    """
    got = 0
    by_library: dict[str, list[tuple[str, str]]] = {}
    for ref, key, name in missing:
        by_library.setdefault(ref.rsplit("/", 1)[0], []).append((key, name))
    for library, wanted in by_library.items():
        into = b.files / library
        source.fetch_files([k for k, _ in wanted], into)
        got += len([1 for k, n in wanted if (into / k / n).is_file()])
    return got


def _commit(b: bundle_mod.Bundle, message: str) -> dict[str, Any]:
    """Pin the chunk and commit it.

    The pin is what carries the bytes: `library.json` is small enough for git,
    but the PDFs are not, and DVC is how this workspace holds bytes. Both go in
    one commit so a checkout can never have the metadata of one version and the
    files of another.
    """
    if not (b.scope / ".git").exists():
        return {"committed": False,
                "detail": f"{b.scope} is not a git checkout — bundle written, "
                          f"not committed"}
    sha = autocommit.pin_chunk(b.scope, bundle_mod.CHUNK, "awm", message)
    return {"committed": bool(sha), "rev": sha,
            "detail": None if sha else "nothing changed"}


# -- apply -------------------------------------------------------------------


def _card(item: dict) -> str:
    """A reference as HTML, for a person to read — the labels carry the
    machine-readable copy.

    Everything is escaped, Zotero's own notes included. An abstract is
    arbitrary text out of a publisher's metadata, and a note in this vault runs
    on awm's own origin.
    """
    e = html.escape
    rows = [("Authors", "; ".join(item.get("creators") or [])),
            ("Year", item.get("year", "")),
            ("Published in", item.get("publication", "")),
            ("Type", item.get("item_type", ""))]
    parts = ["<table>"]
    for name, value in rows:
        if value:
            parts.append(f"<tr><th>{e(name)}</th><td>{e(str(value))}</td></tr>")
    if item.get("doi"):
        doi = e(item["doi"])
        parts.append(f'<tr><th>DOI</th><td><a href="https://doi.org/{doi}">'
                     f"{doi}</a></td></tr>")
    if item.get("url"):
        url = e(item["url"])
        parts.append(f'<tr><th>URL</th><td><a href="{url}">{url}</a></td></tr>')
    parts.append("</table>")
    if item.get("abstract"):
        parts.append(f"<p>{e(item['abstract'])}</p>")
    for note in item.get("notes") or []:
        parts.append(f"<blockquote>{e(note)}</blockquote>")
    return "".join(parts)


def _title(item: dict) -> str:
    """What the note is called: author and year in front, because a library is
    read as a list and a bare title sorts by nothing useful."""
    first = (item.get("creators") or [""])[0].split(",")[0].strip()
    stamp = " ".join(p for p in (first, item.get("year", "")) if p)
    title = item.get("title") or "(untitled)"
    return f"{stamp} — {title}" if stamp else title


def _labels(item: dict) -> dict[str, str]:
    """The citation fields, as labels a person can search and a board can group
    by. Empty ones are left off rather than written blank — `#doi` meaning
    "there is no DOI" would make `#doi` useless as a filter."""
    out = {"itemType": item.get("item_type", ""),
           "zoteroLibraryName": item.get("library_name", ""),
           "year": item.get("year", ""),
           "doi": item.get("doi", ""),
           "url": item.get("url", ""),
           "creators": "; ".join(item.get("creators") or [])}
    return {k: v for k, v in out.items() if v}


def apply(vault, scope: Path | None = None, *,
          parent: str = "root") -> dict[str, Any]:
    """Write the bundle into the vault: collections as a tree, one note per
    reference, each stored file attached.

    Matched on `#zoteroKey`. A note somebody writes inside the library carries
    none, so it is invisible to every pass — and an item that has left Zotero
    is removed only if it carries one, which means the mirror can only ever
    delete what the mirror put there.
    """
    with exclusive(scope) as b:
        return _apply(vault, b, parent=parent)


def _apply(vault, b: bundle_mod.Bundle, *, parent: str) -> dict[str, Any]:
    if not b.exists:
        raise FileNotFoundError(
            f"no bundle at {b.root} — run `awm zotero pull` on the node that "
            f"can reach the library")
    library = b.read()
    items = library.get("items") or []

    root = vault.ensure(
        parent=parent, title=LIBRARY_NOTE,
        content="<p>Mirrored from Zotero. Notes here carry "
                "<code>#zoteroKey</code> and are rewritten on every sync; "
                "anything you write without one is left alone.</p>")
    vault.set_label(root, "zoteroLibrary", "1")

    # One subtree per library. Two libraries may both have a collection called
    # "papers", and merging them would put a shared group's reading list inside
    # somebody's personal one with nothing saying it had happened.
    shelves = _ensure_shelves(vault, root, items)
    folders = _ensure_collections(vault, shelves, root,
                                  library.get("collections") or [])
    known, doubled = _collapse(vault, root, KEY_LABEL)

    made = updated = attached = 0
    for item in items:
        home = shelves.get(item.get("library", ""), root)
        note_id, was_new, was_changed = _upsert_item(vault, home, item, known)
        made += was_new
        updated += was_changed
        attached += _attach(vault, b, note_id, item)
        wanted = [folders[c] for c in item.get("collections") or []
                  if c in folders] or [home]
        vault.place(note_id, wanted)

    seen = {i["ref"] for i in items}
    removed = 0
    for ref, note_id in known.items():
        if ref not in seen:
            vault.delete(note_id)
            removed += 1

    return {"library_note": root, "items": len(items), "created": made,
            "updated": updated, "attached": attached, "removed": removed,
            "deduplicated": doubled, "collections": len(folders),
            "libraries": len(shelves), "versions": library.get("versions")}


def _collapse(vault, root: str, label: str) -> tuple[dict[str, str], int]:
    """One note per key, deleting any second copy of one the mirror owns.

    Taking the first and ignoring the rest leaves a doubled paper in the vault
    for ever, because every later pass makes the same choice and never looks at
    the other. The oldest is kept: it is the one a person may already have
    linked to.
    """
    every = vault.owned_all(root, label)
    doubled = 0
    for extras in every.values():
        for note_id in extras[1:]:
            vault.delete(note_id)
            doubled += 1
    return {key: ids[0] for key, ids in every.items()}, doubled


def _ensure_shelves(vault, root: str, items: list[dict]) -> dict[str, str]:
    """One note per library, named as Zotero names it."""
    named = {}
    for item in items:
        if item.get("library"):
            named.setdefault(item["library"], item.get("library_name")
                             or item["library"])
    out = {}
    for library, name in sorted(named.items(), key=lambda kv: kv[1]):
        note_id = vault.ensure(parent=root, title=name, content="")
        vault.set_label(note_id, LIBRARY_LABEL, library)
        out[library] = note_id
    return out


def _ensure_collections(vault, shelves: dict[str, str], root: str,
                        tree: list[dict]) -> dict[str, str]:
    """Zotero's collection tree as notes, parents before children.

    Ordered by depth rather than recursed, because Zotero returns collections
    in no particular order and a child made before its parent would land at the
    top of the library and stay there.
    """
    by_ref = {c["ref"]: c for c in tree}

    def depth(ref: str, guard: int = 0) -> int:
        parent = by_ref.get(ref, {}).get("parent") or ""
        if not parent or parent not in by_ref or guard > 50:
            return 0
        return 1 + depth(parent, guard + 1)

    known, _ = _collapse(vault, root, COLLECTION_LABEL)
    made: dict[str, str] = {}
    for c in sorted(tree, key=lambda c: (depth(c["ref"]), c["name"])):
        shelf = shelves.get(c.get("library", ""), root)
        under = made.get(c["parent"], shelf) if c["parent"] else shelf
        note_id = known.get(c["ref"])
        if note_id is None:
            note_id = vault.create(parent=under, title=c["name"], type="book",
                                   labels={COLLECTION_LABEL: c["ref"]})
        else:
            vault.update(note_id, title=c["name"])
            vault.place(note_id, [under])
        made[c["ref"]] = note_id
    return made


def _upsert_item(vault, root: str, item: dict,
                 known: dict[str, str]) -> tuple[str, int, int]:
    title, body, labels = _title(item), _card(item), _labels(item)
    note_id = known.get(item["ref"])
    if note_id is None:
        return (vault.create(parent=root, title=title, content=body,
                             labels={KEY_LABEL: item["ref"], **labels}), 1, 0)
    changed = vault.update(note_id, title=title, content=body, labels=labels)
    return note_id, 0, int(changed)


def _attach(vault, b: bundle_mod.Bundle, note_id: str, item: dict) -> int:
    attached = 0
    for key, name in (item.get("files") or {}).items():
        path = b.file_for(f"{item['library']}/{key}", name)
        if path.is_file() and vault.attach(note_id, path, title=name):
            attached += 1
    return attached
