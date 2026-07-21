"""Checkouts: the concurrency contract agents work through.

An agent takes a checkout of a diagram, edits it for as long as it likes while a
person edits the same diagram in the browser, and lands the result as one
transaction. The guarantee is that neither party can silently destroy the
other's work.

**Editing is operations; merging is git.** Operations (:mod:`awm.drawio.dwg`)
are safe *inside* a checkout because a checkout has exactly one writer, so
applying them is deterministic and there is nothing to reconcile. They are not
safe *across* writers: merging two operation streams yields a structurally
valid document that can be semantically wrong with no conflict raised — an
agent that shifts a row of cells to make room for a new one, against a person
who drags one of those same cells, merges cleanly attribute by attribute and
leaves the row visibly broken. A merge algorithm that cannot tell you it failed
is not a merge algorithm. So the merge boundary uses git's line-based
three-way merge, which is dumber and fails loudly.

**Reconciliation only ever happens in :func:`update`,** inside the agent's own
checkout, at a moment the agent chooses, where it can render the result and
check it. :func:`merge` refuses while the checkout is behind. By the time
:func:`merge` runs, landing is a guarded single-file write — atomic, and
incapable of producing a document neither side asked for.

**The escape hatch.** Three-way merge conflicts land in the checkout file as
ordinary ``<<<<<<<`` markers. The agent edits the file at :func:`path` by hand,
then calls :func:`resolve`. This is deliberately the same mechanism a person
would use on any other text file: v1 will meet edge cases the operation layer
cannot express, and the cost of hitting one should be an afternoon of manual
editing, not a blocked workflow. :func:`merge` refuses while markers remain, so
a half-resolved file cannot land.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path

from . import xmlmodel
from .store import Store, StoreError, normalize_save_path

#: Conflict markers `git merge-file` writes. Their presence in a checkout is
#: what "still conflicted" means — the state file records intent, but the file
#: itself is the truth, since the agent resolves by editing it directly.
MARKERS = ("<<<<<<< ", "=======", ">>>>>>> ")

CHECKOUT_FILENAME = "diagram.drawio"
STATE_FILENAME = "checkout.json"


class CheckoutError(RuntimeError):
    """A checkout operation could not be completed."""


class Conflicted(CheckoutError):
    """The checkout holds unresolved conflict markers."""


class Behind(CheckoutError):
    """The live diagram moved since this checkout was taken."""


@dataclass
class Handle:
    """A checkout's durable state. The agent only ever sees ``id``."""

    id: str
    save: str
    base_rev: str | None
    author: str
    created: float
    updated: float
    state: str = "clean"          # clean | conflicted
    conflicts: int = 0
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class Checkouts:
    """Checkout registry rooted at ``root`` (one directory per handle)."""

    def __init__(self, store: Store, root: Path):
        self.store = store
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._repin()

    # --- handle bookkeeping ------------------------------------------------

    def _dir(self, handle_id: str) -> Path:
        if not handle_id or "/" in handle_id or handle_id.startswith("."):
            raise CheckoutError(f"malformed checkout handle {handle_id!r}")
        return self.root / handle_id

    def _load(self, handle_id: str) -> Handle:
        state = self._dir(handle_id) / STATE_FILENAME
        if not state.is_file():
            raise CheckoutError(f"no such checkout: {handle_id!r}")
        return Handle(**json.loads(state.read_text(encoding="utf-8")))

    def _save_state(self, handle: Handle) -> None:
        handle.updated = time.time()
        (self._dir(handle.id) / STATE_FILENAME).write_text(
            json.dumps(handle.as_dict(), indent=2), encoding="utf-8",
        )

    def file_path(self, handle_id: str) -> Path:
        return self._dir(handle_id) / CHECKOUT_FILENAME

    def _repin(self) -> None:
        """Re-pin every live checkout's base revision after a restart.

        Pins live in memory in the store, so a service restart would otherwise
        let an amend move a base sha out from under an existing checkout.
        """
        for handle in self.list():
            if handle.base_rev:
                self.store.pin_revision(handle.base_rev)

    # --- the contract ------------------------------------------------------

    def list(self, save: str | None = None) -> list[Handle]:
        handles = []
        for state in sorted(self.root.glob(f"*/{STATE_FILENAME}")):
            try:
                handle = Handle(**json.loads(state.read_text(encoding="utf-8")))
            except (OSError, TypeError, ValueError):
                continue
            if save is None or handle.save == normalize_save_path(save):
                handles.append(handle)
        return handles

    def checkout(self, save: str, author: str) -> Handle:
        """Take a working copy of ``save`` pinned to its current revision."""
        path = normalize_save_path(save)
        if not self.store.exists(path):
            raise CheckoutError(f"no diagram at {path!r}")
        handle = Handle(
            id=uuid.uuid4().hex[:12],
            save=path,
            base_rev=self.store.head_rev(path),
            author=author,
            created=time.time(),
            updated=time.time(),
        )
        self._dir(handle.id).mkdir(parents=True)
        self.file_path(handle.id).write_text(
            self.store.read(path), encoding="utf-8",
        )
        if handle.base_rev:
            self.store.pin_revision(handle.base_rev)
        self._save_state(handle)
        return handle

    def content_rev(self, handle_id: str) -> str:
        """A revision token for the checkout's *file*, so an editor tab open on
        a checkout gets the same stale-save protection as one open on a live
        diagram. Checkouts are not git-tracked, so the content hash stands in
        for a commit sha."""
        import hashlib

        raw = self.file_path(handle_id).read_bytes()
        return hashlib.sha256(raw).hexdigest()[:16]

    def status(self, handle_id: str) -> dict:
        """Where this checkout stands relative to the live diagram."""
        handle = self._load(handle_id)
        text = self.file_path(handle_id).read_text(encoding="utf-8")
        markers = _count_markers(text)

        behind: list = []
        if handle.base_rev:
            try:
                behind = self.store.changed_since(handle.save, handle.base_rev)
            except StoreError:
                behind = []

        base_text = (self.store.read(handle.save, rev=handle.base_rev)
                     if handle.base_rev else "")
        return {
            "handle": handle.id,
            "save": handle.save,
            "author": handle.author,
            "state": "conflicted" if markers else handle.state,
            "conflict_markers": markers,
            "ahead": text != base_text,
            "behind": len(behind),
            "behind_revisions": [r.as_dict() for r in behind],
            "base_rev": handle.base_rev,
            "live_rev": self.store.head_rev(handle.save),
            "content_rev": self.content_rev(handle_id),
            "path": str(self.file_path(handle_id)),
        }

    def update(self, handle_id: str) -> dict:
        """Pull the live diagram's changes into this checkout.

        A three-way line merge of *base* (what the checkout started from),
        *ours* (the checkout as it stands) and *theirs* (the live diagram).
        Clean merges rebase the checkout onto the live revision. Dirty ones
        leave conflict markers in the file for the agent to resolve by hand —
        see :func:`resolve`.
        """
        handle = self._load(handle_id)
        if handle.state == "conflicted" or _count_markers(
                self.file_path(handle_id).read_text(encoding="utf-8")):
            raise Conflicted(
                f"checkout {handle_id} still has unresolved conflict markers; "
                f"edit {self.file_path(handle_id)} and call resolve first"
            )

        live_rev = self.store.head_rev(handle.save)
        if live_rev == handle.base_rev:
            return {"handle": handle.id, "updated": False, "conflicts": 0,
                    "base_rev": handle.base_rev,
                    "note": "already up to date"}

        ours = self.file_path(handle_id)
        base = ours.with_suffix(".base")
        theirs = ours.with_suffix(".live")
        base.write_text(
            self.store.read(handle.save, rev=handle.base_rev)
            if handle.base_rev else "", encoding="utf-8",
        )
        theirs.write_text(self.store.read(handle.save), encoding="utf-8")

        # merge-file predates every git version question this workspace has;
        # it needs no index, no worktree, and cannot leave MERGE_HEAD behind.
        proc = subprocess.run(
            ["git", "merge-file",
             "-L", f"checkout {handle.id}", "-L", "common base", "-L", "live",
             str(ours), str(base), str(theirs)],
            capture_output=True, text=True,
        )
        base.unlink(missing_ok=True)
        theirs.unlink(missing_ok=True)
        if proc.returncode < 0:
            raise CheckoutError(f"merge failed: {proc.stderr.strip()}")

        conflicts = proc.returncode
        handle.base_rev = live_rev
        handle.conflicts = conflicts
        handle.state = "conflicted" if conflicts else "clean"
        if live_rev:
            self.store.pin_revision(live_rev)
        handle.note = (
            f"{conflicts} conflict(s) — edit the file at {ours} to resolve, "
            "then call resolve" if conflicts else "merged cleanly"
        )
        self._save_state(handle)
        return {"handle": handle.id, "updated": True, "conflicts": conflicts,
                "base_rev": handle.base_rev, "path": str(ours),
                "note": handle.note}

    def resolve(self, handle_id: str) -> dict:
        """Declare a hand-edited checkout resolved.

        Verifies what can be verified — no leftover markers, still parseable as
        a diagram — and refuses otherwise. Everything above this line trusts the
        agent's judgment about *content*; this only enforces that the file is a
        diagram again.
        """
        handle = self._load(handle_id)
        path = self.file_path(handle_id)
        text = path.read_text(encoding="utf-8")

        markers = _count_markers(text)
        if markers:
            raise Conflicted(
                f"{markers} conflict marker(s) remain in {path}; remove them "
                "(keeping the content you want) before resolving"
            )
        try:
            xmlmodel.parse(text)
        except ValueError as exc:
            raise CheckoutError(f"{path} is not a valid diagram: {exc}") from exc

        handle.state = "clean"
        handle.conflicts = 0
        handle.note = "resolved by hand"
        self._save_state(handle)
        return {"handle": handle.id, "state": "clean", "save": handle.save}

    def merge(self, handle_id: str, label: str | None = None,
              keep: bool = False) -> dict:
        """Land the checkout onto the live diagram.

        Refuses if the checkout is conflicted or behind — by construction this
        is never a merge, only a guarded single-file write, which is what makes
        it atomic. Callers that need to win against an actively-autosaving
        browser tab should hold the editor's write lease across this call (see
        :mod:`awm.drawio.session`); this function does not sleep or retry.
        """
        handle = self._load(handle_id)
        path = self.file_path(handle_id)
        text = path.read_text(encoding="utf-8")

        markers = _count_markers(text)
        if markers:
            raise Conflicted(
                f"{markers} conflict marker(s) remain in {path}; resolve before merging"
            )
        if handle.state != "clean":
            raise Conflicted(f"checkout {handle_id} is {handle.state}; resolve first")

        live_rev = self.store.head_rev(handle.save)
        if live_rev != handle.base_rev:
            behind = self.store.changed_since(handle.save, handle.base_rev) \
                if handle.base_rev else []
            raise Behind(
                f"{handle.save} moved since this checkout was taken "
                f"({len(behind)} revision(s)); call update first"
            )

        result = self.store.write(
            handle.save, text, author=handle.author,
            label=label or f"merge checkout {handle.id}",
            allow_amend=False,
        )
        if not keep:
            self.discard(handle_id)
        else:
            handle.base_rev = result["rev"]
            self.store.pin_revision(result["rev"])
            handle.note = "merged"
            self._save_state(handle)
        return {"handle": handle.id, "save": handle.save, "rev": result["rev"],
                "changed": result["changed"], "kept": keep}

    def discard(self, handle_id: str) -> dict:
        handle = self._load(handle_id)
        if handle.base_rev and not any(
            other.base_rev == handle.base_rev
            for other in self.list() if other.id != handle.id
        ):
            self.store.unpin_revision(handle.base_rev)
        _rmtree(self._dir(handle_id))
        return {"handle": handle_id, "discarded": True, "save": handle.save}


def _count_markers(text: str) -> int:
    """Conflict *regions*, counted by opening marker at line start."""
    return sum(1 for line in text.splitlines() if line.startswith(MARKERS[0]))


def _rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def default_root() -> Path:
    override = os.environ.get("AWM_DRAWIO_CHECKOUTS")
    if override:
        return Path(override).expanduser()
    from awm.persistence.databases import service_db_path

    return service_db_path("drawio").parent / "checkouts"
