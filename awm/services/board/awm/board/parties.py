"""Parties: who may use the board, and the one rule about who sees a card.

A party is one bearer token held by a swarm or a sovereign. The board stores
only the token's SHA-256 hash, in a small SQLite table that never leaves this
host. The vault replicates to other machines and crosses Cloudflare in
plaintext, so a token must never be written there.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from awm.persistence import databases

RELATIONS = ("domestic", "foreign", "sovereign")

#: The recipient that any swarm may claim. Never a valid swarm name.
OPEN = "open"

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS parties (
    party_id   TEXT PRIMARY KEY,
    swarm      TEXT NOT NULL,
    principal  TEXT NOT NULL,
    relation   TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
"""


class NotFound(Exception):
    """The card or party does not exist, or the caller may not see it."""


class Conflict(Exception):
    """The card is in a state that refuses this transition (HTTP 409)."""


class Forbidden(Exception):
    """The caller sees the card but may not do this to it."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def can_see(party: dict, card: dict) -> bool:
    """A party sees the cards its swarm sent, cards addressed to its swarm, and open cards.

    The swarm is the unit, not the single bearer: a swarm that rotates a token
    keeps sight of what it posted earlier.
    """
    swarm = party["swarm"]
    return (
        card["sender"]["swarm"] == swarm
        or card["recipient"] == swarm
        or card["recipient"] == OPEN
    )


def _row(r: sqlite3.Row) -> dict:
    return {
        "party_id": r["party_id"],
        "swarm": r["swarm"],
        "principal": r["principal"],
        "relation": r["relation"],
        "token_hash": r["token_hash"],
        "revoked": bool(r["revoked"]),
    }


class Parties:
    """The party table, in a SQLite file at ``path``."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._write = threading.Lock()
        databases.init_db_at(self._path, _SCHEMA, schema_version=1)

    def _conn(self) -> sqlite3.Connection:
        return databases.get_connection_at(self._path)

    def add(self, swarm: str, principal: str, relation: str) -> tuple[dict, str]:
        """Mint a party. The plaintext token is returned once and never stored."""
        if not _SLUG.match(swarm or "") or swarm == OPEN:
            raise ValueError(f"swarm must be a lowercase slug other than {OPEN!r}: {swarm!r}")
        if not _SLUG.match(principal or ""):
            raise ValueError(f"principal must be a lowercase slug: {principal!r}")
        if relation not in RELATIONS:
            raise ValueError(f"relation must be one of {RELATIONS}: {relation!r}")
        token = secrets.token_urlsafe(32)
        party_id = databases.new_uuid()
        conn = self._conn()
        try:
            with self._write:
                conn.execute(
                    "INSERT INTO parties (party_id, swarm, principal, relation,"
                    " token_hash, revoked, created_at) VALUES (?,?,?,?,?,0,?)",
                    (party_id, swarm, principal, relation, hash_token(token), time.time()),
                )
                conn.commit()
            row = conn.execute(
                "SELECT * FROM parties WHERE party_id=?", (party_id,)
            ).fetchone()
        finally:
            conn.close()
        return _row(row), token

    def revoke(self, party_id: str) -> dict:
        conn = self._conn()
        try:
            with self._write:
                cur = conn.execute(
                    "UPDATE parties SET revoked=1 WHERE party_id=?", (party_id,)
                )
                conn.commit()
            if cur.rowcount == 0:
                raise NotFound(f"no party {party_id}")
            row = conn.execute(
                "SELECT * FROM parties WHERE party_id=?", (party_id,)
            ).fetchone()
        finally:
            conn.close()
        return _row(row)

    def resolve(self, token: str | None) -> dict | None:
        """The live party that holds ``token``, or None for an unknown or revoked one."""
        if not token or not isinstance(token, str):
            return None
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM parties WHERE token_hash=? AND revoked=0",
                (hash_token(token),),
            ).fetchone()
        finally:
            conn.close()
        return _row(row) if row else None

    def list(self) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM parties ORDER BY created_at, party_id"
            ).fetchall()
        finally:
            conn.close()
        return [_row(r) for r in rows]
