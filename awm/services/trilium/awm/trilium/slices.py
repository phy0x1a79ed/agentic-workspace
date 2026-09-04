"""Slice tokens: minting, listing, revoking and resolving.

A slice is a link that opens one note and its descendants to somebody with no
awm account, optionally letting them edit note bodies. The token is a
credential, so it lives in this service's own SQLite DB rather than on a note
-- a note anyone signed in can read would otherwise leak the very thing that
stands in for a password.

Two shapes fall out of one table. A **bound** token carries a visitor name
baked in at mint time (`user` is set); an **open** token carries none, and the
visitor's name comes from `?user=` on first arrival, which the edge remembers
in a cookie scoped to the slice's own path -- nothing here needs to know it.

`resolve` collapses "no such token", "revoked" and "expired" into the same
`None` on purpose: the edge turns that into a 404, and a slice that no longer
exists must look exactly like a URL that never did.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from awm.persistence.databases import get_connection, init_service_db

SERVICE = "trilium"
_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE slices (
    token      TEXT PRIMARY KEY,
    note_id    TEXT NOT NULL,
    user       TEXT,
    write      INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL,
    revoked_at REAL
);
CREATE INDEX idx_slices_note ON slices(note_id);
"""


def init() -> None:
    """Create the trilium service's own tables (idempotent)."""
    init_service_db(SERVICE, _SCHEMA, schema_version=_SCHEMA_VERSION)


def new_token() -> str:
    """A token safe to carry as one URL path segment: url-safe base64, which
    never contains `/`."""
    return secrets.token_urlsafe(32)


def _row(r: Any, *, now: float) -> dict[str, Any]:
    return {
        "token": r["token"], "note_id": r["note_id"], "user": r["user"],
        "write": bool(r["write"]), "created_at": r["created_at"],
        "expires_at": r["expires_at"], "revoked_at": r["revoked_at"],
        "active": r["revoked_at"] is None
                  and (r["expires_at"] is None or r["expires_at"] > now),
    }


def mint(*, note_id: str, user: str | None = None, write: bool = False,
         expires_at: float | None = None, now: float | None = None) -> dict[str, Any]:
    """Mint a fresh token for `note_id` and record it. Never reuses a token,
    so an old link cannot be resurrected by minting a new one."""
    now = time.time() if now is None else now
    token = new_token()
    conn = get_connection(SERVICE)
    try:
        conn.execute(
            "INSERT INTO slices (token, note_id, user, write, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token, note_id, user, 1 if write else 0, now, expires_at))
        conn.commit()
    finally:
        conn.close()
    return {"token": token, "note_id": note_id, "user": user, "write": write,
            "created_at": now, "expires_at": expires_at, "revoked_at": None,
            "active": True}


def list_all(note_id: str | None = None, *,
            now: float | None = None) -> list[dict[str, Any]]:
    """Every slice ever minted, newest first. `note_id` restricts to one note;
    otherwise every slice on the vault, active and revoked alike -- an
    operator audit, not a live-only view."""
    now = time.time() if now is None else now
    conn = get_connection(SERVICE)
    try:
        if note_id:
            rows = conn.execute(
                "SELECT * FROM slices WHERE note_id = ? ORDER BY created_at DESC",
                (note_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM slices ORDER BY created_at DESC").fetchall()
        return [_row(r, now=now) for r in rows]
    finally:
        conn.close()


def list_active(note_id: str, *, now: float | None = None) -> list[dict[str, Any]]:
    """The slices on `note_id` that still resolve -- neither revoked nor
    expired. `slice_revoke` clears the note's `#sliced` label only when this
    comes back empty: a note may carry more than one live slice."""
    return [r for r in list_all(note_id, now=now) if r["active"]]


def revoke(token: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Mark `token` revoked. Returns the row -- idempotent on one already
    revoked -- or `None` if no such token was ever minted."""
    now = time.time() if now is None else now
    conn = get_connection(SERVICE)
    try:
        row = conn.execute(
            "SELECT * FROM slices WHERE token = ?", (token,)).fetchone()
        if row is None:
            return None
        if row["revoked_at"] is None:
            conn.execute(
                "UPDATE slices SET revoked_at = ? WHERE token = ?", (now, token))
            conn.commit()
            row = conn.execute(
                "SELECT * FROM slices WHERE token = ?", (token,)).fetchone()
        return _row(row, now=now)
    finally:
        conn.close()


def resolve(token: str, *, now: float | None = None) -> dict[str, Any] | None:
    """The live slice `token` names, or `None` for unknown, revoked or
    expired -- deliberately the same `None` for all three. See the module
    docstring for why that collapse matters."""
    now = time.time() if now is None else now
    conn = get_connection(SERVICE)
    try:
        row = conn.execute(
            "SELECT * FROM slices WHERE token = ?", (token,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    out = _row(row, now=now)
    return out if out["active"] else None
