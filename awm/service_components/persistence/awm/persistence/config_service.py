"""Config service — key-value store backed by a ``config``-owned DB.

This is the KV store (``config`` table) — DISTINCT from the ``awm.config``
settings module (paths/ports/env). The KV store lives in the ``config``
service's own SQLite DB under ``AWM_DIR/services/config/config.db``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from awm.config import GITHUB_USER
from awm.persistence.databases import get_connection, init_service_db

_SERVICE = "config"

# The ``config`` service owns exactly this one KV table.
CONFIG_TABLE_DDL = """\
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CONFIG_SCHEMA_VERSION = 1
_initialized = False


def _ensure_db() -> None:
    """Idempotently create the config service's own DB + ``config`` table."""
    global _initialized
    if not _initialized:
        init_service_db(
            _SERVICE, CONFIG_TABLE_DDL, schema_version=_CONFIG_SCHEMA_VERSION
        )
        _initialized = True


def get_config(key: str, default: str | None = None) -> str | None:
    """Get a config value by key."""
    _ensure_db()
    conn = get_connection(_SERVICE)
    try:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return default
    return row["value"]


def set_config(key: str, value: str) -> None:
    """Set a config value (upsert)."""
    _ensure_db()
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(_SERVICE)
    try:
        conn.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_vagrant_scopes_repo_url() -> str:
    """Resolve the GitHub URL for the unified vagrant-scopes bare repo.

    Stored under the ``vagrant_scopes_repo_url`` config key so an operator
    can repoint it at runtime (e.g. mirror, alternate account) via
    ``set_config``.
    """
    default = f"git@github.com:{GITHUB_USER}/vagrant-scopes.git"
    return get_config("vagrant_scopes_repo_url", default=default) or default
