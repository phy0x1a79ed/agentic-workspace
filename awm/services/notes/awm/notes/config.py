"""Paths + constants for the notes service.

The service owns its own DB (``AWM_DIR/services/notes/notes.db``) and stores each
note's markdown as a uuid-named file *next to* that DB
(``AWM_DIR/services/notes/files/<uuid>.md``). The DB is the index (title-as-path
tree, FTS, embeddings, trash); the file is the canonical content.
"""

from __future__ import annotations

from pathlib import Path

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
    """Pub/sub emit topic a note's collaborators subscribe to for live updates."""
    return f"note:{note_id}"


def files_dir() -> Path:
    """Directory holding the uuid-named ``.md`` files. Created on demand.

    Resolved against the live workspace (``AWM_DIR`` follows ``AWM_WORKSPACE``)
    and co-located with the service DB, so a note's on-disk path is stable and
    copy-pasteable from the editor.
    """
    from awm.persistence.databases import service_db_path

    d = service_db_path("notes").parent / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d
