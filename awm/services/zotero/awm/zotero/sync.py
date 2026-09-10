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
import hashlib
import html
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
                       / "projects" / "trilium" / "release"))

#: Where the library lands in the vault. One note, and everything under it.
LIBRARY_NOTE = os.environ.get("ZOTERO_LIBRARY_NOTE", "Library")

#: The label carrying a Zotero key. It is what makes a note the mirror's to
#: rewrite — a note without one was written by a person and is never touched.
KEY_LABEL = "zoteroKey"
COLLECTION_LABEL = "zoteroCollection"
LIBRARY_LABEL = "zoteroLibraryId"

#: The label that names the note the library lives under. Set it from the
#: Trilium UI and the mirror moves there; it is the whole of the "where does
#: this go" setting.
ROOT_LABEL = "zoteroLibrary"

#: The bundle digest the root note was last written from. It lives on the root
#: rather than in a sidecar because it describes what the *vault* holds, and
#: because root resolution reads the note's labels anyway — so an apply-only
#: tick with nothing to do costs one search instead of thousands of calls.
APPLIED_LABEL = "zoteroApplied"

#: What a note was last written from. The per-item cursor, and the reason
#: landing one new paper no longer rewrites the other eight hundred.
#:
#: **Not Zotero's own item version.** A stored file finishing its download
#: changes what the note should say without moving the version of the item that
#: records it — which is exactly why `pull` grew a `force`. A version cursor
#: would skip that paper's file for ever. This digests the whole bundle record
#: instead, so it moves when the version moves, when a file appears, when
#: collection membership changes, and when the library is renamed.
STAMP_LABEL = "zoteroStamp"

#: Bumped by hand whenever `_title`, `_card` or `_labels` change what a note
#: looks like.
#:
#: **CAUTION** Forgetting is silent and total. Without it, changing how a note
#: is rendered leaves every note in the vault carrying a stamp that says it is
#: current, and only a forced pass ever notices.
RENDER = "1"

#: Zotero's own version for the item, written for a person to read and for the
#: push path to order two updates by. Never the skip decision — see
#: `STAMP_LABEL` for why it cannot be.
VERSION_LABEL = "zoteroVersion"

#: The note that says what the mirror last did, and the label on the root that
#: points at it. A stalled mirror and an idle one are otherwise identical from
#: inside the vault, which is how this one ran dead for days unnoticed.
STATUS_LABEL = "zoteroStatus"
STATUS_POINTER = "zoteroStatusNote"
STATUS_NOTE = "Zotero mirror"


def bundle(scope: Path | None = None) -> bundle_mod.Bundle:
    return bundle_mod.Bundle(scope or VAULT_SCOPE)


class Busy(RuntimeError):
    """Another sync holds the lock."""


class Unreachable(RuntimeError):
    """The node holding the vault could not be reached.

    Ordinary rather than exceptional: the far node reboots, the link drops.
    A ship that fails leaves the far node serving the last good mirror.
    """


class RootError(RuntimeError):
    """The mirror cannot say which note the library belongs under."""


class NoRoot(RootError):
    """No note carries the label, and this node may not create one."""


class Ambiguous(RootError):
    """More than one note carries the label."""


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
        parent: str = "root", commit: bool = True,
        may_create: bool = True, apply_here: bool = True,
        ship_to: str = "") -> dict[str, Any]:
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
        if pulled.get("changed") and apply_here:
            out["apply"] = _apply(vault, b, parent=parent,
                                  may_create=may_create, force=force)
        if pulled.get("changed") and ship_to:
            host, remote_scope = destination_parts(ship_to)
            try:
                out["ship"] = _ship(b, host, remote_scope)
            except Unreachable as e:
                # Reported, not raised. The far node being asleep is ordinary,
                # the same way an unreachable library already is, and it leaves
                # that node serving the last good mirror rather than none.
                out["ship"] = {"shipped": False, "detail": str(e)[:300]}
                log.info("zotero: could not ship to %s: %s", host, e)
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


# -- ship --------------------------------------------------------------------


#: Long enough for a slow link, short enough that a wedged ssh does not hold
#: the mirror's lock through the next tick.
SHIP_TIMEOUT_S = 600.0


