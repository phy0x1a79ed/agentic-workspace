"""Messaging service — scoped message queues for inter-agent communication."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from awm.db import get_connection
from awm.models import (
    MessageSendRequest,
    MessageInfo,
    MessageSearchResponse,
    MessageActionResponse,
)
from awm.services import tasks

# Valid scope patterns
_SCOPE_RE = re.compile(r"^(workspace|project:[a-zA-Z0-9_-]+|task:[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)$")


def _validate_scope(scope: str) -> None:
    if not _SCOPE_RE.match(scope):
        raise ValueError(
            f"Invalid scope '{scope}'. Must be 'workspace', 'project:X', or 'task:X/Y'."
        )


def _row_to_info(row) -> MessageInfo:
    return MessageInfo(
        id=row["id"],
        scope=row["scope"],
        sender=row["sender"],
        msg_type=row["msg_type"],
        subject=row["subject"],
        body=row["body"],
        metadata=row["metadata"],
        status=row["status"],
        created_at=row["created_at"],
        read_at=row["read_at"],
    )


def send_message(req: MessageSendRequest) -> MessageActionResponse:
    """Send a message to a scope."""
    _validate_scope(req.scope)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO messages (scope, sender, msg_type, subject, body, metadata, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'unread', ?)",
            (req.scope, req.sender, req.msg_type, req.subject, req.body, req.metadata, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return MessageActionResponse(
        message=f"Message sent to {req.scope}",
        msg=_row_to_info(row),
    )


def search_messages(
    scope: str | None = None,
    status: str | None = None,
    msg_type: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> MessageSearchResponse:
    """Search/filter messages."""
    conn = get_connection()
    try:
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list = []
        if scope:
            _validate_scope(scope)
            sql += " AND scope = ?"
            params.append(scope)
        if status:
            sql += " AND status = ?"
            params.append(status)
        if msg_type:
            sql += " AND msg_type = ?"
            params.append(msg_type)
        if query:
            sql += " AND (subject LIKE ? OR body LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like])
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    msgs = [_row_to_info(r) for r in rows]
    return MessageSearchResponse(messages=msgs, total=len(msgs))


def read_inbox(
    scope: str,
    status: str | None = "unread",
    limit: int = 50,
) -> MessageSearchResponse:
    """Read messages from a scoped inbox and mark unread ones as read."""
    _validate_scope(scope)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        sql = "SELECT * FROM messages WHERE scope = ?"
        params: list = [scope]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        # Mark fetched unread messages as read
        unread_ids = [r["id"] for r in rows if r["status"] == "unread"]
        if unread_ids:
            placeholders = ",".join("?" * len(unread_ids))
            conn.execute(
                f"UPDATE messages SET status = 'read', read_at = ? WHERE id IN ({placeholders})",
                [now, *unread_ids],
            )
            conn.commit()
            # Re-fetch to return updated rows
            rows = conn.execute(
                f"SELECT * FROM messages WHERE id IN ({placeholders}) ORDER BY created_at DESC",
                unread_ids,
            ).fetchall()
    finally:
        conn.close()
    msgs = [_row_to_info(r) for r in rows]
    return MessageSearchResponse(messages=msgs, total=len(msgs))


def list_recipients() -> list[str]:
    """Return valid recipient scopes for all projects and tasks."""
    from awm.config import REPOS_DIR

    scopes = ["workspace"]
    projects_seen: set[str] = set()

    # Discover projects from filesystem
    if REPOS_DIR.is_dir():
        for child in sorted(REPOS_DIR.iterdir()):
            if child.is_dir() and (child / ".bare").is_dir():
                scopes.append(f"project:{child.name}")
                projects_seen.add(child.name)

    # Add ALL tasks (any status) as recipients — DISTINCT by (project, task)
    result = tasks.list_tasks(status="all")
    tasks_seen: set[str] = set()
    for t in result.tasks:
        if t.project not in projects_seen:
            scopes.append(f"project:{t.project}")
            projects_seen.add(t.project)
        task_key = f"{t.project}/{t.task}"
        if task_key not in tasks_seen:
            scopes.append(f"task:{task_key}")
            tasks_seen.add(task_key)

    return scopes
