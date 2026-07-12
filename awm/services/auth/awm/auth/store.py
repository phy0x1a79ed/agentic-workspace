"""Persistent credential store for the ``auth`` service.

Owns two things in the service's own SQLite DB:

* **The signing secret** — one long-lived HMAC key (``awm_secret``) the edge uses
  to verify+slide session cookies offline. Minted once, never rotated (rotating
  it would invalidate every live session; the credentials rotate instead).
* **The credential generations** — each row is one minted *pair*
  (``login_password``, ``peer_credential``) with a ``minted_at`` / ``expires_at``
  window (``awm_credentials``). Rotation inserts a new generation every cadence;
  with a 12h cadence and 24h validity, up to two generations are valid at once,
  which is exactly what lets a client keep working across a rotation without
  re-authenticating.

This module is pure storage — the rotation *policy* (when to mint, the Discord
push, the ``$AWM_PEER_CRED`` file) lives in :mod:`awm.auth.service`.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from awm.persistence.databases import get_connection, init_service_db

SERVICE = "auth"
_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE awm_secret (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    secret TEXT NOT NULL
);
CREATE TABLE awm_credentials (
    generation     INTEGER PRIMARY KEY AUTOINCREMENT,
    login_password TEXT NOT NULL,
    peer_credential TEXT NOT NULL,
    minted_at      REAL NOT NULL,
    expires_at     REAL NOT NULL
);
"""


def init() -> None:
    """Create the auth DB (idempotent)."""
    init_service_db(SERVICE, _SCHEMA, schema_version=_SCHEMA_VERSION)


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


# --- credential generations ------------------------------------------------


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "generation": row["generation"],
        "login_password": row["login_password"],
        "peer_credential": row["peer_credential"],
        "minted_at": row["minted_at"],
        "expires_at": row["expires_at"],
    }


def latest() -> dict[str, Any] | None:
    """The newest credential generation, or ``None`` if none minted yet."""
    conn = get_connection(SERVICE)
    try:
        row = conn.execute(
            "SELECT * FROM awm_credentials ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        return _row_to_dict(row) if row is not None else None
    finally:
        conn.close()


def valid_generations(now: float | None = None) -> list[dict[str, Any]]:
    """Every generation still inside its validity window, newest first."""
    now = time.time() if now is None else now
    conn = get_connection(SERVICE)
    try:
        rows = conn.execute(
            "SELECT * FROM awm_credentials WHERE expires_at > ? "
            "ORDER BY generation DESC",
            (now,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def mint_generation(*, validity_seconds: float, now: float | None = None,
                    login_password: str | None = None,
                    peer_credential: str | None = None) -> dict[str, Any]:
    """Insert a fresh credential pair and return it.

    ``login_password`` is human-typed once a day, so it is kept short-ish
    (url-safe, ~16 chars); ``peer_credential`` is machine-only and long.
    """
    now = time.time() if now is None else now
    login_password = login_password or secrets.token_urlsafe(12)
    peer_credential = peer_credential or secrets.token_urlsafe(32)
    expires_at = now + validity_seconds
    conn = get_connection(SERVICE)
    try:
        cur = conn.execute(
            "INSERT INTO awm_credentials "
            "(login_password, peer_credential, minted_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (login_password, peer_credential, now, expires_at),
        )
        conn.commit()
        gen = cur.lastrowid
    finally:
        conn.close()
    return {
        "generation": gen,
        "login_password": login_password,
        "peer_credential": peer_credential,
        "minted_at": now,
        "expires_at": expires_at,
    }


def prune_expired(now: float | None = None) -> int:
    """Delete generations whose validity window has fully elapsed. Returns the
    number removed. Keeps every still-valid generation (the overlapping pair)."""
    now = time.time() if now is None else now
    conn = get_connection(SERVICE)
    try:
        cur = conn.execute("DELETE FROM awm_credentials WHERE expires_at <= ?", (now,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
