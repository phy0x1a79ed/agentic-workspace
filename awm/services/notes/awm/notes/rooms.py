"""In-memory collaboration rooms + lazy write-through persistence.

A **room** is the live, authoritative copy of one note's markdown while it is
being edited. Two browsers on the same note share a room: each keystroke is a
``collab_edit`` that 3-way-merges into the room's content, bumps a version, and
fans out to every subscriber. The on-disk ``.md`` file + DB index are the
*durable* copy but they lag the room — a background flusher writes dirty rooms
through every :data:`config.FLUSH_INTERVAL_S` seconds (and once more on
shutdown). This is the deliberate "hold edits in memory, persist every 5 min"
trade: fewer disk writes + a natural merge point, at the cost of losing at most
one flush interval of edits on a hard crash.

Merge model (Google-Docs-lite, sized for a couple of clients):
  - The server is authoritative. Every client sends its full content plus the
    ``base_version`` it last saw.
  - No concurrent change (``base_version == room.version``) → the client's text
    is taken verbatim (fast path).
  - Otherwise a 3-way merge: ``diff-match-patch`` builds the patch the client
    made against the content *it* had (looked up in the per-room snapshot ring)
    and applies it onto the room's current content, so the other client's
    interleaved edits survive.

Thread-safety: read/write note handlers run in a worker thread
(``asyncio.to_thread``) while ``collab_edit`` and the flusher run on the loop
thread, so every registry mutation is guarded by :data:`_LOCK`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from diff_match_patch import diff_match_patch

from . import config, db, index, store

log = logging.getLogger("awm.notes.rooms")

_LOCK = threading.RLock()
_ROOMS: dict[str, "Room"] = {}

_dmp = diff_match_patch()
# A little slack so a patch still lands when the surrounding text drifted from a
# concurrent edit (this is exactly the collaborative case).
_dmp.Match_Threshold = 0.5
_dmp.Patch_DeleteThreshold = 0.5


class Stale(RuntimeError):
    """A write was composed against a note that has since moved on."""


@dataclass
class Room:
    note_id: str
    content: str
    version: int = 0
    dirty: bool = False
    # version -> content, bounded to the most recent ``config.SNAPSHOT_RING``.
    snaps: dict[int, str] = field(default_factory=dict)
    updated: float = field(default_factory=time.monotonic)
    # Revision of the text this process last knew to be in the file. A file
    # whose revision is neither this nor the room's was written by someone
    # else, which is otherwise indistinguishable from a merely stale one.
    persisted_rev: str = ""

    def _record_snap(self) -> None:
        self.snaps[self.version] = self.content
        if len(self.snaps) > config.SNAPSHOT_RING:
            for v in sorted(self.snaps)[: -config.SNAPSHOT_RING]:
                self.snaps.pop(v, None)


def _merge(base: str, mine: str, current: str) -> str:
    """3-way merge: apply the change ``base``→``mine`` onto ``current``."""
    if mine == current or base == mine:
        return current            # client added nothing new
    if base == current:
        return mine               # no concurrent change; client wins wholesale
    patches = _dmp.patch_make(base, mine)
    merged, _applied = _dmp.patch_apply(patches, current)
    return merged


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def open_room(note_id: str, *, initial_content: str | None = None) -> Room:
    """Return the note's room, loading its content from disk on first open."""
    with _LOCK:
        r = _ROOMS.get(note_id)
        if r is None:
            content = store.read(note_id) if initial_content is None else initial_content
            r = Room(note_id=note_id, content=content, version=0)
            r.snaps[0] = content
            r.persisted_rev = db.text_rev(store.read(note_id))
            _ROOMS[note_id] = r
        return r


def snapshot(note_id: str, *, initial_content: str | None = None) -> dict:
    """The current authoritative ``{version, content}`` a joining client adopts."""
    r = open_room(note_id, initial_content=initial_content)
    with _LOCK:
        return {"version": r.version, "content": r.content}


