"""Per-service SQLite DB factory.

Each feature service owns its own SQLite database at
``AWM_DIR/services/<service>/<service>.db``. There is no shared runtime
``state.db`` — the old single-file store survives on disk only as a
read-only legacy seed source (per-service seed steps read it later).

The connection/PRAGMA setup (WAL, busy_timeout, FK ON, ``sqlite3.Row`` row
factory) and the generic version/migration loop are lifted verbatim from the
former ``db.py`` so callers get byte-identical connection behaviour, now keyed
per service rather than a single global path.

A service registers ITS OWN tables via :func:`init_service_db`, passing the
DDL for its tables plus an integer ``schema_version`` it owns independently of
every other service.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from collections import OrderedDict
from pathlib import Path

try:  # SERVICES_DIR lands in awm.config later (T3); derive it until then.
    from awm.config import SERVICES_DIR
except ImportError:  # pragma: no cover - depends on T3 timing
    from awm.config import AWM_DIR
    SERVICES_DIR = AWM_DIR / "services"


def new_uuid() -> str:
    """Mint a fresh uuid4 hex for use as a row primary key."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Per-service DB paths + connections
# ---------------------------------------------------------------------------


def service_db_path(service: str) -> Path:
    """Filesystem path of ``service``'s own SQLite DB.

    ``AWM_DIR/services/<service>/<service>.db``. The parent directory is
    created on demand so callers never have to mkdir first.
    """
    db_dir = SERVICES_DIR / service
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / f"{service}.db"


def get_connection(service: str) -> sqlite3.Connection:
    """Return a new SQLite connection to ``service``'s own DB (WAL mode).

    Same PRAGMA setup as the former single-DB ``db.get_connection``: WAL
    journal, a 5s busy timeout, foreign keys ON, and ``sqlite3.Row`` rows.
    Keyed by service name — every service gets a distinct DB file.
    """
    return get_connection_at(service_db_path(service))


def get_connection_at(path: Path) -> sqlite3.Connection:
    """The same connection setup for a DB at an explicit path — a service that
    keeps one DB per user opens its per-user files through here."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = _open(path)
    _hold_open(path)
    return conn


def _open(path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


# SQLite checkpoints and fsyncs a WAL database when its last connection
# closes, which costs seconds on a busy disk. One idle connection per recent
# DB keeps every caller's close from being the last. A DB replaced on disk
# (new inode) gets a fresh keeper, so the old one never pins a deleted file.
_KEEPERS_MAX = 16
_keepers: OrderedDict[str, tuple[int, sqlite3.Connection]] = OrderedDict()
_keepers_lock = threading.Lock()


def _hold_open(path: Path) -> None:
    key = str(Path(path).resolve())
    try:
        inode = os.stat(key).st_ino
    except OSError:
        return
    with _keepers_lock:
        held = _keepers.get(key)
        if held is not None and held[0] == inode:
            _keepers.move_to_end(key)
            return
    keeper = _open(key, check_same_thread=False)
    with _keepers_lock:
        held = _keepers.pop(key, None)
        _keepers[key] = (inode, keeper)
        stale = [held[1]] if held is not None else []
        while len(_keepers) > _KEEPERS_MAX:
            stale.append(_keepers.popitem(last=False)[1][1])
    for conn in stale:
        conn.close()


def release_keepers() -> None:
    """Close every idle keeper connection, checkpointing each DB."""
    with _keepers_lock:
        held = [conn for _, conn in _keepers.values()]
        _keepers.clear()
    for conn in held:
        conn.close()


# ---------------------------------------------------------------------------
# Schema init + migrations (generic; one owning service at a time)
# ---------------------------------------------------------------------------


def _migrate(
    conn: sqlite3.Connection,
    current: int,
    target: int,
    migrations: dict[tuple[int, int], str],
) -> None:
    """Apply ``migrations`` from ``current`` up to ``target``.

    Generic executescript loop lifted from the former ``db._migrate`` (minus
    the v37 special-case and the cr-sqlite finalize, both obsolete under
    per-service DBs). Each step runs with FKs off for the DROP/RENAME dance
    SQLite's table-rebuild pattern requires, then re-checks declared FKs.
    """
    while current < target:
        next_ver = current + 1
        sql = migrations.get((current, next_ver))
        if sql is None:
            raise RuntimeError(f"No migration path from v{current} to v{next_ver}")
        try:
            conn.executescript(
                "PRAGMA foreign_keys=OFF;\n"
                + sql
                + f"\nUPDATE schema_version SET version = {next_ver};\n"
                + "PRAGMA foreign_key_check;\n"
                + "PRAGMA foreign_keys=ON;"
            )
        except sqlite3.OperationalError as exc:
            # Partial prior migration (column already added but version not
            # bumped due to a crash), or an ALTER against a table the DB never
            # had (contrived test fixtures). Bump version and continue.
            msg = str(exc)
            if "duplicate column name" in msg or "no such table" in msg:
                conn.execute("UPDATE schema_version SET version = ?", (next_ver,))
                conn.commit()
            else:
                raise
        current = next_ver


def init_service_db(
    service: str,
    schema_sql: str,
    *,
    schema_version: int,
    migrations: dict[tuple[int, int], str] | None = None,
) -> None:
    """Create ``service``'s own tables if absent, migrating as needed.

    ``schema_sql`` is the service's DDL for ITS OWN tables only — the full
    current-shape schema, executed once on a fresh DB. ``schema_version`` is
    the integer version that DDL represents; the owning service manages it
    independently of every other service. ``migrations`` maps
    ``(from, to)`` → SQL for upgrading an existing DB.

    A ``schema_version`` (version INTEGER) table is managed here; the service
    need not declare it in ``schema_sql``.
    """
    init_db_at(service_db_path(service), schema_sql,
               schema_version=schema_version, migrations=migrations)


def init_db_at(
    path: Path,
    schema_sql: str,
    *,
    schema_version: int,
    migrations: dict[tuple[int, int], str] | None = None,
) -> None:
    """``init_service_db`` for a DB at an explicit path."""
    conn = get_connection_at(path)
    try:
        row = None
        try:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
        except sqlite3.OperationalError:
            pass  # table doesn't exist yet

        if row is None:
            # Fresh DB — create everything at the current shape.
            conn.executescript(schema_sql)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            existing = conn.execute("SELECT version FROM schema_version").fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (schema_version,),
                )
            else:
                conn.execute(
                    "UPDATE schema_version SET version = ?", (schema_version,)
                )
            conn.commit()
        else:
            current = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            if current < schema_version:
                _migrate(conn, current, schema_version, migrations or {})
                conn.commit()
    finally:
        conn.close()
