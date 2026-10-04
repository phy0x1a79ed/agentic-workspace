"""Notes service DB bootstrap.

Per the modular invariant there is no shared ``state.db``: this service owns its
own tables (notes + FTS + vocab + a per-service ``embeddings`` table) on its own
SQLite DB (``AWM_DIR/services/notes/notes.db``) and stands them up via
``init_service_db`` at startup.
"""

from __future__ import annotations

import sqlite3

from pathlib import Path

from awm.persistence.databases import get_connection_at, init_db_at
from awm.persistence.embeddings import EMBEDDINGS_DDL

from . import config
from .db import NOTES_DDL

SERVICE = "notes"
SCHEMA_VERSION = 1

SCHEMA_SQL = NOTES_DDL + EMBEDDINGS_DDL

_initialized: set[Path] = set()


def init() -> None:
    """Idempotently create the DB + tables for the bound user (or the service)."""
    path = config.db_path()
    if path not in _initialized:
        init_db_at(path, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized.add(path)


def connect() -> sqlite3.Connection:
    """A fresh connection to the bound user's DB, else the service's own
    (WAL, Row factory). A per-user DB is created on first contact."""
    init()
    return get_connection_at(config.db_path())
