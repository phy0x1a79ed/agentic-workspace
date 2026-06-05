"""Messaging service — agent-addressed inboxes (v37).

Recipients address the polymorphic ref vocabulary directly via
``identity.resolve_ref``:

  * ``workspace`` → the workspace's ``system`` user sentinel.
  * ``project:<name>`` → the project's catch-all user (per-project inbox,
    materialized in the users table as ``proj:<name>``).
  * ``scope:<project>/<scope>`` → the agent's uuid.
  * ``user:<name>`` → that user's uuid.

When the recipient resolves to an agent and that agent has an owned room,
``send_message`` dual-writes a ``kind='notification'`` row into the
agent's room transcript so the agent's CLI sees the notification through
the same broadcast path that delivers room posts.
"""

from __future__ import annotations

import json
import re
from typing import Any

from awm.db import get_connection
from awm.models import (
    MessageSendRequest,
    MessageInfo,
    MessagePreview,
    MessageSearchResponse,
    MessageFetchResponse,
    MessageActionResponse,
)
from awm.services import identity
from awm.services.identity import iso_to_ms, ms_to_iso, now_ms


_SCOPE_RE = re.compile(
    r"^(workspace|project:[a-zA-Z0-9_-]+|scope:[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)$"
)


def _validate_scope(scope: str) -> None:
    if not _SCOPE_RE.match(scope):
        raise ValueError(
            f"Invalid scope '{scope}'. Must be 'workspace', 'project:X', "
            f"or 'scope:X/Y'."
        )


def _project_inbox_username(name: str) -> str:
    """The catch-all user that backs ``project:<name>`` inboxes — landed
    in the users table on demand so ``recipient_id`` always FK-resolves."""
    return f"proj:{name}"


def _resolve_recipient(base_scope: str, *, conn=None) -> tuple[str, str]:
    """Resolve a ``base_scope`` string to ``(recipient_id, label)``.

    label is the canonical legacy form (``workspace`` / ``project:X`` /
    ``scope:X/Y``) preserved on the response objects so existing callers
    keep matching.
    """
    if base_scope == "workspace":
        return identity.SYSTEM_USER_ID, "workspace"
    if base_scope.startswith("project:"):
        name = base_scope.split(":", 1)[1]
        uid = identity.user_id_for_username(
            _project_inbox_username(name), conn=conn, create_if_missing=True,
        )
        return (uid or identity.SYSTEM_REF, base_scope)
    if base_scope.startswith("scope:"):
        body = base_scope.split(":", 1)[1]
        if "/" in body:
            proj, sc = body.split("/", 1)
            aid = identity.agent_id_for_scope(proj, sc, conn=conn, active_only=False)
            if aid:
                return (aid, base_scope)
    return (identity.SYSTEM_REF, base_scope)


def _resolve_sender(sender_literal: str, *, conn=None) -> str:
    """Best-effort: ``user:name`` / ``agent:proj/scope`` / ``system`` → ref.
    Unrecognized literals get treated as user names (created on demand)."""
    ref = identity.resolve_ref(sender_literal, conn=conn, create_users=True)
    return ref or identity.SYSTEM_REF


def _row_to_info(row, *, conn=None) -> MessageInfo:
    """Reconstruct legacy MessageInfo shape from a v37 row.

    The legacy ``scope`` / ``sender`` strings come from the recipient/sender
    refs: agents render as ``scope:project/scope``, users as ``user:name``,
    ``system`` as ``workspace``.
    """
    return MessageInfo(
        id=row["id"],
        scope=_label_for_recipient(row["recipient_id"], conn=conn),
        sender=_label_for_sender(row["sender_id"], conn=conn),
        msg_type=row["msg_type"],
        subject=row["subject"],
        body=row["body"],
        metadata=row["metadata"],
        status=row["status"],
        created_at=ms_to_iso(row["created_at"]) or "",
        read_at=ms_to_iso(row["read_at"]),
    )


