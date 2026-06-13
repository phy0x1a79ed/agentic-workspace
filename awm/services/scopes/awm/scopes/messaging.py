"""Messaging service — agent-addressed inboxes (v1 modular).

v1 schema change: ``messages.recipient_id`` / ``messages.sender_id``
(polymorphic uuid refs) are replaced by ``recipient_ref`` / ``sender_ref``
— natural-key literal strings stored directly:
  * 'workspace' → the workspace system sentinel.
  * 'project:<name>' → per-project catch-all inbox.
  * 'agent:<project>/<scope>' → the agent's natural key.
  * 'user:<name>' → that user's natural key.

All SQL goes through ScopesDAO. The rooms dual-write goes through the
IN-PROCESS rooms surface (``awm.scopes.rooms``) — no RPC, since both
surfaces are co-located in this dist.
"""

from __future__ import annotations

import re
from typing import Any

from awm.scopes.dao import ScopesDAO
from awm.scopes.identity import (
    SYSTEM_REF,
    agent_id_for_scope,
    now_ms,
    ms_to_iso,
)
from awm.scopes.models import (
    MessageSendRequest,
    MessageInfo,
    MessagePreview,
    MessageSearchResponse,
    MessageFetchResponse,
    MessageActionResponse,
)


_SCOPE_RE = re.compile(
    r"^(workspace|project:[a-zA-Z0-9_-]+|scope:[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)$"
)


def _validate_scope(scope: str) -> None:
    if not _SCOPE_RE.match(scope):
        raise ValueError(
            f"Invalid scope '{scope}'. Must be 'workspace', 'project:X', "
            f"or 'scope:X/Y'."
        )


def _project_inbox_ref(name: str) -> str:
    """Natural-key ref for the per-project catch-all inbox."""
    return f"project:{name}"


def _scope_to_recipient_ref(base_scope: str) -> str:
    """Map an inbox scope string to its canonical recipient_ref for storage.

    Stored refs:
      - 'workspace' → 'system'
      - 'project:<name>' → 'project:<name>'
      - 'scope:<project>/<scope>' → 'agent:<project>/<scope>'
    """
    if base_scope == "workspace":
        return "system"
    if base_scope.startswith("project:"):
        return base_scope  # stored as 'project:<name>'
    if base_scope.startswith("scope:"):
        body = base_scope[len("scope:"):]
        return f"agent:{body}"
    return base_scope


def _recipient_ref_to_scope_label(ref: str) -> str:
    """Reverse: stored recipient_ref → scope label for API responses."""
    if not ref or ref == "system":
        return "workspace"
    if ref.startswith("project:"):
        return ref
    if ref.startswith("agent:"):
        return "scope:" + ref[len("agent:"):]
    if ref.startswith("user:"):
        return ref
    return ref


def _resolve_sender_ref(sender_literal: str) -> str:
    """Best-effort: resolve caller-supplied sender string to stored ref.

    Recognizes 'user:<name>', 'agent:<proj>/<scope>', 'scope:<proj>/<scope>',
    'system'; bare names become 'user:<name>'.
    """
    if not sender_literal or sender_literal in ("system", SYSTEM_REF):
        return "system"
    if sender_literal.startswith("user:"):
        return sender_literal
    if sender_literal.startswith("agent:"):
        return sender_literal
    if sender_literal.startswith("scope:"):
        return "agent:" + sender_literal[len("scope:"):]
    if "/" in sender_literal:
        return f"agent:{sender_literal}"
    return f"user:{sender_literal}"


def _sender_ref_to_label(ref: str) -> str:
    if not ref or ref == "system":
        return "system"
    return ref  # already in canonical form


def _row_to_info(row) -> MessageInfo:
    return MessageInfo(
        id=row["id"],
        scope=_recipient_ref_to_scope_label(row["recipient_ref"]),
        sender=_sender_ref_to_label(row["sender_ref"]),
        msg_type=row["msg_type"],
        subject=row["subject"],
        body=row["body"],
        metadata=row["metadata"],
        status=row["status"],
        created_at=ms_to_iso(row["created_at"]) or "",
        read_at=ms_to_iso(row["read_at"]),
    )


def _row_to_preview(row) -> MessagePreview:
    return MessagePreview(
        id=row["id"],
        scope=_recipient_ref_to_scope_label(row["recipient_ref"]),
        sender=_sender_ref_to_label(row["sender_ref"]),
        msg_type=row["msg_type"],
        subject=row["subject"],
        status=row["status"],
        created_at=ms_to_iso(row["created_at"]) or "",
        read_at=ms_to_iso(row["read_at"]),
    )


def _agent_owned_room_id(agent_ref: str) -> str | None:
    """Find an open room owned by the given 'agent:project/scope' ref."""
    if not agent_ref.startswith("agent:"):
        return None
    body = agent_ref[len("agent:"):]
    if "/" not in body:
        return None
    proj, sc = body.split("/", 1)
    dao = ScopesDAO()
    row = dao.query_one(
        "SELECT id FROM rooms WHERE owner_project=? AND owner_scope=? "
        "AND status='open' ORDER BY created_at DESC LIMIT 1",
        (proj, sc),
    )
    return row["id"] if row else None


