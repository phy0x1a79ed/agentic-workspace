"""Outer-repo worktree versioning flow for shared resources."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from awm.config import WORKSPACE_ROOT, TASKS_DIR
from awm.db import get_connection
from awm.models import (
    SharedEditRequest,
    SharedEditInfo,
    SharedEditListResponse,
    SharedEditActionResponse,
)
from awm.services import locks
from awm.models import LockAcquireRequest


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _row_to_info(row) -> SharedEditInfo:
    return SharedEditInfo(
        id=row["id"],
        name=row["name"],
        worktree_path=row["worktree_path"],
        branch=row["branch"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        status=row["status"],
    )


def start_edit(req: SharedEditRequest) -> SharedEditActionResponse:
    """Start a shared resource edit: acquire lock, create branch + worktree."""
    # Acquire exclusive lock on __shared__/<name>
    lock_result = locks.acquire(LockAcquireRequest(
        resource_path=f"__shared__/{req.name}",
        holder_id=req.created_by,
        lock_type="exclusive",
    ))
    if lock_result.lock is None:
        return SharedEditActionResponse(
            message=f"Cannot start edit: {lock_result.message}",
        )

    branch = f"shared/{req.name}"
    worktree_path = TASKS_DIR / "_shared" / req.name

    # Create branch from current HEAD of the outer repo
    r = _run(["git", "-C", str(WORKSPACE_ROOT), "branch", branch, "HEAD"])
    if r.returncode != 0:
        # Branch may already exist — try to continue
        if "already exists" not in r.stderr:
            locks.release(f"__shared__/{req.name}", req.created_by)
            raise RuntimeError(f"Failed to create branch: {r.stderr}")

    # Create worktree
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    r = _run(["git", "-C", str(WORKSPACE_ROOT), "worktree", "add",
              str(worktree_path), branch])
    if r.returncode != 0:
        locks.release(f"__shared__/{req.name}", req.created_by)
        # Clean up branch if worktree failed
        _run(["git", "-C", str(WORKSPACE_ROOT), "branch", "-d", branch])
        raise RuntimeError(f"Failed to create worktree: {r.stderr}")

    # Record in DB
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO shared_edits (name, worktree_path, branch, created_by, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'active')",
            (req.name, str(worktree_path), branch, req.created_by, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM shared_edits WHERE name = ?", (req.name,)).fetchone()
        return SharedEditActionResponse(
            message=f"Shared edit started. Work in: {worktree_path}",
            edit=_row_to_info(row),
        )
    finally:
        conn.close()


def merge_edit(name: str) -> SharedEditActionResponse:
    """Merge a shared resource edit back into the main working tree."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM shared_edits WHERE name = ? AND status = 'active'",
            (name,),
        ).fetchone()
        if row is None:
            return SharedEditActionResponse(message=f"No active shared edit named '{name}'")

        info = _row_to_info(row)
        branch = info.branch

        # Merge from main working tree
        r = _run(["git", "-C", str(WORKSPACE_ROOT), "merge", branch,
                  "-m", f"Merge shared edit: {name}"])
        if r.returncode != 0:
            return SharedEditActionResponse(
                message=f"Merge conflict: {r.stderr.strip()}. Resolve in {info.worktree_path} and retry.",
                edit=info,
            )

        # Clean up: remove worktree, delete branch, release lock
        _run(["git", "-C", str(WORKSPACE_ROOT), "worktree", "remove", info.worktree_path, "--force"])
        _run(["git", "-C", str(WORKSPACE_ROOT), "branch", "-d", branch])
        locks.release(f"__shared__/{name}", info.created_by)

        # Update DB
        conn.execute("UPDATE shared_edits SET status = 'merged' WHERE name = ?", (name,))
        conn.commit()

        row = conn.execute("SELECT * FROM shared_edits WHERE name = ?", (name,)).fetchone()
        return SharedEditActionResponse(
            message=f"Shared edit '{name}' merged successfully",
            edit=_row_to_info(row),
        )
    finally:
        conn.close()


def list_edits(status: str | None = None) -> SharedEditListResponse:
    """List shared edits, optionally filtered by status."""
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM shared_edits WHERE status = ? ORDER BY created_at",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM shared_edits ORDER BY created_at"
            ).fetchall()
        edits = [_row_to_info(r) for r in rows]
        return SharedEditListResponse(edits=edits, total=len(edits))
    finally:
        conn.close()