def _row_to_preview(row, *, conn=None) -> MessagePreview:
    return MessagePreview(
        id=row["id"],
        scope=_label_for_recipient(row["recipient_id"], conn=conn),
        sender=_label_for_sender(row["sender_id"], conn=conn),
        msg_type=row["msg_type"],
        subject=row["subject"],
        status=row["status"],
        created_at=ms_to_iso(row["created_at"]) or "",
        read_at=ms_to_iso(row["read_at"]),
    )


def _label_for_recipient(ref: str, *, conn=None) -> str:
    if ref == identity.SYSTEM_REF or not ref:
        return "workspace"
    # users.id?
    own = conn is None
    c = conn or get_connection()
    try:
        urow = c.execute("SELECT username FROM users WHERE id=?", (ref,)).fetchone()
        if urow:
            name = urow[0]
            if name == "system" or ref == identity.SYSTEM_USER_ID:
                return "workspace"
            if name.startswith("proj:"):
                return f"project:{name[len('proj:'):]}"
            return f"user:{name}"
        arow = c.execute(
            "SELECT p.name, a.scope FROM agents a "
            "JOIN projects p ON p.id = a.project_id WHERE a.id=?",
            (ref,),
        ).fetchone()
        if arow:
            return f"scope:{arow[0]}/{arow[1]}"
    finally:
        if own:
            c.close()
    return ref


def _label_for_sender(ref: str, *, conn=None) -> str:
    if ref == identity.SYSTEM_REF or not ref:
        return "system"
    own = conn is None
    c = conn or get_connection()
    try:
        urow = c.execute("SELECT username FROM users WHERE id=?", (ref,)).fetchone()
        if urow:
            return f"user:{urow[0]}"
        arow = c.execute(
            "SELECT p.name, a.scope FROM agents a "
            "JOIN projects p ON p.id = a.project_id WHERE a.id=?",
            (ref,),
        ).fetchone()
        if arow:
            return f"agent:{arow[0]}/{arow[1]}"
    finally:
        if own:
            c.close()
    return ref


