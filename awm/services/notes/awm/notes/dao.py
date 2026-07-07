"""Notes service DB bootstrap.

Per the modular invariant there is no shared ``state.db``: this service owns its
own tables (notes + FTS + vocab + a per-service ``embeddings`` table) on its own
SQLite DB (``AWM_DIR/services/notes/notes.db``) and stands them up via
``init_service_db`` at startup.
"""

from __future__ import annotations

import sqlite3

from awm.persistence.databases import get_connection, init_service_db
from awm.persistence.embeddings import EMBEDDINGS_DDL

from .db import NOTES_DDL

SERVICE = "notes"
SCHEMA_VERSION = 1

SCHEMA_SQL = NOTES_DDL + EMBEDDINGS_DDL

_initialized = False


def init() -> None:
    """Idempotently create the notes service's DB + tables."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


def connect() -> sqlite3.Connection:
    """A fresh connection to the notes service's own DB (WAL, Row factory)."""
    return get_connection(SERVICE)