def send_message(req: MessageSendRequest) -> MessageActionResponse:
    """Send a message. Inserts into messages table and (when the recipient
    is an agent with an owned room) posts a notification to its transcript."""
    _validate_scope(req.scope)
    base_scope = req.scope

    recipient_ref = _scope_to_recipient_ref(base_scope)
    sender_ref = _resolve_sender_ref(req.sender)

    dao = ScopesDAO()
    cur_rows: list[Any] = []
    with dao.transaction() as conn:
        dao2 = ScopesDAO(conn=conn)
        dao2.execute(
            "INSERT INTO messages "
            "(recipient_ref, sender_ref, msg_type, subject, body, metadata, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'unread', ?)",
            (recipient_ref, sender_ref, req.msg_type, req.subject, req.body,
             req.metadata, now_ms()),
        )

    # Fetch the inserted row
    dao3 = ScopesDAO()
    row = dao3.query_one(
        "SELECT * FROM messages WHERE recipient_ref=? AND sender_ref=? "
        "ORDER BY created_at DESC LIMIT 1",
        (recipient_ref, sender_ref),
    )
    info = _row_to_info(row)

    # Dual-write: if recipient is an agent with an owned room, post a
    # transcript notification so the running agent's session sees it.
    if base_scope.startswith("scope:"):
        owned_room = _agent_owned_room_id(recipient_ref)
        if owned_room:
            try:
                from awm.scopes import rooms as rooms_svc
                rooms_svc.post_transcript(
                    owned_room, author=sender_ref, kind="notification",
                    body=req.subject or req.body or "",
                    meta={"message_id": info.id, "msg_type": req.msg_type},
                )
            except Exception:
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
    dao = ScopesDAO()
    sql = "SELECT * FROM messages WHERE 1=1"
    params: list = []
    if scope:
        _validate_scope(scope)
        recipient_ref = _scope_to_recipient_ref(scope)
        sql += " AND recipient_ref = ?"
        params.append(recipient_ref)
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
    rows = dao.query_all(sql, params)
    previews = [_row_to_preview(r) for r in rows]
    return MessageSearchResponse(messages=previews, total=len(previews))


def fetch_messages(
    scope: str,
    status: str | None = None,
    msg_type: str | None = None,
    limit: int = 50,
    mark_read: bool = False,
) -> MessageFetchResponse:
    _validate_scope(scope)
    recipient_ref = _scope_to_recipient_ref(scope)
    dao = ScopesDAO()
    sql = "SELECT * FROM messages WHERE recipient_ref = ?"
    params: list = [recipient_ref]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if msg_type:
        sql += " AND msg_type = ?"
        params.append(msg_type)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = dao.query_all(sql, params)

    marked = 0
    if mark_read and rows:
        unread_ids = [r["id"] for r in rows if r["status"] == "unread"]
        if unread_ids:
            placeholders = ",".join("?" * len(unread_ids))
            with dao.transaction() as conn:
                dao2 = ScopesDAO(conn=conn)
                dao2.execute(
                    f"UPDATE messages SET status='read', read_at=? "
                    f"WHERE id IN ({placeholders})",
                    [now_ms(), *unread_ids],
                )
            marked = len(unread_ids)
            rows = dao.query_all(sql, params)

    msgs = [_row_to_info(r) for r in rows]
    return MessageFetchResponse(messages=msgs, total=len(msgs), marked_read_count=marked)


def mark_read(message_id: int | str) -> MessageActionResponse:
    """Flip a single message's status to 'read'."""
    try:
        mid = int(str(message_id).split("@", 1)[0])
    except ValueError:
        return MessageActionResponse(
            message=f"Invalid message id {message_id!r}", msg=None,
        )
    dao = ScopesDAO()
    row = dao.query_one("SELECT * FROM messages WHERE id=?", (mid,))
    if row is None:
        return MessageActionResponse(
            message=f"No message {message_id} found", msg=None,
        )
    with dao.transaction() as conn:
        ScopesDAO(conn=conn).execute(
            "UPDATE messages SET status='read', read_at=? WHERE id=?",
            (now_ms(), mid),
        )
    row = dao.query_one("SELECT * FROM messages WHERE id=?", (mid,))
    info = _row_to_info(row)
    return MessageActionResponse(
        message=f"Marked {message_id} as read", msg=info,
    )


def list_recipients(query: str, include_inactive: bool = False) -> list[str]:
    """Return valid recipient scopes matching ``query``.

    ``query`` is required. Pass ``"*"`` for all; any other value is
    compiled as a regex. ``include_inactive`` controls whether completed
    scopes appear.
    """
    from awm.config import PROJECTS_DIR
    from awm.scopes import scopes as scope_svc

    if not query:
        raise ValueError("query is required; pass '*' for all recipients")

    recipients = ["workspace"]
    projects_seen: set[str] = set()

    if PROJECTS_DIR.is_dir():
        for child in sorted(PROJECTS_DIR.iterdir()):
            if child.is_dir() and (child / ".bare").is_dir():
                recipients.append(f"project:{child.name}")
                projects_seen.add(child.name)

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
