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
from pathlib import Path
from typing import Any

from awm.gatewayclient import call_sync

log = logging.getLogger("awm.zotero.vault")

SERVICE = "trilium"

#: Long, because an apply is thousands of calls and a single slow one behind a
#: PDF import should not fail the pass.
TIMEOUT_S = 300.0


class VaultError(RuntimeError):
    """The trilium service answered, and said no."""


def _call(fn: str, **args: Any) -> Any:
    try:
        return call_sync(SERVICE, fn, args, timeout=TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — the transport's own errors vary
        raise VaultError(f"trilium {fn}: {e}") from e


class Vault:
    """What a mirror needs of a knowledge base."""

    # -- reading -------------------------------------------------------------

    def owned_all(self, root: str | None, label: str) -> dict[str, list[str]]:
        """Every note under `root` carrying `label`, as `{value: [note_id…]}`.
        A `root` of `None` searches the whole vault, which is how the mirror
        counts what it owns outside the subtree it is writing.

        One search rather than a walk: the mirror's notes are scattered through
        the collection tree, and this is what tells an update from an insert
        for all of them at once.

        A list rather than one id, because two ids for one key is a state the
        vault can be in — two syncs running at once each create the note the
        other has not written yet — and a caller that cannot see the second
        copy can never remove it.

        `archived` is set because a mirrored note somebody archived is still
        the mirror's. Trilium's search excludes archived notes by default and
        the flag is inherited, so without this one checkbox on a note — or on
        anything above it — hides a paper the mirror owns, and the next pass
        creates a second copy of it that the collapse pass cannot see either.
        """
        hits = _call("note_search", query=f"#{label}", ancestor=root or "",
                     limit=10000, fast=False, archived=True)
        out: dict[str, list[str]] = {}
        for note in (hits or {}).get("results") or []:
            for a in note.get("attributes") or []:
                if a.get("type") == "label" and a.get("name") == label:
                    out.setdefault(a.get("value") or "", []).append(note["noteId"])
        out.pop("", None)
        return out

    def labelled(self, label: str) -> list[dict[str, Any]]:
        """Every note in the vault carrying `label`, with its own labels.

        No ancestor, because this is the call that *finds* the root and so
        cannot be scoped to one. Archived notes are included for the reason
        `owned_all` gives. Sorted by note id so a message naming an ambiguity
        names it the same way twice.

        The labels come back with the hit: the root's own attributes are read
        on every pass anyway, so the sync's cursor costs no extra round trip.
        """
        hits = _call("note_search", query=f"#{label}", limit=100, fast=False,
                     archived=True)
        out: list[dict[str, Any]] = []
        for note in (hits or {}).get("results") or []:
            out.append({
                "note_id": note["noteId"],
                "title": note.get("title") or "",
                "labels": {a.get("name"): a.get("value") or ""
                           for a in note.get("attributes") or []
                           if a.get("type") == "label"},
            })
        return sorted(out, key=lambda n: n["note_id"])

    def owned(self, root: str, label: str) -> dict[str, str]:
        """The same, keeping one note per value."""
        return {key: ids[0] for key, ids in self.owned_all(root, label).items()}

    def read(self, note_id: str) -> dict:
        return _call("note_get", note_id=note_id)

    # -- writing -------------------------------------------------------------

    def ensure(self, *, parent: str, title: str, content: str,
               type: str = "book") -> str:
        """The note with exactly this title under this parent, made if absent.
        Used for the library's own root, whose identity is what it is called."""
        return _call("note_upsert", parent=parent, title=title,
                     content=content, type=type)["note_id"]

    def create(self, *, parent: str, title: str, content: str = "",
               type: str = "text", labels: dict[str, str] | None = None) -> str:
        return _call("note_create", parent=parent, title=title,
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
        changed = _call("note_update", **args).get("changed") or {}
        return any(v not in (False, [], None) for v in changed.values())

    def set_label(self, note_id: str, name: str, value: str) -> bool:
        return bool(_call("attr_set", note_id=note_id, name=name,
                          value=value).get("changed"))

    def place(self, note_id: str, parents: list[str]) -> dict:
        """Show the note under exactly these parents.

        One call, because it is one question. It is also a branch operation
        rather than a copy: a paper in three collections is one note in three
        places, and copies would let it diverge from itself.
        """
        return _call("note_place", note_id=note_id, parents=sorted(set(parents)))

    def attach(self, note_id: str, path: Path, *,
               title: str | None = None) -> bool:
        """Attach a file the *service* can read.

        The path is sent rather than the bytes: an apply carries a hundred
        megabytes of PDF, and base64 over the RPC envelope would carry it
        twice. Both processes are on this host and the bundle is a committed
        artifact of it, so a path is a shared reference rather than a guess.
        """
        out = _call("attachment_put", note_id=note_id, path=str(path),
                    title=title or path.name)
        return bool(out.get("changed"))

    def delete(self, note_id: str) -> None:
        _call("note_delete", note_id=note_id)
