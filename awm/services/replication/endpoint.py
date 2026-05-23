"""Federated endpoints for cr-sqlite replication.

``GET /peer/db-sync?since=<db_version>`` returns the local cr-sqlite
changesets at or after ``db_version``, JSON-encoded with binary blobs
base64ed. The remote peer applies them via ``INSERT INTO crsql_changes``.

Auth is the standard peer-bearer + ``X-Awm-From`` cross-check; the route is
registered on the existing /peer/* router from :mod:`awm.api.peer`.
"""

from __future__ import annotations

import base64
import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from awm.db import get_connection
from awm.middleware_auth import require_peer_bearer
from awm.services.replication import schema as repl_schema

log = logging.getLogger("awm.replication.endpoint")


router = APIRouter(prefix="/peer", tags=["peer-replication"])


def _b64(val: Any) -> Any:
    """Encode bytes as base64 strings; pass through other types unchanged."""
    if isinstance(val, (bytes, bytearray, memoryview)):
        return {"_b": base64.b64encode(bytes(val)).decode("ascii")}
    return val


def _decode(val: Any) -> Any:
    """Inverse of :func:`_b64`. Accepts plain values too."""
    if isinstance(val, dict) and set(val.keys()) == {"_b"}:
        return base64.b64decode(val["_b"])
    return val


@router.get("/db-sync", dependencies=[Depends(require_peer_bearer)])
def get_db_sync(
    request: Request,
    since: int = Query(0, ge=0, description="cr-sqlite db_version cursor"),
    limit: int = Query(5000, ge=1, le=50000),
) -> dict:
    """Return cr-sqlite changes from ``crsql_changes`` strictly after
    ``since``. Response shape::

        {
            "site_id":   "<b64 of this peer's crsql_site_id>",
            "db_version": <int — max db_version in this batch>,
            "changes": [
                {"table": "rooms", "pk": {"_b":...}, "cid":"...",
                 "val":..., "col_version":N, "db_version":N,
                 "site_id":{"_b":...}, "cl":N, "seq":N},
                ...
            ],
            "more": bool,
        }
    """
    if not repl_schema.crsqlite_available():
        raise HTTPException(503, "cr-sqlite extension not loaded on this peer")
    conn = get_connection()
    try:
        # Local site id (so the caller can avoid sending its own site's
        # changes back to it via filtering).
        try:
            site_row = conn.execute("SELECT crsql_site_id()").fetchone()
            site_id_blob = bytes(site_row[0]) if site_row else b""
        except sqlite3.OperationalError as exc:
            raise HTTPException(503, f"cr-sqlite not active: {exc}")

        try:
            rows = conn.execute(
                'SELECT "table", pk, cid, val, col_version, db_version, site_id, cl, seq '
                'FROM crsql_changes WHERE db_version > ? '
                'ORDER BY db_version ASC, seq ASC LIMIT ?',
                (since, limit + 1),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise HTTPException(503, f"crsql_changes unavailable: {exc}")

        more = len(rows) > limit
        rows = rows[:limit]
        changes = [
            {
                "table": r["table"],
                "pk": _b64(r["pk"]),
                "cid": r["cid"],
                "val": _b64(r["val"]),
                "col_version": r["col_version"],
                "db_version": r["db_version"],
                "site_id": _b64(r["site_id"]),
                "cl": r["cl"],
                "seq": r["seq"],
            }
            for r in rows
        ]
        max_db_version = max((c["db_version"] for c in changes), default=since)
    finally:
        try:
            repl_schema.finalize(conn)
        finally:
            conn.close()
    return {
        "site_id": base64.b64encode(site_id_blob).decode("ascii"),
        "db_version": max_db_version,
        "changes": changes,
        "more": more,
    }


def apply_changes(conn: sqlite3.Connection, changes: list[dict]) -> int:
    """Apply a peer's ``changes`` to this connection's cr-sqlite. Returns
    the number applied (skipping any rows whose ``site_id`` matches ours —
    those are echoes of our own writes)."""
    if not changes:
        return 0
    site_row = conn.execute("SELECT crsql_site_id()").fetchone()
    self_site = bytes(site_row[0]) if site_row else b""
    applied = 0
    for c in changes:
        c_site = _decode(c.get("site_id", b""))
        if isinstance(c_site, bytes) and c_site == self_site:
            continue
        conn.execute(
            'INSERT INTO crsql_changes ("table", pk, cid, val, col_version, '
            'db_version, site_id, cl, seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                c["table"],
                _decode(c.get("pk")),
                c["cid"],
                _decode(c.get("val")),
                c["col_version"],
                c["db_version"],
                _decode(c.get("site_id", b"")),
                c.get("cl", 1),
                c.get("seq", 0),
            ),
        )
        applied += 1
    conn.commit()
    return applied
