"""CRUD over the ``discord_operators`` table.

A row whitelists a Discord user to run the ``/login`` slash command, and
maps them to an ``awm_user`` identity that ``X-Awm-As`` will report.
"""

from __future__ import annotations

from datetime import datetime, timezone

from awm.db import get_connection


def add_operator(discord_user_id: str, awm_user: str) -> dict:
    """Idempotent: re-adding overwrites the ``awm_user`` mapping."""
    if not discord_user_id or not str(discord_user_id).strip():
        raise ValueError("discord_user_id is required")
    if not awm_user or not str(awm_user).strip():
        raise ValueError("awm_user is required")
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """\
            INSERT INTO discord_operators (discord_user_id, awm_user, added_at)
            VALUES (?, ?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                awm_user = excluded.awm_user
            """,
            (str(discord_user_id).strip(), str(awm_user).strip(), now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"discord_user_id": str(discord_user_id).strip(),
            "awm_user": str(awm_user).strip(), "added_at": now}


def remove_operator(discord_user_id: str) -> bool:
    """Return True if a row was deleted."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM discord_operators WHERE discord_user_id = ?",
            (str(discord_user_id).strip(),),
        )
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


def list_operators() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM discord_operators ORDER BY added_at"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "discord_user_id": r["discord_user_id"],
            "awm_user": r["awm_user"],
            "added_at": r["added_at"],
        }
        for r in rows
    ]


def lookup(discord_user_id: str) -> str | None:
    """Return the ``awm_user`` for a Discord ID, or None if not whitelisted."""
    if not discord_user_id:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT awm_user FROM discord_operators WHERE discord_user_id = ?",
            (str(discord_user_id).strip(),),
        ).fetchone()
    finally:
        conn.close()
    return row["awm_user"] if row else None