def _run(argv: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
    """One place for the subprocess call, so a test can watch the command line
    without a network."""
    return subprocess.run(argv, input=stdin, capture_output=True, text=True,
                          timeout=SHIP_TIMEOUT_S, check=False)


def _rsync_argv(source_path: Path, host: str, remote_dir: str) -> list[str]:
    """The command that carries the library.

    `--rsync-path` is the house idiom for this pair of nodes: ssh lands as an
    unprivileged user who cannot read `/var/lib/awm` at all, so the *remote*
    rsync is the thing that has to run as the service account.

    `--chmod` is not cosmetic. `library.json` is a read-only hardlink into the
    DVC cache, and `-a` would faithfully carry mode 444 across — leaving the
    far node a bundle its own pull could never replace.
    """
    return ["rsync", "-a", "--chmod=F644",
            "--rsync-path=sudo -n -u awm rsync",
            str(source_path), f"{host}:{remote_dir}/"]


def destination_parts(destination: str) -> tuple[str, str]:
    """`host:/path/to/vault/scope`, split and checked.

    Checked at the seam rather than at the rsync, because a destination with no
    path silently becomes the far node's filesystem root.
    """
    host, _, remote_scope = destination.partition(":")
    remote_scope = remote_scope.rstrip("/")
    if not host or not remote_scope.startswith("/"):
        raise ValueError(
            f"ship destination {destination!r} is not host:/path — it names "
            f"the vault scope on the far node, e.g. "
            f"sirius:/var/lib/awm/projects/trilium/release")
    return host, remote_scope


def ship(destination: str, scope: Path | None = None) -> dict[str, Any]:
    """Carry `library.json` to the node that holds the vault.

    The third leg the service always implied: pull on the node with the
    library, ship, apply on the node with the vault. It is a verb rather than a
    script so that it takes the same lock the pull half does: a ship that ran
    while a pull was mid-pass would carry a bundle whose stored files had been
    pruned for a library the JSON no longer describes.

    Only the JSON travels. The stored files are two orders of magnitude larger
    and the apply side already skips a file the bundle does not hold, so a node
    that receives only this gets a complete mirror without the PDFs.

    `destination` is `host:/path/to/vault/scope` — the far node's vault scope,
    which is where that node's own bundle reader already looks. Spelled in full
    rather than derived, because a far node's workspace root is not this one's
    and a guess that is wrong writes a library somewhere nobody reads.
    """
    host, remote_scope = destination_parts(destination)
    with exclusive(scope) as b:
        return _ship(b, host, remote_scope)


def _ship(b: bundle_mod.Bundle, host: str, remote_scope: str) -> dict[str, Any]:
    if not b.exists:
        raise FileNotFoundError(f"no bundle at {b.root} — nothing to ship")
    remote_dir = f"{remote_scope}/{bundle_mod.CHUNK}"
    # A shell fed on stdin, never an argument string: `sudo -u` runs one
    # command, so a `&&` written as an argument would be interpreted by the
    # calling user's shell and silently run under the wrong identity.
    made = _run(["ssh", host, "sudo -n -u awm bash -s"],
                stdin=f"test -d {remote_scope} && mkdir -p {remote_dir}\n")
    if made.returncode != 0:
        raise Unreachable(
            f"{host}:{remote_scope} is not a vault scope this node can reach: "
            f"{(made.stderr or made.stdout).strip()[:300]}")
    sent = _run(_rsync_argv(b.library_json, host, remote_dir))
    if sent.returncode != 0:
        raise Unreachable(
            f"rsync to {host} failed: "
            f"{(sent.stderr or sent.stdout).strip()[:300]}")
    return {"shipped": True, "host": host, "remote": remote_dir,
            "digest": b.digest, "bytes": b.library_json.stat().st_size}


# -- apply -------------------------------------------------------------------


def _followable(url: str) -> bool:
    """Whether a publisher's URL may become an `href`.

    Escaping makes the metadata safe as *text*; it does nothing to an href, and
    these notes run on awm's own origin. So the scheme is checked rather than
    the characters: `javascript:` and `data:` render as text instead.

    Protocol-relative is allowed deliberately. The library holds one such URL,
    and an http/https-only guard would silently unlink it.
    """
    u = url.strip()
    if u.startswith("//"):
        return True
    return u.lower().startswith(("http://", "https://"))


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
                     f"https://doi.org/{doi}</a></td></tr>")
    if item.get("url"):
        url = e(item["url"])
        if _followable(item["url"]):
            parts.append(f'<tr><th>URL</th><td>'
                         f'<a href="{url}">{url}</a></td></tr>')
        else:
            parts.append(f"<tr><th>URL</th><td>{url}</td></tr>")
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
           # Not `#journal`: the same field carries a book or a proceedings
           # title, and naming those a journal would be wrong.
           "publication": item.get("publication", ""),
           "doi": item.get("doi", ""),
           "url": item.get("url", ""),
           "creators": "; ".join(item.get("creators") or [])}
    return {k: v for k, v in out.items() if v}