def apply_edit(note_id: str, base_version: int, content: str) -> dict:
    """Merge a client's edit into the room. Returns the new authoritative
    ``{version, content, changed}`` (``changed`` false = a no-op edit)."""
    with _LOCK:
        r = _ROOMS.get(note_id) or open_room(note_id)
        if int(base_version) >= r.version:
            merged = content                       # no concurrent change
        else:
            base = r.snaps.get(int(base_version))
            merged = _merge(base if base is not None else r.content, content, r.content)
        if merged == r.content:
            return {"version": r.version, "content": r.content, "changed": False}
        r.version += 1
        r.content = merged
        r.dirty = True
        r.updated = time.monotonic()
        r._record_snap()
        return {"version": r.version, "content": r.content, "changed": True}


def live_content(note_id: str) -> str | None:
    """The room's in-memory content if one is open, else ``None`` (read disk)."""
    with _LOCK:
        r = _ROOMS.get(note_id)
        return r.content if r is not None else None


def live_text(note_id: str) -> str:
    """A note's authoritative text right now: the room's copy, else the file.

    The one definition of "live" every writer measures drift against. Reading
    the file happens outside the lock — it is only reached when no room exists,
    so there is nothing to be consistent with.
    """
    live = live_content(note_id)
    return live if live is not None else store.read(note_id)


def live_rev(note_id: str) -> str:
    """The revision token of :func:`live_text` — what a writer must still match."""
    return db.text_rev(live_text(note_id))


def text_at_rev(note_id: str, rev: str) -> str | None:
    """The note's text at ``rev``, if it is still reachable from here.

    A revision token is a hash, so it cannot be inverted — this finds the text
    among the copies that happen to still exist: the file, the room's current
    content, and the room's recent snapshot ring. ``None`` means the base a
    writer composed against is gone, and the only honest answer is to make it
    take a checkout and reconcile deliberately.
    """
    disk = store.read(note_id)
    if db.text_rev(disk) == rev:
        return disk
    with _LOCK:
        r = _ROOMS.get(note_id)
        if r is None:
            return None
        if db.text_rev(r.content) == rev:
            return r.content
        for _version, text in sorted(r.snaps.items(), reverse=True):
            if db.text_rev(text) == rev:
                return text
    return None


def file_diverged(note_id: str) -> bool:
    """Is an open room holding text the file does not have?

    The condition that makes an out-of-band file edit invisible: readers get
    the room, the file says something else, and the next flush erases the
    difference. Costs a file read, so it answers ``False`` immediately when no
    room is open — which is the overwhelmingly common case.
    """
    with _LOCK:
        r = _ROOMS.get(note_id)
        if r is None:
            return False
        content = r.content
    return content != store.read(note_id)


def sync_from_disk(note_id: str, content: str) -> None:
    """Reconcile a room to an authoritative out-of-band write (a CLI/agent
    ``save`` that already persisted to disk). Bumps the version so a later
    subscriber adopts it; marks clean since disk already matches."""
    with _LOCK:
        r = _ROOMS.get(note_id)
        if r is None:
            return
        if r.content != content:
            r.version += 1
            r.content = content
            r._record_snap()
        r.dirty = False
        r.persisted_rev = db.text_rev(content)


def drop(note_id: str) -> None:
    """Forget a room (note trashed/purged). Any unflushed edits are discarded —
    callers trash/purge the durable copy separately."""
    with _LOCK:
        _ROOMS.pop(note_id, None)


# ---------------------------------------------------------------------------
# Flush — write dirty rooms through to the durable store + indexes
# ---------------------------------------------------------------------------


def _preserve_out_of_band_write(note_id: str) -> Path | None:
    """Save aside a file someone else wrote, before this flush overwrites it.

    A room shadows disk, so an out-of-band write to the ``.md`` is invisible to
    every reader and is erased by the next flush — the failure this whole
    checkout system exists to remove. It cannot be *prevented* here: the room
    holds text a person typed, and declining the flush would destroy that
    instead. So the bytes are kept and the fact is logged, and nothing is lost
    either way. Returns the copy's path, or ``None`` if the file is as expected.
    """
    with _LOCK:
        r = _ROOMS.get(note_id)
        expected = r.persisted_rev if r is not None else ""
    if not expected:
        return None
    on_disk = store.read(note_id)
    if db.text_rev(on_disk) == expected:
        return None
    dest = config.orphaned_dir() / f"{note_id}.{int(time.time())}.md"
    dest.write_text(on_disk, encoding="utf-8")
    log.warning(
        "note %s: the file was written out of band and a live room holds newer "
        "text; the file's version is preserved at %s. Edit notes through "
        "`notes checkout` / `notes merge`, not by writing the file.",
        note_id, dest,
    )
    return dest


