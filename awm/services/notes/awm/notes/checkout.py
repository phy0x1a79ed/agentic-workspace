"""Checkouts: the concurrency contract for editing a note.

An agent takes a checkout of a note, edits it for as long as it likes while a
person types into the same note in the browser, and lands the result as one
transaction. The guarantee is that neither party can silently destroy the
other's work — which is exactly what writing the ``.md`` file directly does
today, in both directions and with no error anywhere.

**The working copy is the editing interface; merging is git.** A note is
markdown, so there is no operation layer to build: the file at :func:`path` is
the thing you edit, with whatever tool you like. What that buys is a single
writer inside the checkout, which is what makes editing safe there. Across
writers nothing is safe but a merge that can *fail*, so the boundary uses git's
line-based three-way merge — dumber than anything content-aware, and loud when
it cannot decide.

**Reconciliation only ever happens in :func:`update`,** inside the caller's own
checkout, at a moment it chose, where it can read the result before landing it.
:func:`merge` refuses while the checkout is behind. By then landing is a guarded
single write and cannot produce a note neither side asked for.

**What "live" means here.** Notes keeps an open note in an in-memory room that
shadows the file, so the live text is the room's when one exists and the file's
otherwise (:func:`awm.notes.rooms.live_text`). Drift is measured against a hash
of that text, never the room's version counter — which is ephemeral, and resets
to zero every time the service restarts.

**No history to fall back on.** Drawio's equivalent stores diagrams in a git
repo and can ask it what a document looked like when a checkout was taken. A
note is one file, overwritten in place. So a checkout carries its own base
snapshot beside the working copy, and that snapshot is the merge base. The
happy consequence is that a checkout is entirely on disk: nothing needs
rebuilding after a restart.

**The escape hatch.** Conflicts land in the working copy as ordinary
``<<<<<<<`` markers. Edit the file by hand, then call :func:`resolve` — the
same thing you would do to any other conflicted text file. :func:`merge` refuses
while markers remain, so a half-resolved note cannot land.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config, db, rooms

#: Conflict markers ``git merge-file`` writes. Their presence in a working copy
#: is what "still conflicted" means — the state file records intent, but the
#: file itself is the truth, since resolving is done by editing it.
MARKERS = ("<<<<<<< ", "=======", ">>>>>>> ")

WORKING_FILENAME = "note.md"
BASE_FILENAME = "base.md"
STATE_FILENAME = "checkout.json"

log = logging.getLogger("awm.notes.checkout")


class CheckoutError(RuntimeError):
    """A checkout operation could not be completed."""


class Conflicted(CheckoutError):
    """The working copy holds unresolved conflict markers."""


class Behind(CheckoutError):
    """The live note moved since this checkout was taken."""


@dataclass
class Handle:
    """A checkout's durable state. The caller only ever sees ``id``."""

    id: str
    note_id: str
    note_path: str
    base_rev: str
    author: str
    created: float
    updated: float
    state: str = "clean"          # clean | conflicted
    conflicts: int = 0
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def count_markers(text: str) -> int:
    """Conflict *regions*, counted by opening marker at line start."""
    return sum(1 for line in text.splitlines() if line.startswith(MARKERS[0]))


def merge3(base: str, ours: str, theirs: str,
           labels: tuple[str, str, str] = ("ours", "common base", "live")) -> tuple[str, int]:
    """Three-way line merge. Returns ``(merged_text, conflict_count)``.

    ``git merge-file`` needs no index, no worktree, and cannot leave a
    ``MERGE_HEAD`` behind; its exit status *is* the number of conflict regions.
    Everything runs in a scratch directory so a failed merge cannot leave
    fragments where something else would later read them as content.
    """
    with tempfile.TemporaryDirectory(prefix="awm-notes-merge-") as tmp:
        d = Path(tmp)
        ours_p, base_p, theirs_p = d / "ours", d / "base", d / "theirs"
        ours_p.write_text(ours, encoding="utf-8")
        base_p.write_text(base, encoding="utf-8")
        theirs_p.write_text(theirs, encoding="utf-8")
        proc = subprocess.run(
            ["git", "merge-file",
             "-L", labels[0], "-L", labels[1], "-L", labels[2],
             str(ours_p), str(base_p), str(theirs_p)],
            capture_output=True, text=True,
        )
        # Negative is a signal; git itself reports an actual error as 255. Both
        # would otherwise read as an implausible pile of conflicts.
        if proc.returncode < 0 or proc.returncode > 127:
            raise CheckoutError(f"merge failed: {proc.stderr.strip() or proc.returncode}")
        return ours_p.read_text(encoding="utf-8"), proc.returncode


