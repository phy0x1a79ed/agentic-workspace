"""Writing service DB bootstrap.

Per the modular invariant there is no shared ``state.db``: this service owns its
own tables (samples + tags + FTS + a per-service ``embeddings`` table) on its own
SQLite DB (``AWM_DIR/services/writing/writing.db``) and stands them up via
``init_service_db`` at startup.
"""

from __future__ import annotations

import sqlite3

from awm.persistence.databases import get_connection, init_service_db
from awm.persistence.embeddings import EMBEDDINGS_DDL

from .db import WRITING_DDL

SERVICE = "writing"
SCHEMA_VERSION = 1

SCHEMA_SQL = WRITING_DDL + EMBEDDINGS_DDL

_initialized = False


def init() -> None:
    """Idempotently create the writing service's DB + tables."""
    global _initialized
    if not _initialized:
        init_service_db(SERVICE, SCHEMA_SQL, schema_version=SCHEMA_VERSION)
        _initialized = True


def connect() -> sqlite3.Connection:
    """A fresh connection to the writing service's own DB (WAL, Row factory)."""
    return get_connection(SERVICE)
