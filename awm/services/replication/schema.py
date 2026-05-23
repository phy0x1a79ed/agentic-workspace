"""cr-sqlite extension loading and CRR registration.

The extension binary is vendored at ``awm/_native/crsqlite.so`` (Linux x86_64
for now — capella/xps/mira are the only deployment targets). If missing or
unloadable, the daemon falls back to single-peer mode and replication is a
no-op — the rest of AWM keeps working.

CRR registration is idempotent: ``crsql_as_crr`` is safe to call repeatedly
on the same table (cr-sqlite tracks state in its own shadow tables).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger("awm.replication.schema")


# Vendored Linux x86_64 binary. Override via env for other platforms /
# operator-built binaries.
_DEFAULT_EXT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "_native" / "crsqlite.so"
)
CRSQLITE_EXT_PATH = Path(os.environ.get("AWM_CRSQLITE_EXT", str(_DEFAULT_EXT_PATH)))


# Tables registered as Conflict-free Replicated Relations. The five
# INTEGER-PK tables (room_posts, scopes, session_logs, messages, artifacts)
# are intentionally absent until their per-table INTEGER→UUID migrations
# land (one per PR per the phase-4 plan); cr-sqlite requires non-AUTOINCREMENT
# unique PKs so each peer can mint new IDs without collisions.
CRR_TABLES: tuple[str, ...] = (
    "rooms",
    "room_participants",
    "peers",
    "discord_operators",
)


_loaded_in_process = False


def crsqlite_available() -> bool:
    """True iff the extension binary is present on disk."""
    return CRSQLITE_EXT_PATH.is_file()


def load_extension(conn: sqlite3.Connection) -> bool:
    """Load cr-sqlite into ``conn``. Returns True on success.

    On failure (missing binary, load error), logs a warning once and
    returns False — the caller should proceed in single-peer mode.
    """
    global _loaded_in_process
    if not crsqlite_available():
        if not _loaded_in_process:
            log.warning(
                "cr-sqlite extension not found at %s — running in single-peer "
                "mode (no DB replication). Drop the binary at that path to "
                "enable.", CRSQLITE_EXT_PATH,
            )
            _loaded_in_process = True
        return False
    try:
        conn.enable_load_extension(True)
        conn.load_extension(str(CRSQLITE_EXT_PATH))
    except (sqlite3.OperationalError, AttributeError) as exc:
        if not _loaded_in_process:
            log.warning(
                "cr-sqlite extension failed to load (%s) — running in "
                "single-peer mode.", exc,
            )
            _loaded_in_process = True
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except (sqlite3.OperationalError, AttributeError):
            pass
    return True


def register_all_crrs(conn: sqlite3.Connection) -> list[str]:
    """Idempotently mark each ``CRR_TABLES`` entry as a CRR. Returns the list
    of successfully-registered tables. Tables that don't exist yet (partial
    DBs from older versions) are skipped silently."""
    registered: list[str] = []
    for table in CRR_TABLES:
        try:
            # Skip if table doesn't exist (avoid raising during partial migrations).
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            conn.execute(f"SELECT crsql_as_crr('{table}')")
            registered.append(table)
        except sqlite3.OperationalError as exc:
            log.warning("crsql_as_crr(%s) failed: %s", table, exc)
    try:
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return registered


def finalize(conn: sqlite3.Connection) -> None:
    """Call ``crsql_finalize()`` before closing a connection that loaded the
    extension. Safe to call even if the extension wasn't loaded."""
    try:
        conn.execute("SELECT crsql_finalize()")
    except sqlite3.OperationalError:
        pass