class Checkouts:
    """Checkout registry rooted at ``root`` (one directory per handle)."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else config.checkouts_dir()
        self.root.mkdir(parents=True, exist_ok=True)

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
        return self._dir(handle_id) / WORKING_FILENAME

    def base_path(self, handle_id: str) -> Path:
        return self._dir(handle_id) / BASE_FILENAME

    # --- the contract ------------------------------------------------------

    def list(self, note_id: str | None = None) -> list[Handle]:
        handles: list[Handle] = []
        for state in sorted(self.root.glob(f"*/{STATE_FILENAME}")):
            try:
                handle = Handle(**json.loads(state.read_text(encoding="utf-8")))
            except (OSError, TypeError, ValueError) as exc:
                # Skipping it makes a caller's whole checkout vanish from
                # `notes checkouts` — the work is still on disk beside this
                # file, so say where.
                log.warning("unreadable checkout state at %s (%s); the working "
                            "copy beside it is not listed", state, exc)
                continue
            if note_id is None or handle.note_id == note_id:
                handles.append(handle)
        return handles

    def checkout(self, note_id: str, author: str, *, note_path: str = "") -> Handle:
        """Take a working copy of the note's live text, pinned to its revision."""
        text = rooms.live_text(note_id)
        handle = Handle(
            id=uuid.uuid4().hex[:12],
            note_id=note_id,
            note_path=note_path,
            base_rev=db.text_rev(text),
            author=author,
            created=time.time(),
            updated=time.time(),
        )
        self._dir(handle.id).mkdir(parents=True)
        self.file_path(handle.id).write_text(text, encoding="utf-8")
        self.base_path(handle.id).write_text(text, encoding="utf-8")
        self._save_state(handle)
        return handle

    def note_id_of(self, handle_id: str) -> str:
        return self._load(handle_id).note_id

    def read(self, handle_id: str) -> str:
        self._load(handle_id)                      # 404 on a bad handle, not a bare OSError
        return self.file_path(handle_id).read_text(encoding="utf-8")

    def write(self, handle_id: str, content: str) -> dict:
        """Replace the working copy's text. Local to the checkout until merged."""
        handle = self._load(handle_id)
        path = self.file_path(handle_id)
        path.write_text(content, encoding="utf-8")
        markers = count_markers(content)
        handle.state = "conflicted" if markers else handle.state
        handle.conflicts = markers or handle.conflicts
        self._save_state(handle)
        return {"handle": handle.id, "note_id": handle.note_id,
                "path": str(path), "bytes": len(content.encode("utf-8")),
                "conflict_markers": markers}

    def status(self, handle_id: str) -> dict:
        """Where this checkout stands relative to the live note."""
        handle = self._load(handle_id)
        text = self.read(handle_id)
        base_text = self.base_path(handle_id).read_text(encoding="utf-8")
        markers = count_markers(text)
        live_rev = rooms.live_rev(handle.note_id)
        return {
            "handle": handle.id,
            "note_id": handle.note_id,
            "note_path": handle.note_path,
            "author": handle.author,
            "state": "conflicted" if markers else handle.state,
            "conflict_markers": markers,
            "ahead": text != base_text,
            "behind": live_rev != handle.base_rev,
            "base_rev": handle.base_rev,
            "live_rev": live_rev,
            "path": str(self.file_path(handle_id)),
        }

    def update(self, handle_id: str) -> dict:
        """Pull the live note's changes into this checkout.

        A three-way line merge of *base* (what the checkout started from),
        *ours* (the working copy as it stands) and *theirs* (the live note).
        Clean merges rebase the checkout onto the live revision. Dirty ones
        leave conflict markers in the working copy for the caller to resolve by
        hand — see :meth:`resolve`.
        """
        handle = self._load(handle_id)
        ours = self.read(handle_id)
        if handle.state == "conflicted" or count_markers(ours):
            raise Conflicted(
                f"checkout {handle_id} still has unresolved conflict markers; "
                f"edit {self.file_path(handle_id)} and call resolve first"
            )

        live = rooms.live_text(handle.note_id)
        live_rev = db.text_rev(live)
        if live_rev == handle.base_rev:
            return {"handle": handle.id, "updated": False, "conflicts": 0,
                    "base_rev": handle.base_rev, "note": "already up to date"}

        base = self.base_path(handle_id).read_text(encoding="utf-8")
        merged, conflicts = merge3(base, ours, live,
                                   labels=(f"checkout {handle.id}", "common base", "live note"))

        self.file_path(handle_id).write_text(merged, encoding="utf-8")
        # The base must follow the live text, or the next update re-merges
        # changes this one already folded in.
        self.base_path(handle_id).write_text(live, encoding="utf-8")
        handle.base_rev = live_rev
        handle.conflicts = conflicts
        handle.state = "conflicted" if conflicts else "clean"
        handle.note = (
            f"{conflicts} conflict(s) — edit the file at "
            f"{self.file_path(handle_id)} to resolve, then call resolve"
            if conflicts else "merged cleanly"
        )
        self._save_state(handle)
        return {"handle": handle.id, "updated": True, "conflicts": conflicts,
                "base_rev": handle.base_rev, "path": str(self.file_path(handle_id)),
                "note": handle.note}

    def resolve(self, handle_id: str) -> dict:
        """Declare a hand-edited checkout resolved.

        Markdown always parses, so unlike drawio's equivalent there is nothing
        to validate but the markers themselves — everything about *content* is
        the caller's judgment.
        """
        handle = self._load(handle_id)
        path = self.file_path(handle_id)
        markers = count_markers(path.read_text(encoding="utf-8"))
        if markers:
            raise Conflicted(
                f"{markers} conflict marker(s) remain in {path}; remove them "
                "(keeping the content you want) before resolving"
            )
        handle.state = "clean"
        handle.conflicts = 0
        handle.note = "resolved by hand"
        self._save_state(handle)
        return {"handle": handle.id, "state": "clean", "note_id": handle.note_id}

    def merge(self, conn, handle_id: str, *, keep: bool = False) -> dict:
        """Land the checkout onto the live note.

        Refuses if the checkout is conflicted or behind — by construction this
        is never a merge, only a guarded single write, which is what makes it
        atomic. The write itself goes through :func:`awm.notes.rooms.land`, so
        an open browser adopts the result instead of overwriting it.
        """
        handle = self._load(handle_id)
        path = self.file_path(handle_id)
        text = path.read_text(encoding="utf-8")

        markers = count_markers(text)
        if markers:
            raise Conflicted(
                f"{markers} conflict marker(s) remain in {path}; resolve before merging"
            )
        if handle.state != "clean":
            raise Conflicted(f"checkout {handle_id} is {handle.state}; resolve first")

        live_rev = rooms.live_rev(handle.note_id)
        if live_rev != handle.base_rev:
            raise Behind(
                f"note {handle.note_id} moved since this checkout was taken; "
                "call update first"
            )

        try:
            landed = rooms.land(conn, handle.note_id, text, handle.base_rev)
        except rooms.Stale as exc:
            # Someone wrote between the check above and the lock. Same answer.
            raise Behind(str(exc)) from exc

        if keep:
            handle.base_rev = landed["rev"]
            handle.note = "merged"
            self.base_path(handle_id).write_text(text, encoding="utf-8")
            self._save_state(handle)
        else:
            self.discard(handle_id)
        return {"handle": handle.id, "note_id": handle.note_id,
                "rev": landed["rev"], "version": landed["version"], "kept": keep}

    def discard(self, handle_id: str) -> dict:
        handle = self._load(handle_id)
        _rmtree(self._dir(handle_id))
        return {"handle": handle_id, "discarded": True, "note_id": handle.note_id}


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# The service holds one registry; tests build their own against a tmp root.
_REGISTRY: Checkouts | None = None


def registry() -> Checkouts:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = Checkouts()
    return _REGISTRY


def reset_registry() -> None:
    """Drop the cached registry so the next call re-reads ``config``."""
    global _REGISTRY
    _REGISTRY = None
