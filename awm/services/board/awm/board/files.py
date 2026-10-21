"""Files on a card: bytes a party attaches, kept on the host beside the event log.

A file is content-addressed: its bytes live once under ``blobs/<sha256>``, and a
row in ``files.db`` records which card it was attached to, by whom, under what
name. The vault never holds the bytes, because it replicates to other machines
and crosses Cloudflare in plaintext. A file is visible to exactly the parties
that may see its card, and it is pruned with the event log's retention.

The sender states the sha256 and the board recomputes it, so a truncated or
altered upload is refused rather than stored.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from awm.persistence import databases

#: Largest file a party may attach. The edge's body cap for the upload route is this plus nothing.
DEFAULT_MAX_BYTES = 16 * 1024 * 1024
#: Bytes all live files may hold together.
DEFAULT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024

_SHA = re.compile(r"^[0-9a-f]{64}$")
_NAME_BAD = re.compile(r"[^A-Za-z0-9._ +=@,-]")
MAX_NAME = 120

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id   TEXT PRIMARY KEY,
    card_id   TEXT NOT NULL,
    name      TEXT NOT NULL,
    size      INTEGER NOT NULL,
    sha256    TEXT NOT NULL,
    swarm     TEXT NOT NULL,
    principal TEXT NOT NULL,
    party     TEXT NOT NULL,
    agent     TEXT,
    node      TEXT,
    at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS files_card ON files (card_id, at);
"""


class TooLarge(ValueError):
    """The file, or the store with it, would pass its cap."""


def clean_name(name: str | None) -> str:
    """A file name safe to show and to write anywhere: one path segment, no leading dot."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = _NAME_BAD.sub("_", base).lstrip(".")[:MAX_NAME]
    return base or "file"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _public(row: sqlite3.Row) -> dict:
    return {"file_id": row["file_id"], "card_id": row["card_id"], "name": row["name"],
            "size": row["size"], "sha256": row["sha256"],
            "sender": {"swarm": row["swarm"], "principal": row["principal"], "party": row["party"]},
            "agent": row["agent"], "node": row["node"], "at": _iso(row["at"])}


class FileStore:
    """The files table and the blob directory under ``directory``."""

    def __init__(self, directory: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 total_bytes: int = DEFAULT_TOTAL_BYTES) -> None:
        self.directory = Path(directory)
        self.blobs = self.directory / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.total_bytes = total_bytes
        self._path = self.directory / "files.db"
        databases.init_db_at(self._path, _SCHEMA, schema_version=1)

    def _conn(self) -> sqlite3.Connection:
        return databases.get_connection_at(self._path)

    def used(self) -> int:
        conn = self._conn()
        try:
            row = conn.execute("SELECT COALESCE(SUM(size), 0) AS n FROM"
                               " (SELECT DISTINCT sha256, size FROM files)").fetchone()
        finally:
            conn.close()
        return int(row["n"])

    def put(self, card_id: str, name: str, data: bytes, sha256: str, sender: dict, *,
            agent: str | None = None, node: str | None = None) -> dict:
        """Store ``data`` as a file on the card. Refuses a hash that does not match the bytes."""
        claimed = str(sha256 or "").strip().lower()
        if not _SHA.match(claimed):
            raise ValueError("sha256 must be 64 hex digits")
        if not data:
            raise ValueError("the file is empty")
        if len(data) > self.max_bytes:
            raise TooLarge(f"a file may be at most {self.max_bytes} bytes")
        actual = hashlib.sha256(data).hexdigest()
        if actual != claimed:
            raise ValueError("sha256 does not match the bytes received")
        blob = self.blobs / actual
        if not blob.exists():
            if self.used() + len(data) > self.total_bytes:
                raise TooLarge("the board's file store is full")
            tmp = blob.with_suffix(f".{uuid.uuid4().hex}.part")
            tmp.write_bytes(data)
            os.chmod(tmp, 0o400)
            os.replace(tmp, blob)
        file_id = uuid.uuid4().hex
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO files (file_id, card_id, name, size, sha256, swarm, principal,"
                " party, agent, node, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (file_id, card_id, clean_name(name), len(data), actual, sender["swarm"],
                 sender["principal"], sender["party"], agent, node, time.time()))
            conn.commit()
            row = conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
        finally:
            conn.close()
        return _public(row)

    def list(self, card_id: str) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM files WHERE card_id = ? ORDER BY at",
                                (card_id,)).fetchall()
        finally:
            conn.close()
        return [_public(r) for r in rows]

    def get(self, card_id: str, file_id: str) -> tuple[dict, Path] | None:
        """The file's record and its blob, or None when the card holds no such file."""
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM files WHERE file_id = ? AND card_id = ?",
                               (file_id, card_id)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        blob = self.blobs / row["sha256"]
        return (_public(row), blob) if blob.exists() else None

    def prune(self, days: float) -> int:
        """Forget files older than ``days`` and delete blobs no file names. Returns files removed."""
        cutoff = time.time() - days * 86400
        conn = self._conn()
        try:
            removed = conn.execute("DELETE FROM files WHERE at < ?", (cutoff,)).rowcount
            live = {r["sha256"] for r in conn.execute("SELECT DISTINCT sha256 FROM files")}
            conn.commit()
        finally:
            conn.close()
        for blob in self.blobs.iterdir():
            if blob.name not in live and _SHA.match(blob.name):
                blob.unlink(missing_ok=True)
        return removed
