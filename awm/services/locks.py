"""Lock acquire/release/heartbeat/reap with file + folder semantics."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from awm.db import get_connection
from awm.config import HEARTBEAT_STALE_THRESHOLD
from awm.models import (
    LockAcquireRequest,
    LockInfo,
    LockListResponse,
    LockActionResponse,
)


def _row_to_info(row: sqlite3.Row) -> LockInfo:
    return LockInfo(
        id=row["id"],
        resource_path=row["resource_path"],
        holder_id=row["holder_id"],
        holder_pid=row["holder_pid"],
        lock_type=row["lock_type"],
        acquired_at=row["acquired_at"],
        heartbeat_at=row["heartbeat_at"],
        metadata=row["metadata"],
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int) -> bool:
    """Check if a PID is alive (Linux /proc check)."""
    return os.path.isdir(f"/proc/{pid}")


def _find_conflicts(
    conn: sqlite3.Connection,
    resource_path: str,
    holder_id: str,
    lock_type: str,
) -> list[LockInfo]:
    """Find existing locks that conflict with a requested lock."""
    rows = conn.execute(
        """
        SELECT * FROM locks WHERE
            (resource_path = ?1) OR
            (?1 LIKE resource_path || '%') OR
            (resource_path LIKE ?1 || '%')
        """,
        (resource_path,),
    ).fetchall()

    conflicts = []
    for row in rows:
        # Same holder on same path — idempotent re-acquire
        if row["holder_id"] == holder_id and row["resource_path"] == resource_path:
            continue
        # Two shared locks on the exact same path are fine
        if lock_type == "shared" and row["lock_type"] == "shared" and row["resource_path"] == resource_path:
            continue
        # Everything else is a conflict
        conflicts.append(_row_to_info(row))
    return conflicts


def acquire(req: LockAcquireRequest) -> LockActionResponse:
    """Acquire a lock, failing if there's a conflict."""
    conn = get_connection()
    try:
        conflicts = _find_conflicts(conn, req.resource_path, req.holder_id, req.lock_type)
        if conflicts:
            holders = ", ".join(f"{c.holder_id} ({c.resource_path})" for c in conflicts)
            return LockActionResponse(
                message=f"Conflict: resource locked by {holders}",
                lock=None,
            )

        now = _now_iso()
        # Upsert — if same holder re-acquires, update heartbeat
        conn.execute(
            """
            INSERT INTO locks (resource_path, holder_id, holder_pid, lock_type, acquired_at, heartbeat_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(resource_path, holder_id) DO UPDATE SET
                heartbeat_at = excluded.heartbeat_at,
                holder_pid = excluded.holder_pid,
                lock_type = excluded.lock_type,
                metadata = excluded.metadata
            """,
            (req.resource_path, req.holder_id, req.holder_pid, req.lock_type, now, now, req.metadata),
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM locks WHERE resource_path = ? AND holder_id = ?",
            (req.resource_path, req.holder_id),
        ).fetchone()

        return LockActionResponse(
            message="Lock acquired",
            lock=_row_to_info(row),
        )
    finally:
        conn.close()


def release(resource_path: str, holder_id: str) -> LockActionResponse:
    """Release a specific lock."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM locks WHERE resource_path = ? AND holder_id = ?",
            (resource_path, holder_id),
        ).fetchone()
        if row is None:
            return LockActionResponse(message="No matching lock found")

        info = _row_to_info(row)
        conn.execute(
            "DELETE FROM locks WHERE resource_path = ? AND holder_id = ?",
            (resource_path, holder_id),
        )
        conn.commit()
        return LockActionResponse(message="Lock released", lock=info)
    finally:
        conn.close()


def heartbeat(holder_id: str) -> LockActionResponse:
    """Renew heartbeat for all locks held by a given holder."""
    conn = get_connection()
    try:
        now = _now_iso()
        cursor = conn.execute(
            "UPDATE locks SET heartbeat_at = ? WHERE holder_id = ?",
            (now, holder_id),
        )
        conn.commit()
        return LockActionResponse(
            message=f"Heartbeat renewed for {cursor.rowcount} lock(s)",
        )
    finally:
        conn.close()


def list_locks(holder_id: str | None = None, path: str | None = None) -> LockListResponse:
    """List active locks, optionally filtered."""
    conn = get_connection()
    try:
        query = "SELECT * FROM locks WHERE 1=1"
        params: list = []
        if holder_id:
            query += " AND holder_id = ?"
            params.append(holder_id)
        if path:
            query += " AND resource_path = ?"
            params.append(path)
        query += " ORDER BY acquired_at"

        rows = conn.execute(query, params).fetchall()
        locks = [_row_to_info(r) for r in rows]
        return LockListResponse(locks=locks, total=len(locks))
    finally:
        conn.close()


def reap_stale() -> int:
    """Remove stale locks. Returns count of reaped locks."""
    conn = get_connection()
    try:
        now = datetime.now(timezone.utc)
        rows = conn.execute("SELECT * FROM locks").fetchall()
        reaped = 0
        for row in rows:
            stale = False
            # Check PID first (instant reap on crash)
            if row["holder_pid"] and not _pid_alive(row["holder_pid"]):
                stale = True
            else:
                # Check heartbeat age
                hb = datetime.fromisoformat(row["heartbeat_at"])
                if hb.tzinfo is None:
                    hb = hb.replace(tzinfo=timezone.utc)
                age = (now - hb).total_seconds()
                if age > HEARTBEAT_STALE_THRESHOLD:
                    stale = True

            if stale:
                conn.execute("DELETE FROM locks WHERE id = ?", (row["id"],))
                reaped += 1

        conn.commit()
        return reaped
    finally:
        conn.close()
