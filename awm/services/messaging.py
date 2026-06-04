"""Messaging service — scoped message queues for inter-agent communication."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from awm.db import get_connection
from awm.models import (
    MessageSendRequest,
    MessageInfo,
    MessagePreview,
    MessageSearchResponse,
    MessageFetchResponse,
    MessageActionResponse,
)
from awm.services import scopes as scope_svc
from awm.services.network import peers as peer_svc
from awm.services.replication.schema import new_uuid, next_legacy_id


def _local_peer_id() -> str:
    """Local peer id from PEER_FILE, '' if not initialized."""
    try:
        ident = peer_svc.get_local_identity()
    except Exception:
        return ""
    if ident is None:
        return ""
    return ident.get("peer_id") or ""

# Valid scope patterns. Optional ``@<peer-id>`` suffix routes the operation
# to a remote awm instance registered via `awm peer add`.
_SCOPE_RE = re.compile(
    r"^(workspace|project:[a-zA-Z0-9_-]+|scope:[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)"
    r"(@[A-Za-z0-9][A-Za-z0-9_-]*)?$"
)


def _split_peer(scope: str) -> tuple[str, str | None]:
    """Return (base_scope, peer_id_or_None). Raises ValueError on bad shape."""
    m = _SCOPE_RE.match(scope)
    if not m:
        raise ValueError(
            f"Invalid scope '{scope}'. Must be 'workspace', 'project:X', "
            f"or 'scope:X/Y', optionally suffixed with '@<peer-id>'."
        )
    base = m.group(1)
    peer = m.group(2)[1:] if m.group(2) else None
    return base, peer


def _validate_scope(scope: str) -> None:
    """Validate the local scope shape. Post-v33 cross-host fetch/search
    is supported: the ``@peer`` suffix narrows the local query to rows
    whose ``origin_peer`` matches, because CRR replication means every
    peer already has a copy."""
    # _split_peer raises on bad shapes; we don't need the result.
    _split_peer(scope)


def _row_to_info(row) -> MessageInfo:
    return MessageInfo(
        id=row["legacy_id"],
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


def _row_to_preview(row) -> MessagePreview:
    return MessagePreview(
        id=row["legacy_id"],
        scope=row["scope"],
        sender=row["sender"],
        msg_type=row["msg_type"],
        subject=row["subject"],
        status=row["status"],
        created_at=row["created_at"],
        read_at=row["read_at"],
    )


def send_message(req: MessageSendRequest) -> MessageActionResponse:
    """Send a message to a scope.

    If the scope carries an ``@<peer-id>`` suffix, the message is forwarded
    to that peer's ``/inbox`` (and not stored locally). Otherwise it lands
    in the local DB.
    """
    base_scope, peer_id = _split_peer(req.scope)

    if peer_id is not None:
        # Forward to remote peer. Don't pollute the local DB.
        from awm.services.network import federation
        payload = req.model_dump()
        payload["scope"] = base_scope  # strip @peer for the remote
        try:
            remote = federation.forward_send(peer_id, payload)
        except federation.UnknownPeerError as exc:
            raise ValueError(str(exc)) from exc
        # Re-shape into MessageActionResponse from the remote payload
        return MessageActionResponse(
            message=f"Message forwarded to {req.scope}",
            msg=MessageInfo(**remote["msg"]) if isinstance(remote, dict) and "msg" in remote else None,
        )

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        origin = _local_peer_id()
        legacy = next_legacy_id(conn, "messages", origin)
        uid = new_uuid()
        conn.execute(
            "INSERT INTO messages "
            "(uuid, legacy_id, origin_peer, scope, sender, msg_type, "
            " subject, body, metadata, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?)",
            (uid, legacy, origin, base_scope, req.sender, req.msg_type,
             req.subject, req.body, req.metadata, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM messages WHERE uuid = ?", (uid,),
        ).fetchone()
    finally:
        conn.close()
    return MessageActionResponse(
        message=f"Message sent to {base_scope}",
        msg=_row_to_info(row),
    )


def search_messages(
    scope: str | None = None,
    status: str | None = None,
    msg_type: str | None = None,
    query: str | None = None,
    limit: int = 50,
) -> MessageSearchResponse:
    """Browse message previews (no body/metadata) across scopes.

    For the full content of a specific scope's messages, use `fetch_messages`.
    """
    conn = get_connection()
    try:
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list = []
        if scope:
            _validate_scope(scope)
            base, peer = _split_peer(scope)
            sql += " AND scope = ?"
            params.append(base)
            if peer is not None:
                sql += " AND origin_peer = ?"
                params.append(peer)
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
    previews = [_row_to_preview(r) for r in rows]
    return MessageSearchResponse(messages=previews, total=len(previews))


def fetch_messages(
    scope: str,
    status: str | None = None,
    msg_type: str | None = None,
    limit: int = 50,
    mark_read: bool = False,
) -> MessageFetchResponse:
    """Retrieve full messages for a scope, optionally marking them read atomically.

    ``scope`` is required — this is the "read my inbox" primitive.  When
    ``mark_read=True``, any returned rows that were ``unread`` are flipped to
    ``read`` in the same transaction and the returned rows reflect the new state.
    """
    _validate_scope(scope)
    base, peer = _split_peer(scope)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        sql = "SELECT * FROM messages WHERE scope = ?"
        params: list = [base]
        if peer is not None:
            sql += " AND origin_peer = ?"
            params.append(peer)
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
            unread_uuids = [r["uuid"] for r in rows if r["status"] == "unread"]
            if unread_uuids:
                placeholders = ",".join("?" * len(unread_uuids))
                conn.execute(
                    f"UPDATE messages SET status = 'read', read_at = ? "
                    f"WHERE uuid IN ({placeholders})",
                    [now, *unread_uuids],
                )
                conn.commit()
                marked = len(unread_uuids)
                rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    msgs = [_row_to_info(r) for r in rows]
    return MessageFetchResponse(messages=msgs, total=len(msgs), marked_read_count=marked)


def mark_read(message_id: int | str) -> MessageActionResponse:
    """Flip a single message's status to ``read``. Accepts ``42`` for the
    local row or ``'42@peer'`` to target a row that originated on a remote
    peer (which post-replication exists locally too)."""
    legacy_id, peer = peer_svc.parse_id_ref(message_id)
    origin = peer if peer is not None else _local_peer_id()
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM messages WHERE legacy_id = ? AND origin_peer = ?",
            (legacy_id, origin),
        ).fetchone()
        if row is None:
            return MessageActionResponse(
                message=f"No message {message_id} found", msg=None,
            )
        conn.execute(
            "UPDATE messages SET status = 'read', read_at = ? WHERE uuid = ?",
            (now, row["uuid"]),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM messages WHERE uuid = ?", (row["uuid"],),
        ).fetchone()
    finally:
        conn.close()
    return MessageActionResponse(
        message=f"Marked {message_id} as read",
        msg=_row_to_info(row),
    )


def list_recipients(query: str, peer: str | None = None) -> list[str]:
    """Return valid recipient scopes matching ``query``.

    ``query`` is required. Pass ``"*"`` to return every recipient; any other
    value is compiled as a regex and matched against each recipient string
    (``workspace``, ``project:<p>``, ``scope:<p>/<s>``).

    ``peer`` is reserved for a future cached cross-peer view; only ``None`` or
    ``"local"`` is honoured today.
    """
    from awm.config import PROJECTS_DIR

    if not query:
        raise ValueError("query is required; pass '*' for all recipients")
    if peer not in (None, "local"):
        raise NotImplementedError(
            "peer fan-out for inbox_recipients is not yet wired "
            "— see plan-out-an-update-zesty-nygaard"
        )

    recipients = ["workspace"]
    projects_seen: set[str] = set()

    # Discover projects from filesystem
    if PROJECTS_DIR.is_dir():
        for child in sorted(PROJECTS_DIR.iterdir()):
            if child.is_dir() and (child / ".bare").is_dir():
                recipients.append(f"project:{child.name}")
                projects_seen.add(child.name)

    # Add ALL scopes (any status) as recipients — DISTINCT by (project, scope)
    result = scope_svc.list_scopes(status="all")
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
