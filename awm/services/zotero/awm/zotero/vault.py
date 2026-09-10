"""The vault, as this service is allowed to reach it: over the trilium
service's verbs.

The vault belongs to `trilium`. This service does not import it and does not
open its own ETAPI connection, for the reason the architecture gives generally
— a cross-service reference is a call to the owning service, never an import —
and for one specific to this pair: `trilium` supervises the Trilium child. It
starts it, restarts it, and holds it down across a restore. A second writer
reaching past that would write into a database being swapped out from under it,
and would learn nothing from the gate that exists to stop exactly this.

So everything below is `gatewayclient.call_sync("trilium", …)`. It is the note
API that service grew for the purpose, and this is its first real consumer.

**Why the interface is this narrow.** A mirror needs eight verbs, not the
twenty-eight the trilium domain has. Naming them here is what lets the sync be
tested against a dictionary, and what stops the sync growing a dependence on
some corner of ETAPI that would then have to be carried across a node boundary.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any

from awm.gatewayclient import call_sync

log = logging.getLogger("awm.zotero.vault")

SERVICE = "trilium"

#: Long, because an apply is thousands of calls and a single slow one behind a
#: PDF import should not fail the pass.
TIMEOUT_S = 300.0

#: The most notes a scan will look at.
#:
#: A search that comes back holding exactly this many has almost certainly been
#: cut off, and a cut-off scan is worse than no scan at all: the pass cannot see
#: notes the mirror already owns, decides they are new, and creates a second
#: copy of every one of them. That is the 216-doubled-papers failure with a
#: different cause, and every call still succeeds.
SCAN_LIMIT = 10000


class VaultError(RuntimeError):
    """The trilium service answered, and said no."""


def labels_of(note: dict) -> dict[str, str]:
    """The labels a note owns, which is not the same as the labels it shows.

    Trilium hands back inherited attributes alongside a note's own, so a label
    arriving from a template above it looks identical to one the mirror wrote.
    Reading an inherited value as this note's cursor would skip a note that was
    never written. The trilium service's own attribute writer draws the line in
    the same place, by owner.
    """
    note_id = note.get("noteId")
    return {a.get("name"): a.get("value") or ""
            for a in note.get("attributes") or []
            if a.get("type") == "label" and a.get("noteId") == note_id}


class Vault:
    """What a mirror needs of a knowledge base."""

    def __init__(self) -> None:
        #: Verb -> how many times this pass called it. The mirror's whole cost
        #: is round trips, so a change that claims to be cheaper is judged by
        #: this rather than by a stopwatch.
        self.calls: Counter[str] = Counter()

    def _call(self, fn: str, **args: Any) -> Any:
        self.calls[fn] += 1
        try:
            return call_sync(SERVICE, fn, args, timeout=TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 — the transport's own errors vary
            raise VaultError(f"trilium {fn}: {e}") from e

    # -- reading -------------------------------------------------------------

    def scan(self, root: str | None, label: str) -> dict[str, list[dict]]:
        """Every note under `root` carrying `label`, with everything the search
        already told us about it: `{value: [{note_id, title, labels, parents}…]}`.

        A `root` of `None` searches the whole vault, which is how the mirror
        counts what it owns outside the subtree it is writing.

        One search rather than a walk: the mirror's notes are scattered through
        the collection tree, and this is what tells an update from an insert
        for all of them at once.

        **The extra fields are free, and they are the point.** A Trilium search
        result carries the matched note's whole attribute list and its current
        parents. Reading only the label that was filtered on threw away both,
        and the pass then paid a round trip per note to ask again — a cursor
        read and a placement check that the search had already answered.

        A list rather than one entry per value, because two notes for one key is
        a state the vault can be in — two syncs running at once each create the
        note the other has not written yet — and a caller that cannot see the
        second copy can never remove it.

        `archived` is set because a mirrored note somebody archived is still
        the mirror's. Trilium's search excludes archived notes by default and
        the flag is inherited, so without this one checkbox on a note — or on
        anything above it — hides a paper the mirror owns, and the next pass
        creates a second copy of it that the collapse pass cannot see either.
        """
        hits = self._call("note_search", query=f"#{label}", ancestor=root or "",
                          limit=SCAN_LIMIT, fast=False, archived=True)
        results = (hits or {}).get("results") or []
        if len(results) >= SCAN_LIMIT:
            raise VaultError(
                f"the vault holds at least {SCAN_LIMIT} notes carrying "
                f"#{label} and the search was cut off. A pass cannot run on a "
                f"partial answer: it would treat every note it could not see as "
                f"new and create a second copy of it.")
        out: dict[str, list[dict]] = {}
        for note in results:
            labels = labels_of(note)
            value = labels.get(label)
            if not value:
                continue
            out.setdefault(value, []).append({
                "note_id": note["noteId"],
                "title": note.get("title") or "",
                "labels": labels,
                "parents": list(note.get("parentNoteIds") or []),
            })
        return out

    def owned_all(self, root: str | None, label: str) -> dict[str, list[str]]:
        """`scan`, keeping only the ids. The shape every caller wanted before
        the rest of the search result turned out to be worth keeping."""
        return {value: [n["note_id"] for n in notes]
                for value, notes in self.scan(root, label).items()}

    def labelled(self, label: str) -> list[dict[str, Any]]:
        """Every note in the vault carrying `label`, with its own labels.

        No ancestor, because this is the call that *finds* the root and so
        cannot be scoped to one. Archived notes are included for the reason
        `owned_all` gives. Sorted by note id so a message naming an ambiguity
        names it the same way twice.

        The labels come back with the hit: the root's own attributes are read
        on every pass anyway, so the sync's cursor costs no extra round trip.
        """
        hits = self._call("note_search", query=f"#{label}", limit=100,
                          fast=False, archived=True)
        out: list[dict[str, Any]] = []
        for note in (hits or {}).get("results") or []:
            out.append({
                "note_id": note["noteId"],
                "title": note.get("title") or "",
                "labels": labels_of(note),
                "parents": list(note.get("parentNoteIds") or []),
            })
        return sorted(out, key=lambda n: n["note_id"])

    def owned(self, root: str, label: str) -> dict[str, str]:
        """The same, keeping one note per value."""
        return {key: ids[0] for key, ids in self.owned_all(root, label).items()}

    def read(self, note_id: str) -> dict:
        return self._call("note_get", note_id=note_id)

    # -- writing -------------------------------------------------------------

    def ensure(self, *, parent: str, title: str, content: str,
               type: str = "book") -> str:
        """The note with exactly this title under this parent, made if absent.
        Used for the library's own root, whose identity is what it is called."""
        return self._call("note_upsert", parent=parent, title=title,
                     content=content, type=type)["note_id"]

    def create(self, *, parent: str, title: str, content: str = "",
               type: str = "text", labels: dict[str, str] | None = None) -> str:
        return self._call("note_create", parent=parent, title=title,
                     content=content, type=type, labels=labels or {})["note_id"]

    def update(self, note_id: str, *, title: str | None = None,
               content: str | None = None,
               labels: dict[str, str] | None = None) -> bool:
        """Change a note, and say whether anything actually moved.

        Everything in one call: five labels set one at a time is five round
        trips per note, and this runs over a thousand of them on a timer.
        """
        args: dict[str, Any] = {"note_id": note_id}
        if title is not None:
            args["title"] = title
        if content is not None:
            args["content"] = content
        if labels:
            args["labels"] = labels
        if len(args) == 1:
            return False
        changed = self._call("note_update", **args).get("changed") or {}
        return any(v not in (False, [], None) for v in changed.values())

    def set_label(self, note_id: str, name: str, value: str) -> bool:
        return bool(self._call("attr_set", note_id=note_id, name=name,
                          value=value).get("changed"))

    def place(self, note_id: str, parents: list[str]) -> dict:
        """Show the note under exactly these parents.

        One call, because it is one question. It is also a branch operation
        rather than a copy: a paper in three collections is one note in three
        places, and copies would let it diverge from itself.
        """
        return self._call("note_place", note_id=note_id, parents=sorted(set(parents)))

    def attach(self, note_id: str, path: Path, *,
               title: str | None = None) -> bool:
        """Attach a file the *service* can read.

        The path is sent rather than the bytes: an apply carries a hundred
        megabytes of PDF, and base64 over the RPC envelope would carry it
        twice. Both processes are on this host and the bundle is a committed
        artifact of it, so a path is a shared reference rather than a guess.
        """
        out = self._call("attachment_put", note_id=note_id, path=str(path),
                    title=title or path.name)
        return bool(out.get("changed"))

    def delete(self, note_id: str) -> None:
        self._call("note_delete", note_id=note_id)