def _agent_owned_room_id(agent_ref: str) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM rooms WHERE owner_agent_id=? AND status='open' "
            "ORDER BY created_at DESC LIMIT 1",
            (agent_ref,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def send_message(req: MessageSendRequest) -> MessageActionResponse:
    """Send a message. Inserts into the local messages table and (when the
    recipient is an agent with an owned room) posts a notification into
    its transcript."""
    _validate_scope(req.scope)
    base_scope = req.scope

    conn = get_connection()
    try:
        recipient_id, _label = _resolve_recipient(base_scope, conn=conn)
        sender_id = _resolve_sender(req.sender, conn=conn)
        cur = conn.execute(
            "INSERT INTO messages "
            "(recipient_id, sender_id, msg_type, subject, body, metadata, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'unread', ?)",
            (recipient_id, sender_id, req.msg_type, req.subject, req.body,
             req.metadata, now_ms()),
        )
        msg_id = cur.lastrowid
        conn.commit()
        row = conn.execute(
            "SELECT * FROM messages WHERE id=?", (msg_id,),
        ).fetchone()
        info = _row_to_info(row, conn=conn)
    finally:
        conn.close()

    # Dual-write: if the recipient is an agent with an owned room, post a
    # transcript notification so the running CLI sees the ping.
    if base_scope.startswith("scope:"):
        owned_room = _agent_owned_room_id(recipient_id)
        if owned_room:
            try:
                from awm.services import rooms as rooms_svc
                rooms_svc.post_transcript(
                    owned_room, author=sender_id, kind="notification",
                    body=req.subject or req.body or "",
                    meta={"message_id": msg_id, "msg_type": req.msg_type},
                )
            except Exception:  # noqa: BLE001
                pass

    return MessageActionResponse(
        message=f"Message sent to {base_scope}",
        msg=info,
    )


def search_messages(
    scope: str | None = None,
    status: str | None = None,
    msg_type: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> MessageSearchResponse:
    conn = get_connection()
    try:
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list = []
        if scope:
            _validate_scope(scope)
            recipient_id, _label = _resolve_recipient(scope, conn=conn)
            sql += " AND recipient_id = ?"
            params.append(recipient_id)
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
        previews = [_row_to_preview(r, conn=conn) for r in rows]
    finally:
        conn.close()
    return MessageSearchResponse(messages=previews, total=len(previews))


def fetch_messages(
    scope: str,
    status: str | None = None,
    msg_type: str | None = None,
    limit: int = 50,
    mark_read: bool = False,
) -> MessageFetchResponse:
    _validate_scope(scope)
    conn = get_connection()
    try:
        recipient_id, _label = _resolve_recipient(scope, conn=conn)
        sql = "SELECT * FROM messages WHERE recipient_id = ?"
        params: list = [recipient_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if msg_type:
            sql += " AND msg_type = ?"
            params.append(msg_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()

        marked = 0
        if mark_read and rows:
            unread_ids = [r["id"] for r in rows if r["status"] == "unread"]
            if unread_ids:
                placeholders = ",".join("?" * len(unread_ids))
                conn.execute(
                    f"UPDATE messages SET status='read', read_at=? "
                    f"WHERE id IN ({placeholders})",
                    [now_ms(), *unread_ids],
                )
                conn.commit()
                marked = len(unread_ids)
                rows = conn.execute(sql, params).fetchall()
        msgs = [_row_to_info(r, conn=conn) for r in rows]
    finally:
        conn.close()
    return MessageFetchResponse(messages=msgs, total=len(msgs), marked_read_count=marked)


def mark_read(message_id: int | str) -> MessageActionResponse:
    """Flip a single message's status to ``read``. Accepts an integer id."""
    try:
        mid = int(str(message_id).split("@", 1)[0])
    except ValueError:
        return MessageActionResponse(
            message=f"Invalid message id {message_id!r}", msg=None,
        )
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM messages WHERE id=?", (mid,),
        ).fetchone()
        if row is None:
            return MessageActionResponse(
                message=f"No message {message_id} found", msg=None,
            )
        conn.execute(
            "UPDATE messages SET status='read', read_at=? WHERE id=?",
            (now_ms(), mid),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM messages WHERE id=?", (mid,),
        ).fetchone()
        info = _row_to_info(row, conn=conn)
    finally:
        conn.close()
    return MessageActionResponse(
        message=f"Marked {message_id} as read", msg=info,
    )


def list_recipients(query: str,
                    include_inactive: bool = False) -> list[str]:
    """Return valid recipient scopes matching ``query``.

    ``query`` is required. Pass ``"*"`` to return every recipient; any other
    value is compiled as a regex and matched against each recipient string
    (``workspace``, ``project:<p>``, ``scope:<p>/<s>``).

    ``include_inactive`` controls whether completed / deleted scopes appear
    as valid send targets. Default is False: stale scopes don't crowd the
    recipient list. Pass True for the rare backfill case.
    """
    from awm.config import PROJECTS_DIR
    from awm.services import scopes as scope_svc

    if not query:
        raise ValueError("query is required; pass '*' for all recipients")

    recipients = ["workspace"]
    projects_seen: set[str] = set()

    if PROJECTS_DIR.is_dir():
        for child in sorted(PROJECTS_DIR.iterdir()):
            if child.is_dir() and (child / ".bare").is_dir():
                recipients.append(f"project:{child.name}")
                projects_seen.add(child.name)

    # Add active scopes as recipients (or every status when explicitly asked)
    status = "all" if include_inactive else "active"
    result = scope_svc.search_scopes(status=status, limit=10_000)
    scopes_seen: set[str] = set()
    for s in result.scopes:
        if s.project not in projects_seen:
            recipients.append(f"project:{s.project}")
            projects_seen.add(s.project)
        scope_key = f"{s.project}/{s.scope}"
        if scope_key not in scopes_seen:
            recipients.append(f"scope:{scope_key}")
            scopes_seen.add(scope_key)

    if query == "*":
        return recipients
    try:
        pattern = re.compile(query)
    except re.error as exc:
        raise ValueError(f"invalid regex {query!r}: {exc}") from exc
    return [r for r in recipients if pattern.search(r)]
