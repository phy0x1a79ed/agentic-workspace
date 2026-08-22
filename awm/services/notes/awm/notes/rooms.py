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

import threading
import time
from dataclasses import dataclass, field

from diff_match_patch import diff_match_patch

from . import config, db, index, store

_LOCK = threading.RLock()
_ROOMS: dict[str, "Room"] = {}

_dmp = diff_match_patch()
# A little slack so a patch still lands when the surrounding text drifted from a
# concurrent edit (this is exactly the collaborative case).
_dmp.Match_Threshold = 0.5
_dmp.Patch_DeleteThreshold = 0.5


@dataclass
class Room:
    note_id: str
    content: str
    version: int = 0
    dirty: bool = False
    # version -> content, bounded to the most recent ``config.SNAPSHOT_RING``.
    snaps: dict[int, str] = field(default_factory=dict)
    updated: float = field(default_factory=time.monotonic)

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


def drop(note_id: str) -> None:
    """Forget a room (note trashed/purged). Any unflushed edits are discarded —
    callers trash/purge the durable copy separately."""
    with _LOCK:
        _ROOMS.pop(note_id, None)


# ---------------------------------------------------------------------------
# Flush — write dirty rooms through to the durable store + indexes
# ---------------------------------------------------------------------------


def _persist(conn, note_id: str, content: str) -> None:
    r = db.get_note(conn, note_id)
    if r is None:
        return                                     # note was hard-deleted
    chash = db.content_hash(content)
    # 1) Durable content + keyword index — fast, committed FIRST so even a
    #    force-kill mid-flush (embedding can be slow: first call loads the
    #    sentence-transformers model) leaves the note fully saved + searchable.
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
    # 2) Embedding — slower; committed separately. If it's interrupted or the
    #    embedding stack is unavailable, the row's embedded_hash stays stale so a
    #    later write re-embeds (no data lost).
    if chash != r["embedded_hash"]:
        index.reembed(conn, note_id, content, chash)
        conn.commit()


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