def _labelled_root(vault, *, may_create: bool) -> dict[str, Any] | None:
    """The note somebody labelled, or `None` if there is none to find.

    Refusing on two labelled notes is not fastidiousness. The removal pass is
    scoped to the resolved root, so a root that alternated between passes would
    build the whole library under one, then build it again under the other and
    delete the first — a churn storm with a revision on every note and no error
    anywhere.
    """
    hits = vault.labelled(ROOT_LABEL)
    if len(hits) > 1:
        raise Ambiguous(
            f"{len(hits)} notes carry #{ROOT_LABEL} "
            f"({', '.join(h['note_id'] for h in hits)}) — the mirror needs "
            f"exactly one; remove the label from all but the one you want")
    if hits:
        return hits[0]
    if not may_create:
        raise NoRoot(
            f"no note carries #{ROOT_LABEL}, and this node may not create the "
            f"library — add the label to the note you want the library under, "
            f"from the Trilium UI")
    return None


def _resolve_root(vault, *, parent: str,
                  may_create: bool) -> tuple[str, str, dict[str, str]]:
    """The note the library lives under, and where that answer came from.

    A note somebody labelled `#zoteroLibrary` wins, so the destination is a
    setting in the Trilium UI rather than a constant here.
    """
    hit = _labelled_root(vault, may_create=may_create)
    if hit is not None:
        # Neither `ensure` nor `set_label` is called on this branch, and that
        # is the whole of its correctness. `ensure` resolves through
        # `note_upsert`, which replaces the body of a title match and would
        # erase whatever the person wrote in the note they chose; `set_label`
        # patches the value, silently turning a bare `#zoteroLibrary` typed in
        # the UI into `#zoteroLibrary=1`. The note is taken exactly as it is.
        return hit["note_id"], "label", hit["labels"]
    root = vault.ensure(
        parent=parent, title=LIBRARY_NOTE,
        content="<p>Mirrored from Zotero. Notes here carry "
                "<code>#zoteroKey</code> and are rewritten on every sync; "
                "anything you write without one is left alone.</p>")
    vault.set_label(root, ROOT_LABEL, "1")
    return root, "title", {ROOT_LABEL: "1"}


