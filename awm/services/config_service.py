"""Config service — key-value store backed by the config table."""

from __future__ import annotations

from datetime import datetime, timezone

from awm.db import get_connection


def get_config(key: str, default: str | None = None) -> str | None:
    """Get a config value by key."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return default
    return row["value"]


def set_config(key: str, value: str) -> None:
    """Set a config value (upsert)."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now),
        )
        conn.commit()
    finally:
        conn.close()
