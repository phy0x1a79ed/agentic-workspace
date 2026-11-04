"""Persistent credential store for the ``auth`` service.

Owns two things in the service's own SQLite DB:

* **The signing secret** — one long-lived HMAC key (``awm_secret``) the edge uses
  to verify+slide session cookies offline. Minted once, never rotated (rotating
  it would invalidate every live session; the credentials rotate instead).

* **Device tokens** — ``awm_device_tokens`` holds, per minted token, its owner
  (``operator`` for every token minted since v6), a label, an optional scope and the SHA-256 of its secret. The secret itself is never stored. A
  revoked row stays, with ``revoked_at`` set, so a listing can still name it.

This module is pure storage. No login password or peer credential lives here:
a person signs in with the passkey (fleet.md R84), and a node with its ssh edge.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from awm.persistence.databases import get_connection, init_service_db

SERVICE = "auth"
_SCHEMA_VERSION = 7

_SCHEMA = """
CREATE TABLE awm_secret (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    secret TEXT NOT NULL
);
CREATE TABLE awm_device_tokens (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    label         TEXT NOT NULL,
    secret_sha256 TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_used_at  REAL,
    revoked_at    REAL,
    scope         TEXT
);
"""

_DEVICE_TOKENS_TABLE = """\
CREATE TABLE IF NOT EXISTS awm_device_tokens (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    label         TEXT NOT NULL,
    secret_sha256 TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_used_at  REAL,
    revoked_at    REAL
);
"""

_MIGRATIONS: dict[tuple[int, int], str] = {
    (1, 2): """\
CREATE TABLE IF NOT EXISTS awm_users (
    username   TEXT PRIMARY KEY,
    pw_hash    TEXT NOT NULL,
    pw_salt    TEXT NOT NULL,
    pw_params  TEXT NOT NULL,
    created_at REAL NOT NULL,
    disabled   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS awm_login_fail (
    key          TEXT PRIMARY KEY,
    fails        INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0,
    last_at      REAL NOT NULL DEFAULT 0
);
""",
    # v3 added the retired Penpot credential table. The step stays so the
    # version chain holds, and creates nothing.
    (2, 3): "",
    (3, 4): _DEVICE_TOKENS_TABLE,
    (4, 5): "ALTER TABLE awm_device_tokens ADD COLUMN scope TEXT;",
    # Static per-user accounts are gone. A token was live only while its owner
    # existed and was enabled, so revoke the rest before the owners vanish:
    # dropping the table must not bring a disabled user's token back.
    # awm_penpot held Penpot passwords in plaintext (install.md R134).
    (5, 6): """\
UPDATE awm_device_tokens
   SET revoked_at = (julianday('now') - 2440587.5) * 86400.0
 WHERE revoked_at IS NULL
   AND (scope IS NULL
        OR username NOT IN (SELECT username FROM awm_users WHERE disabled = 0));
DROP TABLE IF EXISTS awm_users;
DROP TABLE IF EXISTS awm_penpot;
""",
    # The passkey grant replaced the shared login password and its lockout
    # (fleet.md R84, users.md R130), and the edge list the peer credential.
    (6, 7): """\
DROP TABLE IF EXISTS awm_credentials;
DROP TABLE IF EXISTS awm_login_fail;
""",
}


def init() -> None:
    """Create the auth DB (idempotent)."""
    init_service_db(SERVICE, _SCHEMA, schema_version=_SCHEMA_VERSION,
                    migrations=_MIGRATIONS)


# --- signing secret --------------------------------------------------------


def ensure_secret() -> str:
    """Return the HMAC signing secret, minting it once on first use."""
    conn = get_connection(SERVICE)
    try:
        row = conn.execute("SELECT secret FROM awm_secret WHERE id = 1").fetchone()
        if row is not None:
            return row["secret"]
        secret = secrets.token_urlsafe(48)
        conn.execute("INSERT INTO awm_secret (id, secret) VALUES (1, ?)", (secret,))
        conn.commit()
        return secret
    finally:
        conn.close()


# --- device tokens -----------------------------------------------------------


def _device_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "label": row["label"],
        "secret_sha256": row["secret_sha256"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "revoked_at": row["revoked_at"],
        "scope": row["scope"],
    }


def device_insert(token_id: str, *, username: str, label: str, secret_sha256: str,
                  scope: str | None = None,
                  now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    conn = get_connection(SERVICE)
    try:
        conn.execute(
            "INSERT INTO awm_device_tokens "
            "(id, username, label, secret_sha256, created_at, scope) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token_id, username, label, secret_sha256, now, scope))
        conn.commit()
    finally:
        conn.close()
    return device_get(token_id)  # type: ignore[return-value]


def device_get(token_id: str) -> dict[str, Any] | None:
    conn = get_connection(SERVICE)
    try:
        row = conn.execute(
            "SELECT * FROM awm_device_tokens WHERE id = ?", (token_id,)).fetchone()
        return _device_row(row) if row is not None else None
    finally:
        conn.close()


def device_list() -> list[dict[str, Any]]:
    """Every token, oldest first. Revoked ones included."""
    conn = get_connection(SERVICE)
    try:
        rows = conn.execute(
            "SELECT * FROM awm_device_tokens ORDER BY created_at, id").fetchall()
        return [_device_row(r) for r in rows]
    finally:
        conn.close()


def device_live() -> list[dict[str, Any]]:
    """Every unrevoked token, oldest first."""
    conn = get_connection(SERVICE)
    try:
        rows = conn.execute(
            "SELECT * FROM awm_device_tokens WHERE revoked_at IS NULL "
            "ORDER BY created_at, id").fetchall()
        return [_device_row(r) for r in rows]
    finally:
        conn.close()


def device_revoke(token_id: str, now: float | None = None) -> bool:
    """Mark a token revoked. Keeps the first ``revoked_at`` on a second call.
    Returns whether the token exists."""
    now = time.time() if now is None else now
    conn = get_connection(SERVICE)
    try:
        conn.execute(
            "UPDATE awm_device_tokens SET revoked_at = ? "
            "WHERE id = ? AND revoked_at IS NULL", (now, token_id))
        conn.commit()
    finally:
        conn.close()
    return device_get(token_id) is not None


def device_touch(used: dict[str, float]) -> int:
    """Move ``last_used_at`` forward to each reported time. Never moves it back.
    Returns the number of rows changed."""
    if not used:
        return 0
    conn = get_connection(SERVICE)
    try:
        changed = 0
        for token_id, at in used.items():
            cur = conn.execute(
                "UPDATE awm_device_tokens SET last_used_at = ? "
                "WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)",
                (at, token_id, at))
            changed += cur.rowcount
        conn.commit()
        return changed
    finally:
        conn.close()