def _persist_durable(conn, note_id: str, content: str) -> tuple[str | None, str | None]:
    """The fast half of a write: file, row, keyword index — committed together.

    Split out from :func:`_persist` because :func:`land` holds the room lock
    across it and must not also hold it across the embedding. Returns
    ``(content_hash, embedded_hash)`` so the caller can decide about embedding,
    or ``(None, None)`` if the note was hard-deleted underneath us.
    """
    r = db.get_note(conn, note_id)
    if r is None:
        return None, None                          # note was hard-deleted
    _preserve_out_of_band_write(note_id)
    chash = db.content_hash(content)
    # Committed FIRST so even a force-kill mid-flush (embedding can be slow:
    # first call loads the sentence-transformers model) leaves the note fully
    # saved + searchable.
    store.write(note_id, content)
    db.upsert_note(
        conn,
        {
            "id": note_id,
            "path": r["path"],
            "created": r["created"],
            "modified": db.now_iso(),
            "deleted_at": r["deleted_at"],
            "content_hash": chash,
            "embedded_hash": r["embedded_hash"],
        },
    )
    db.fts_replace(conn, note_id, r["path"], content)
    conn.commit()
    return chash, r["embedded_hash"]


def _persist(conn, note_id: str, content: str) -> None:
    chash, embedded_hash = _persist_durable(conn, note_id, content)
    # Embedding — slower; committed separately. If it's interrupted or the
    # embedding stack is unavailable, the row's embedded_hash stays stale so a
    # later write re-embeds (no data lost).
    if chash is not None and chash != embedded_hash:
        index.reembed(conn, note_id, content, chash)
        conn.commit()


def land(conn, note_id: str, content: str, expect_rev: str) -> dict:
    """Replace a note's live text as one transaction, or refuse.

    Every durable change to a note's body goes through here, so "am I about to
    clobber someone?" is one question, asked in one place, under the lock that
    keeps the answer true until the write lands. It **refuses** rather than
    merging: reconciliation belongs to the caller, in its own working copy, at
    a moment it chose — by then landing is a guarded single write that cannot
    produce a note neither side asked for.

    Returns the landed revision and the room's new version (``None`` if no
    browser has this note open), which the caller fans out to subscribers.
    """
    with _LOCK:
        r = _ROOMS.get(note_id)
        current = r.content if r is not None else store.read(note_id)
        if db.text_rev(current) != expect_rev:
            raise Stale(
                f"note {note_id} has moved since {expect_rev} "
                f"(now {db.text_rev(current)}); reconcile against the live text first"
            )
        chash, embedded_hash = _persist_durable(conn, note_id, content)
        if chash is None:
            # The row went away underneath us. Reporting success here would be
            # the same silent no-op this module exists to make impossible.
            raise Stale(f"note {note_id} no longer exists")
        version = None
        if r is not None:
            if r.content != content:
                r.version += 1
                r.content = content
                r._record_snap()
            r.dirty = False
            r.persisted_rev = db.text_rev(content)
            version = r.version
    # Outside the lock: the first embed in a process loads the model, and every
    # keystroke on this note queues behind whatever holds it.
    if chash is not None and chash != embedded_hash:
        index.reembed(conn, note_id, content, chash)
        conn.commit()
    return {"note_id": note_id, "rev": db.text_rev(content), "version": version}


def flush_all(conn) -> list[str]:
    """Persist every dirty room. Safe to call from a worker thread — it snapshots
    the dirty set under the lock, does the (blocking) disk/index writes outside
    it, then clears ``dirty`` only for rooms that didn't change meanwhile."""
    with _LOCK:
        pending = [(r.note_id, r.content, r.version) for r in _ROOMS.values() if r.dirty]
    flushed: list[str] = []
    for note_id, content, version in pending:
        _persist(conn, note_id, content)
        with _LOCK:
            r = _ROOMS.get(note_id)
            if r is not None and r.version == version:
                r.dirty = False
        flushed.append(note_id)
    return flushed