def apply(vault, scope: Path | None = None, *,
          parent: str = "root", may_create: bool = True,
          force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Write the bundle into the vault: collections as a tree, one note per
    reference, each stored file attached.

    Matched on `#zoteroKey`. A note somebody writes inside the library carries
    none, so it is invisible to every pass — and an item that has left Zotero
    is removed only if it carries one, which means the mirror can only ever
    delete what the mirror put there.

    A pass whose bundle digest already sits on the root does nothing. The
    tradeoff is deliberate and is a change from how this used to behave: a
    mirror note somebody hand-edited is no longer repaired until the library
    itself moves. `force` is the way to repair it.
    """
    with exclusive(scope) as b:
        if dry_run:
            return _plan(vault, b, parent=parent, may_create=may_create)
        return _apply(vault, b, parent=parent, may_create=may_create,
                      force=force)


def _stamp(item: dict) -> str:
    """A fingerprint of everything the note is rendered from.

    Over the whole bundle record, because the record is the only input: title,
    card and labels are all functions of it. So the fingerprint is a strict
    superset of every narrower cursor, and the awkward cases — a file that
    arrives after the item stopped changing, a paper filed into a new
    collection — need no special handling at all.

    Prefixed by `RENDER` so that changing the renderer invalidates every stamp
    at once. A digest alone cannot see that the code around it moved.
    """
    blob = json.dumps(item, sort_keys=True, ensure_ascii=False)
    return f"{RENDER}:{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Survey:
    """What the vault already holds, read once, before anything is written.

    Three searches and no walk. Each one already carries the whole of what it
    found — every note's own labels and its current parents — so the comparisons
    the pass used to make by asking are made here instead, for nothing.
    """

    root: str
    root_from: str
    root_labels: dict[str, str] = field(default_factory=dict)
    #: `#zoteroKey` value -> the notes carrying it.
    items: dict[str, list[dict]] = field(default_factory=dict)
    folders: dict[str, list[dict]] = field(default_factory=dict)
    shelves: dict[str, list[dict]] = field(default_factory=dict)


def _survey(vault, root: str, root_from: str,
            root_labels: dict[str, str]) -> Survey:
    return Survey(root=root, root_from=root_from, root_labels=root_labels,
                  items=vault.scan(root, KEY_LABEL),
                  folders=vault.scan(root, COLLECTION_LABEL),
                  shelves=vault.scan(root, LIBRARY_LABEL))


def _first(found: dict[str, list[dict]]) -> dict[str, dict]:
    """One note per value, taking any duplicate as read rather than removing it.

    What the push path uses. It sees part of the library, and a second note for
    a key is indistinguishable from a note it simply was not told about, so
    removing one is not its call to make.
    """
    return {value: notes[0] for value, notes in found.items() if notes}


def _collapse(vault, found: dict[str, list[dict]]) -> tuple[dict[str, dict], int]:
    """One note per value, deleting any second copy of one the mirror owns.

    Taking the first and ignoring the rest leaves a doubled paper in the vault
    for ever, because every later pass makes the same choice and never looks at
    the other.

    **A duplicate is invisible to the stamp.** The second copy carries the same
    current fingerprint as the first, so this cannot be folded into the skip and
    has to keep looking at everything the scan found.
    """
    doubled = 0
    for notes in found.values():
        for extra in notes[1:]:
            vault.delete(extra["note_id"])
            doubled += 1
    return _first(found), doubled


def _ensure_shelves(vault, survey: Survey, known: dict[str, dict],
                    items: list[dict]) -> tuple[dict[str, str], int]:
    """One note per library, named as Zotero names it.

    Found by its label, not by its title. Resolving a title under the root
    walked every child of the root on every pass, once per library — and
    renaming a shelf in the interface made the next pass create a second one
    beside it, while the first kept the label and the papers.
    """
    named: dict[str, str] = {}
    for item in items:
        if item.get("library"):
            named.setdefault(item["library"],
                             item.get("library_name") or item["library"])
    out: dict[str, str] = {}
    made = 0
    for library, name in sorted(named.items(), key=lambda kv: kv[1]):
        have = known.get(library)
        if have is None:
            out[library] = vault.create(parent=survey.root, title=name,
                                        type="book",
                                        labels={LIBRARY_LABEL: library})
            made += 1
            continue
        note_id = have["note_id"]
        if have["title"] != name:
            vault.update(note_id, title=name)
        if sorted(set(have["parents"])) != [survey.root]:
            vault.place(note_id, [survey.root])
        out[library] = note_id
    return out, made


def _ensure_collections(vault, survey: Survey, known: dict[str, dict],
                        shelves: dict[str, str],
                        tree: list[dict]) -> tuple[dict[str, str], int, int]:
    """Zotero's collection tree as notes, parents before children.

    Ordered by depth rather than recursed, because Zotero returns collections
    in no particular order and a child made before its parent would land at the
    top of the library and stay there.

    Title and placement are compared against what the scan already reported, so
    a tree that has not moved costs nothing. That is also why a renamed or moved
    collection needs no cursor of its own: the papers inside it keep the same
    parent note, so none of them is touched.
    """
    by_ref = {c["ref"]: c for c in tree}

    def depth(ref: str, guard: int = 0) -> int:
        parent = by_ref.get(ref, {}).get("parent") or ""
        if not parent or parent not in by_ref or guard > 50:
            return 0
        return 1 + depth(parent, guard + 1)

    made: dict[str, str] = {}
    created = moved = 0
    for c in sorted(tree, key=lambda c: (depth(c["ref"]), c["name"])):
        shelf = shelves.get(c.get("library", ""), survey.root)
        under = made.get(c["parent"], shelf) if c["parent"] else shelf
        have = known.get(c["ref"])
        if have is None:
            note_id = vault.create(parent=under, title=c["name"], type="book",
                                   labels={COLLECTION_LABEL: c["ref"]})
            created += 1
        else:
            note_id = have["note_id"]
            if have["title"] != c["name"]:
                vault.update(note_id, title=c["name"])
            if sorted(set(have["parents"])) != [under]:
                vault.place(note_id, [under])
                moved += 1
        made[c["ref"]] = note_id
    return made, created, moved


def _upsert(vault, b: bundle_mod.Bundle, survey: Survey,
            known: dict[str, dict], items: list[dict],
            collections: list[dict], *,
            force: bool = False) -> tuple[dict[str, Any], dict[str, str]]:
    """Make these papers and these collections true, and delete nothing.

    One code path for both callers: the reconcile pass hands it the whole
    library, the push path hands it what just changed. **Absence means nothing
    here.** A caller holding part of the library must not be able to conclude
    anything from a paper it was not given, so every removal lives in `_retire`
    and only the caller that can see everything runs it.
    """
    shelves, shelves_made = _ensure_shelves(vault, survey, _first(survey.shelves),
                                            items)
    folders, folders_made, folders_moved = _ensure_collections(
        vault, survey, _first(survey.folders), shelves, collections)

    # `unchanged`, not `skipped`: the root's digest guard already answers
    # "this whole pass had nothing to do" under that name, and one word meaning
    # two things in one result is how a reader draws the wrong conclusion.
    out: dict[str, Any] = {"created": 0, "updated": 0, "unchanged": 0,
                           "replaced": 0, "attached": 0}
    #: The paper worth linking to from the status note: the newest thing this
    #: pass made, falling back to the last thing it changed.
    newest: tuple[int, str, str] | None = None
    touched_last: tuple[str, str] | None = None
    for item in items:
        home = shelves.get(item.get("library", ""), survey.root)
        wanted = sorted({folders[c] for c in item.get("collections") or []
                         if c in folders} or {home})
        have = known.get(item["ref"])
        stamp = _stamp(item)
        fresh = (have is not None and not force
                 and have["labels"].get(STAMP_LABEL) == stamp)

        if have is None:
            # Created under the parent it belongs to, rather than created and
            # then moved. On a first pass that is one fewer branch write and
            # one fewer note read per paper.
            note_id = vault.create(
                parent=wanted[0], title=_title(item), content=_card(item),
                labels={KEY_LABEL: item["ref"],
                        VERSION_LABEL: str(item.get("version") or 0),
                        **_labels(item)})
            out["created"] += 1
            placed = [wanted[0]]
            rank = (int(item.get("version") or 0), note_id, _title(item))
            if newest is None or rank[0] >= newest[0]:
                newest = rank
        else:
            note_id = have["note_id"]
            placed = sorted(set(have["parents"]))
            if fresh:
                out["unchanged"] += 1
            else:
                vault.update(
                    note_id, title=_title(item), content=_card(item),
                    labels={VERSION_LABEL: str(item.get("version") or 0),
                            **_labels(item)})
                out["updated"] += 1
                touched_last = (note_id, _title(item))

        if not fresh:
            out["attached"] += _attach(vault, b, note_id, item)

        # Placement is checked on every pass whatever the stamp says, because
        # the scan already reported where the note is and comparing costs
        # nothing. It is also what puts back a paper somebody dragged in the
        # interface, which no cursor would ever notice.
        if placed != wanted:
            vault.place(note_id, wanted)
            if have is not None:
                out["replaced"] += 1

        if not fresh:
            # Last, after the file and after the placement. A stamp written with
            # the content would mark a paper whose file never arrived, or whose
            # placement failed, as current — and nothing revisits it.
            vault.set_label(note_id, STAMP_LABEL, stamp)

    out.update({"collections": len(folders), "collections_created": folders_made,
                "collections_moved": folders_moved,
                "libraries": len(shelves), "libraries_created": shelves_made,
                "newest": ({"note_id": newest[1], "title": newest[2]}
                           if newest else
                           {"note_id": touched_last[0], "title": touched_last[1]}
                           if touched_last else None)})
    return out, folders


#: What counts as having done something. A pass that changed nothing writes no
#: status, so a quiet mirror puts no revision on the note and costs no call.
DID_SOMETHING = ("created", "updated", "replaced", "attached", "removed",
                 "deduplicated", "collections_created", "collections_moved",
                 "libraries_created")


def _status_note(vault, survey: Survey) -> tuple[str, bool]:
    """The note the mirror writes its own state into, made if there is none.

    Found by a pointer label on the root, which `_labelled_root` has already
    read — so on the ordinary pass this costs nothing at all. Only a vault that
    has never had one, or has lost it, pays a search.

    **Created, never ensured.** `ensure` resolves a title under the root and
    would adopt a note somebody happened to call the same thing, which is the
    trap the shelf step already documents. The pointer is the identity; the
    title is decoration and carries a timestamp precisely because a changed
    title is the cheapest thing a person watching the tree notices.
    """
    pointed = survey.root_labels.get(STATUS_POINTER) or ""
    if pointed:
        return pointed, False
    hits = [h for h in vault.labelled(STATUS_LABEL)]
    if hits:
        return hits[0]["note_id"], True
    return vault.create(parent=survey.root, title=STATUS_NOTE, type="text",
                        labels={STATUS_LABEL: "1"}), True


def _status_body(out: dict[str, Any], versions: Any) -> str:
    e = html.escape
    counts = [(name, out.get(name)) for name in
              ("created", "updated", "unchanged", "replaced", "attached",
               "removed", "deduplicated")]
    rows = [f"<tr><th>{e(n)}</th><td>{e(str(v))}</td></tr>"
            for n, v in counts if v]
    parts = [f"<p>Last change {e(_now())} · "
             f"from {e(str(out.get('source') or 'a reconcile'))}.</p>",
             "<table>", *rows, "</table>"]
    if versions:
        parts.append("<p>Library versions: "
                     + e(", ".join(f"{k} {v}" for k, v in
                                   sorted((versions or {}).items())))
                     + "</p>")
    newest = out.get("newest")
    if newest and newest.get("note_id"):
        parts.append(f'<p>Newest: <a href="#root/{e(newest["note_id"])}">'
                     f'{e(newest["title"])}</a></p>')
    for name, label in (("outside_root", "keyed notes outside the library"),
                        ("stale_collections", "collections Zotero no longer has")):
        value = out.get(name)
        if value:
            parts.append(f"<p>{e(label)}: {e(str(value))}</p>")
    return "".join(parts)


def _write_status(vault, survey: Survey, out: dict[str, Any],
                  versions: Any) -> dict[str, Any] | None:
    """Say in the vault what the mirror just did. One call, and none at all on a
    pass that changed nothing."""
    if not any(out.get(name) for name in DID_SOMETHING):
        return None
    note_id, fresh_pointer = _status_note(vault, survey)
    summary = ", ".join(f"{out[n]} {n}" for n in
                        ("created", "updated", "removed") if out.get(n)) \
        or "no papers changed"
    vault.update(note_id, title=f"{STATUS_NOTE} — {_now()} · {summary}",
                 content=_status_body(out, versions))
    if fresh_pointer:
        vault.set_label(survey.root, STATUS_POINTER, note_id)
    return {"note_id": note_id, "summary": summary}


def _retire(vault, known: dict[str, dict], seen: set[str]) -> int:
    """The papers the mirror owns that the library no longer names.

    The only place a note is removed for being absent, and so the only place a
    caller has to be holding the whole library to be allowed to call.
    """
    removed = 0
    for ref, note in known.items():
        if ref not in seen:
            vault.delete(note["note_id"])
            removed += 1
    return removed


def _plan(vault, b: bundle_mod.Bundle, *, parent: str,
          may_create: bool) -> dict[str, Any]:
    """What an apply would do, writing nothing.

    Three searches and a file read. It can now say what it would *update* as
    well as what it would create, because the fingerprint that decides is
    already on the note and already in the scan — the render-and-compare this
    used to have to avoid is exactly the work the stamp removed.
    """
    if not b.exists:
        raise FileNotFoundError(f"no bundle at {b.root}")
    library = b.read()
    items = library.get("items") or []
    # Resolved without creating: a dry run that made the root note would be a
    # write, and the one thing this verb promises is that it makes none.
    hit = _labelled_root(vault, may_create=may_create)
    root = hit["note_id"] if hit else ""
    labels = hit["labels"] if hit else {}
    survey = (_survey(vault, root, "label", labels) if root
              else Survey(root="", root_from="would create"))
    known = _first(survey.items)
    folders = _first(survey.folders)
    shelves = _first(survey.shelves)

    def would(item: dict) -> str:
        have = known.get(item["ref"])
        if have is None:
            return "create"
        if have["labels"].get(STAMP_LABEL) != _stamp(item):
            return "update"
        wanted = sorted({folders[c]["note_id"]
                         for c in item.get("collections") or []
                         if c in folders}
                        or {(shelves.get(item.get("library", "")) or {})
                            .get("note_id", root)})
        return "replace" if sorted(set(have["parents"])) != wanted else "skip"

    verdicts = [would(i) for i in items]
    seen = {i["ref"] for i in items}
    tree = library.get("collections") or []
    return {
        "dry_run": True,
        "library_note": root, "root_from": survey.root_from,
        "digest": b.digest, "applied": labels.get(APPLIED_LABEL) or "",
        "up_to_date": labels.get(APPLIED_LABEL) == b.digest,
        "would_visit": len(items),
        "would_create": verdicts.count("create"),
        "would_update": verdicts.count("update"),
        "would_replace": verdicts.count("replace"),
        "would_leave_alone": verdicts.count("skip"),
        "would_delete": len([r for r in known if r not in seen]),
        "would_deduplicate": sum(len(n) - 1 for n in survey.items.values()),
        "collections": len(tree),
        "stale_collections": sorted(r for r in survey.folders
                                    if r not in {c["ref"] for c in tree}),
        "outside_root": _outside(vault,
                                 sum(len(n) for n in survey.items.values())),
        "calls": dict(getattr(vault, "calls", {})),
    }


def _apply(vault, b: bundle_mod.Bundle, *, parent: str,
           may_create: bool = True, force: bool = False) -> dict[str, Any]:
    if not b.exists:
        raise FileNotFoundError(
            f"no bundle at {b.root} — run `awm zotero pull` on the node that "
            f"can reach the library")
    library = b.read()
    items = library.get("items") or []

    root, source_of_root, labels = _resolve_root(
        vault, parent=parent, may_create=may_create)
    digest = b.digest
    if not force and labels.get(APPLIED_LABEL) == digest:
        return {"library_note": root, "root_from": source_of_root,
                "skipped": True, "digest": digest, "items": len(items),
                "detail": "the vault already holds this bundle",
                "calls": dict(getattr(vault, "calls", {}))}

    survey = _survey(vault, root, source_of_root, labels)
    known, doubled = _collapse(vault, survey.items)

    # One subtree per library. Two libraries may both have a collection called
    # "papers", and merging them would put a shared group's reading list inside
    # somebody's personal one with nothing saying it had happened.
    out, _folders = _upsert(vault, b, survey, known, items,
                            library.get("collections") or [], force=force)

    removed = _retire(vault, known, {i["ref"] for i in items})

    # Last, and only here: a pass that died halfway must retry rather than
    # declare itself done.
    vault.set_label(root, APPLIED_LABEL, digest)

    out.update({"library_note": root, "root_from": source_of_root,
                "digest": digest, "items": len(items), "removed": removed,
                "deduplicated": doubled,
                "versions": library.get("versions")})
    # The one search left that reads the whole vault, and it produces a single
    # integer. A stray keyed subtree appears when the root moves, which is
    # exactly when things get created — so a pass that changed nothing has
    # nothing to find and does not look.
    if out["created"] or removed or doubled or force:
        out["outside_root"] = _outside(vault, len(items))
    else:
        out["outside_root"] = None
    out["status_note"] = _write_status(vault, survey, out,
                                       library.get("versions"))
    out["calls"] = dict(getattr(vault, "calls", {}))
    return out


def _outside(vault, inside: int) -> int:
    """Keyed notes anywhere in the vault that the pass did not just write.

    Counting notes rather than keys, and against the item count rather than a
    second scoped search: every item has exactly one note under the root by the
    time this runs, so anything above that total lives somewhere else. An
    orphaned subtree left by a root that moved otherwise shows up only as a
    bibliography appearing twice with nothing saying why.
    """
    everywhere = sum(len(ids) for ids in vault.owned_all(None, KEY_LABEL).values())
    return max(0, everywhere - inside)


def _attach(vault, b: bundle_mod.Bundle, note_id: str, item: dict) -> int:
    attached = 0
    for key, name in (item.get("files") or {}).items():
        path = b.file_for(f"{item['library']}/{key}", name)
        if path.is_file() and vault.attach(note_id, path, title=name):
            attached += 1
    return attached
