"""Paths + constants for the notes service.

The service owns its own DB (``AWM_DIR/services/notes/notes.db``) and stores each
note's markdown as a uuid-named file *next to* that DB
(``AWM_DIR/services/notes/files/<uuid>.md``). The DB is the index (title-as-path
tree, FTS, embeddings, trash); the file is the canonical content.

With a user bound (``awm.config.userroot``) every path moves: the files go to
``<user root>/notes/`` — the user's git worktree, so they are versioned — and
the DB, checkouts and orphans to ``SERVICES_DIR/notes/users/<user>/``. One DB
per user keeps the stores isolated by construction. Unbound, the legacy paths
above apply unchanged.
"""

from __future__ import annotations

from pathlib import Path

from awm.config import userroot

# Embedding namespace inside the per-service ``embeddings`` table.
SOURCE_TYPE = "note"

# How long a trashed note lingers before ``purge_expired`` hard-deletes it.
TRASH_TTL_DAYS = 30

# Live-collaboration + lazy-persist tuning.
#   FLUSH_INTERVAL_S — how often the background flusher writes dirty in-memory
#     rooms through to disk (file + DB + FTS + embeddings). Edits live in memory
#     between flushes; a crash can lose at most one interval of edits — the
#     deliberate durability/throughput trade the design calls for.
#   SNAPSHOT_RING — how many recent per-version content snapshots each room keeps
#     so a lagging client's ``base_version`` can be found for a 3-way merge.
FLUSH_INTERVAL_S = 300
SNAPSHOT_RING = 64


def collab_topic(note_id: str) -> str:
    """Pub/sub emit topic a note's collaborators subscribe to for live updates.

    ``note:<user>:<id>`` for a bound user — the public edge admits a subscriber
    only to topics carrying its own user — else the legacy ``note:<id>``."""
    user = userroot.current()
    return f"note:{user}:{note_id}" if user else f"note:{note_id}"


def room_key(note_id: str) -> str:
    """Registry key for a note's live room: one room per (user, note). The
    legacy store keeps the bare id."""
    user = userroot.current()
    return f"{user}/{note_id}" if user else note_id


def _state_dir() -> Path | None:
    user = userroot.current()
    return userroot.state_dir("notes", user) if user else None


def db_path() -> Path:
    """The index DB for the bound user, else the service's own."""
    from awm.persistence.databases import service_db_path

    state = _state_dir()
    return state / "notes.db" if state else service_db_path("notes")


def files_dir() -> Path:
    """Directory holding the uuid-named ``.md`` files. Created on demand.

    For a bound user: ``<user root>/notes``, inside the worktree. Otherwise
    resolved against the live workspace (``AWM_DIR`` follows ``AWM_WORKSPACE``)
    and co-located with the service DB, so a note's on-disk path is stable and
    copy-pasteable from the editor.
    """
    from awm.persistence.databases import service_db_path

    user = userroot.current()
    if user:
        d = userroot.root_for(user) / "notes"
    else:
        d = service_db_path("notes").parent / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def orphaned_dir() -> Path:
    """Where a file written out of band is kept when a flush would erase it.

    Deliberately not ``files_dir`` — a stray ``.md`` there is one restore script
    away from being read back as a note.
    """
    from awm.persistence.databases import service_db_path

    state = _state_dir()
    d = (state or service_db_path("notes").parent) / "orphaned"
    d.mkdir(parents=True, exist_ok=True)
    return d


def checkouts_dir() -> Path:
    """Directory holding one subdirectory per open checkout. Created on demand.

    A sibling of ``files_dir`` rather than a child, so nothing that walks the
    notes directory can mistake a working copy for a note.
    """
    import os

    override = os.environ.get("AWM_NOTES_CHECKOUTS")
    state = _state_dir()
    if state is not None:
        d = state / "checkouts"
    elif override:
        d = Path(override).expanduser()
    else:
        from awm.persistence.databases import service_db_path

        d = service_db_path("notes").parent / "checkouts"
    d.mkdir(parents=True, exist_ok=True)
    return d
