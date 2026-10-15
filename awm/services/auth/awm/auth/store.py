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

* **User accounts** — ``awm_users`` holds one scrypt-hashed static password
  per username (set by the admin CLI, never rotated), and ``awm_login_fail``
  the failed-attempt counters that back the lockout, keyed ``u:<name>`` and
  ``ip:<addr>``.

* **Penpot credentials** — ``awm_penpot`` holds, per awm username, the Penpot
  profile awm signs in as on that person's behalf. Stored in the clear, not
  hashed: this is a credential awm must *present* to a foreign service, not one
  it verifies. The password is also what a rotation offers Penpot as its *old*
  password, so this table and Penpot's own profile row are two halves of one
  fact — see :mod:`awm.auth.penpot` for what to do when they disagree.

This module is pure storage — the rotation *policy* (when to mint, the Discord
push, the ``$AWM_PEER_CRED`` file), password hashing and the lockout policy live
in :mod:`awm.auth.service`.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from awm.persistence.databases import get_connection, init_service_db

SERVICE = "auth"
_SCHEMA_VERSION = 3

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
CREATE TABLE awm_users (
    username   TEXT PRIMARY KEY,
    pw_hash    TEXT NOT NULL,
    pw_salt    TEXT NOT NULL,
    pw_params  TEXT NOT NULL,
    created_at REAL NOT NULL,
    disabled   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE awm_login_fail (
    key          TEXT PRIMARY KEY,
    fails        INTEGER NOT NULL DEFAULT 0,
    locked_until REAL NOT NULL DEFAULT 0,
    last_at      REAL NOT NULL DEFAULT 0
);
CREATE TABLE awm_penpot (
    username   TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    password   TEXT NOT NULL,
    rotated_at REAL NOT NULL,
    created_at REAL NOT NULL
);
"""

_PENPOT_TABLE = """\
CREATE TABLE IF NOT EXISTS awm_penpot (
    username   TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    password   TEXT NOT NULL,
    rotated_at REAL NOT NULL,
    created_at REAL NOT NULL
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
    (2, 3): _PENPOT_TABLE,
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


# --- user accounts ---------------------------------------------------------


def _user_row(row: Any) -> dict[str, Any]:
    return {
        "username": row["username"],
        "pw_hash": row["pw_hash"],
        "pw_salt": row["pw_salt"],
        "pw_params": row["pw_params"],
        "created_at": row["created_at"],
        "disabled": bool(row["disabled"]),
    }


def user_get(username: str) -> dict[str, Any] | None:
    conn = get_connection(SERVICE)
    try:
        row = conn.execute(
            "SELECT * FROM awm_users WHERE username = ?", (username,)).fetchone()
        return _user_row(row) if row is not None else None
    finally:
        conn.close()


def user_list() -> list[dict[str, Any]]:
    conn = get_connection(SERVICE)
    try:
        rows = conn.execute("SELECT * FROM awm_users ORDER BY username").fetchall()
        return [_user_row(r) for r in rows]
    finally:
        conn.close()


def user_upsert(username: str, *, pw_hash: str, pw_salt: str, pw_params: str,
                now: float | None = None) -> dict[str, Any]:
    """Create ``username`` or replace its password material. Keeps ``disabled``
    and ``created_at`` on an existing row."""
    now = time.time() if now is None else now
    conn = get_connection(SERVICE)
    try:
        conn.execute(
            "INSERT INTO awm_users (username, pw_hash, pw_salt, pw_params, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET pw_hash = excluded.pw_hash, "
            "pw_salt = excluded.pw_salt, pw_params = excluded.pw_params",
            (username, pw_hash, pw_salt, pw_params, now),
        )
        conn.commit()
    finally:
        conn.close()
    return user_get(username)  # type: ignore[return-value]


def user_set_disabled(username: str, disabled: bool) -> bool:
    conn = get_connection(SERVICE)
    try:
        cur = conn.execute(
            "UPDATE awm_users SET disabled = ? WHERE username = ?",
            (1 if disabled else 0, username))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- login-failure counters --------------------------------------------------


def fail_get(key: str) -> dict[str, Any]:
    conn = get_connection(SERVICE)
    try:
        row = conn.execute(
            "SELECT * FROM awm_login_fail WHERE key = ?", (key,)).fetchone()
        if row is None:
            return {"key": key, "fails": 0, "locked_until": 0.0, "last_at": 0.0}
        return {"key": key, "fails": row["fails"],
                "locked_until": row["locked_until"], "last_at": row["last_at"]}
    finally:
        conn.close()


def fail_record(key: str, *, threshold: int, lock_seconds: float,
                now: float | None = None) -> dict[str, Any]:
    """Count one failed attempt on ``key``. Reaching ``threshold`` locks the key
    for ``lock_seconds`` and resets the counter for the next window."""
    now = time.time() if now is None else now
    cur = fail_get(key)
    fails = cur["fails"] + 1
    locked_until = cur["locked_until"]
    if fails >= threshold:
        locked_until = now + lock_seconds
        fails = 0
    conn = get_connection(SERVICE)
    try:
        conn.execute(
            "INSERT INTO awm_login_fail (key, fails, locked_until, last_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET fails = excluded.fails, "
            "locked_until = excluded.locked_until, last_at = excluded.last_at",
            (key, fails, locked_until, now))
        conn.commit()
    finally:
        conn.close()
    return {"key": key, "fails": fails, "locked_until": locked_until, "last_at": now}


def fail_clear(key: str) -> None:
    conn = get_connection(SERVICE)
    try:
        conn.execute("DELETE FROM awm_login_fail WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


# --- Penpot credentials ------------------------------------------------------


def _penpot_row(row: Any) -> dict[str, Any]:
    return {
        "username": row["username"],
        "email": row["email"],
        "password": row["password"],
        "rotated_at": row["rotated_at"],
        "created_at": row["created_at"],
    }


def penpot_get(username: str) -> dict[str, Any] | None:
    conn = get_connection(SERVICE)
    try:
        row = conn.execute(
            "SELECT * FROM awm_penpot WHERE username = ?", (username,)).fetchone()
        return _penpot_row(row) if row is not None else None
    finally:
        conn.close()


def penpot_list() -> list[dict[str, Any]]:
    conn = get_connection(SERVICE)
    try:
        rows = conn.execute("SELECT * FROM awm_penpot ORDER BY username").fetchall()
        return [_penpot_row(r) for r in rows]
    finally:
        conn.close()


def penpot_upsert(username: str, *, email: str, password: str,
                  now: float | None = None) -> dict[str, Any]:
    """Record the Penpot credential awm holds for ``username``.

    Replaces both the email and the password on an existing row: re-recording
    is how a credential that drifted out of step with Penpot's own profile row
    is put back, and that repair is worthless if it cannot overwrite.
    """
    now = time.time() if now is None else now
    conn = get_connection(SERVICE)
    try:
        conn.execute(
            "INSERT INTO awm_penpot (username, email, password, rotated_at, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET email = excluded.email, "
            "password = excluded.password, rotated_at = excluded.rotated_at",
            (username, email, password, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return penpot_get(username)  # type: ignore[return-value]


def penpot_set_password(username: str, password: str,
                        now: float | None = None) -> bool:
    """Store a rotated password. Called only after Penpot has confirmed it."""
    now = time.time() if now is None else now
    conn = get_connection(SERVICE)
    try:
        cur = conn.execute(
            "UPDATE awm_penpot SET password = ?, rotated_at = ? WHERE username = ?",
            (password, now, username))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def penpot_delete(username: str) -> bool:
    conn = get_connection(SERVICE)
    try:
        cur = conn.execute("DELETE FROM awm_penpot WHERE username = ?", (username,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
