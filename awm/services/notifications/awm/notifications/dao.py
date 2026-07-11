"""Notifications service DB bootstrap.

Per the modular invariant there is no shared ``state.db``: this service owns
its own tables on its own SQLite DB
(``AWM_DIR/services/notifications/notifications.db``) and stands them up via
``init_service_db`` at startup.
"""

from __future__ import annotations

import sqlite3

from awm.persistence.databases import get_connection, init_service_db

from .db import MIGRATIONS, NOTIFICATIONS_DDL

SERVICE = "notifications"
SCHEMA_VERSION = 2

_initialized = False


def init() -> None:
    """Idempotently create the notifications service's DB + tables."""
    global _initialized
    if not _initialized:
        init_service_db(
            SERVICE, NOTIFICATIONS_DDL,
            schema_version=SCHEMA_VERSION, migrations=MIGRATIONS,
        )
        _initialized = True


def connect() -> sqlite3.Connection:
    """A fresh connection to this service's own DB (WAL, Row factory)."""
    return get_connection(SERVICE)
