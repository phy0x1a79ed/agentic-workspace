"""Rooms: agent-owned conversations with a single owning agent + a guest
list + an append-only transcript.

v37 model:
- A room has exactly one ``owner_agent_id`` (NOT NULL). Unowned rooms
  cannot exist.
- ``guest_list`` is the persistent membership set: ``(room_id, guest_kind,
  guest_ref)`` where ``guest_kind`` ∈ ``{'user', 'agent'}``. The owner is
  NEVER a guest of its own room — owner ≠ guest in the v37 data model.
- ``room_transcripts`` is the append-only event log. ``kind`` is one of
  message / tool_use / tool_result / system / notification / join / leave /
  slash / session_start / session_end / compact / system_prompt.
  ``author`` is a polymorphic ref: agents.id | users.id | the sentinel
  string 'system'.

The rooms service stays free of AgentInstance's concrete shape so it can
be unit-tested without the agent runtime. agent_instances registers
dispatchers here so a post can push into a local AgentInstance.input_queue,
forward to a remote peer, or fan out to subscribing peers.
"""

from __future__ import annotations

import asyncio
import json
import uuid as _uuid
from dataclasses import dataclass
from typing import Callable, Iterable

from awm.db import get_connection
from awm.services import identity, rooms_names
from awm.services.identity import display_for_ref, ms_to_iso, now_ms


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RoomError(Exception):
    """Base class for rooms-service failures."""


class RoomNotFound(RoomError):
    pass


class RoomClosed(RoomError):
    pass


class RoomArchiveBlocked(RoomError):
    """``archive_room`` refused because the room still has an active owner
    agent. Inspect ``.blocking_scopes`` for the offending agent labels."""

    def __init__(self, room_id: str, blocking_scopes: list[str]):
        self.room_id = room_id
        self.blocking_scopes = blocking_scopes
        super().__init__(
            f"room {room_id} has {len(blocking_scopes)} active scope "
            f"participant(s): {', '.join(blocking_scopes)}"
        )


# ---------------------------------------------------------------------------
# Public dataclasses — kept shape-compatible with the legacy API so callers
# (api/rooms, web-ui, MCP) keep getting ``project/scope``-style identifiers.
# ---------------------------------------------------------------------------

@dataclass
class Room:
    id: str
    host_peer_id: str
    created_at: str           # ISO TEXT for API stability over INTEGER ms
    closed_at: str | None
    topic: str | None
    status: str
    close_on_exit: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id, "host_peer_id": self.host_peer_id,
            "created_at": self.created_at, "closed_at": self.closed_at,
            "topic": self.topic, "status": self.status,
            "close_on_exit": self.close_on_exit,
        }


@dataclass
class Participant:
    """API-shape compatibility for the legacy ``list_participants`` callers.
    ``kind`` is 'scope' (owner agent), 'user' (guest), or 'agent' (guest);
    ``identifier`` is the rendered ``project/scope`` for agents or the
    username for users."""
    room_id: str
    kind: str
    identifier: str
    joined_at: str
    left_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "room_id": self.room_id, "kind": self.kind,
            "identifier": self.identifier, "joined_at": self.joined_at,
            "left_at": self.left_at,
        }


@dataclass
class Post:
    """API-shape compatibility for the legacy ``Post``. ``id`` is the
    room_transcripts.id (TEXT uuid in v37). ``author`` is rendered as a
    human-readable string (``agent:proj/scope``, ``user:name``, or
    ``system``) so the existing dispatch/post pipeline can keep matching
    on prefixes."""
    id: str
    room_id: str
    author: str
    body: str
    kind: str
    ts: str  # ISO TEXT

    def to_dict(self) -> dict:
        return {
            "id": self.id, "room_id": self.room_id, "author": self.author,
            "body": self.body, "kind": self.kind, "ts": self.ts,
        }


# ---------------------------------------------------------------------------
# Local peer identity for host_peer_id stamping
# ---------------------------------------------------------------------------

def _local_peer_id() -> str:
    from awm.services.network import peers as peer_svc
    ident = peer_svc.get_local_identity()
    if ident is None or not ident.get("peer_id"):
        return "local"
    return ident["peer_id"]


# ---------------------------------------------------------------------------
# Row → dataclass adapters
# ---------------------------------------------------------------------------

def _ref_to_legacy_author(ref: str, *, conn=None) -> str:
    """Render an author uuid back to ``agent:proj/scope`` / ``user:name`` /
    ``system`` for the legacy Post.author shape."""
    if ref == identity.SYSTEM_REF or not ref:
        return "system"
    label = display_for_ref(ref, conn=conn)
    # display_for_ref returns 'proj/scope' for agents and bare 'name' for
    # users. Reattach the legacy prefix so callers that match on
    # ``author.startswith('agent:')`` keep working.
    if "/" in label:
        return f"agent:{label}"
    return f"user:{label}"


def _row_to_room(row) -> Room:
    return Room(
        id=row["id"],
        # v37 dropped host_peer_id from the table — synthesize on read so the
        # API surface stays stable. Always the local peer for now (we own
        # every locally-visible room post-v37).
        host_peer_id=_local_peer_id(),
        created_at=ms_to_iso(row["created_at"]) or "",
        closed_at=ms_to_iso(row["closed_at"]),
        topic=row["topic"],
        status=row["status"],
        close_on_exit=False,
    )


def _row_to_post(row, *, conn=None) -> Post:
    return Post(
        id=row["id"],
        room_id=row["room_id"],
        author=_ref_to_legacy_author(row["author"], conn=conn),
        body=row["body"] or "",
        kind=row["kind"],
        ts=ms_to_iso(row["ts"]) or "",
    )


# ---------------------------------------------------------------------------
# In-process event bus for live subscribers
# ---------------------------------------------------------------------------

_subscribers: dict[str, set[asyncio.Queue]] = {}
_subscribers_lock = asyncio.Lock()


async def subscribe_room(room_id: str, queue: asyncio.Queue) -> None:
    async with _subscribers_lock:
        _subscribers.setdefault(room_id, set()).add(queue)


async def unsubscribe_room(room_id: str, queue: asyncio.Queue) -> None:
    async with _subscribers_lock:
        bucket = _subscribers.get(room_id)
        if bucket is None:
            return
        bucket.discard(queue)
        if not bucket:
            _subscribers.pop(room_id, None)


def _broadcast(room_id: str, event: dict) -> None:
    bucket = _subscribers.get(room_id)
    if not bucket:
        return
    for q in list(bucket):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            try:
                q.put_nowait({"type": "lagged"})
            except asyncio.QueueFull:
                pass


# ---------------------------------------------------------------------------
# Dispatcher hooks (registered by agent_instances)
# ---------------------------------------------------------------------------

_local_scope_dispatcher: Callable[[str, str, Post], None] | None = None
_remote_scope_dispatcher: Callable[[str, str, str, Post], None] | None = None
_shadow_peer_dispatcher: Callable[[str, str, Post], None] | None = None
_close_room_kill_callback: Callable[[str], None] | None = None


def set_local_scope_dispatcher(fn: Callable[[str, str, Post], None] | None) -> None:
    global _local_scope_dispatcher
    _local_scope_dispatcher = fn


def set_remote_scope_dispatcher(fn: Callable[[str, str, str, Post], None] | None) -> None:
    global _remote_scope_dispatcher
    _remote_scope_dispatcher = fn


def set_shadow_peer_dispatcher(fn: Callable[[str, str, Post], None] | None) -> None:
    global _shadow_peer_dispatcher
    _shadow_peer_dispatcher = fn


def set_close_room_kill_callback(fn: Callable[[str], None] | None) -> None:
    global _close_room_kill_callback
    _close_room_kill_callback = fn


# ---------------------------------------------------------------------------
# Scope identifier helpers (legacy API surface)
# ---------------------------------------------------------------------------

def _split_scope(ident: str) -> tuple[str, str | None]:
    """Return (local_scope, peer_id_or_None) for legacy ``proj/scope`` or
    ``proj/scope@peer`` identifiers."""
    if "@" in ident:
        base, peer = ident.rsplit("@", 1)
        return base, peer
    return ident, None


def _resolve_scope_to_agent_id(scope_key: str, *, conn=None) -> str | None:
    """Legacy ``project/scope`` → agents.id (active row preferred)."""
    if "/" not in scope_key:
        return None
    proj, sc = scope_key.split("/", 1)
    return identity.agent_id_for_scope(proj, sc, conn=conn, active_only=False)


# ---------------------------------------------------------------------------
# Room CRUD
# ---------------------------------------------------------------------------

def _room_exists(name: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1 FROM rooms WHERE id=?", (name,)).fetchone()
    finally:
        conn.close()
    return row is not None


def create_room(*, topic: str | None = None,
                scopes: Iterable[str] = (),
                opener: str = "user:operator",
                host_peer_id: str | None = None,
                close_on_exit: bool = False,
                owner_agent_id: str | None = None) -> Room:
    """Create a new room with an auto-generated name. v37 requires an
    owning agent; if ``owner_agent_id`` is not passed, the first entry in
    ``scopes`` is resolved to an agent id and used as the owner. Any
    remaining ``scopes`` entries enroll as agent guests. ``host_peer_id``
    and ``close_on_exit`` are accepted for legacy callers but no longer
    persisted in the v37 rooms shape (rooms close on owner-retirement, not
    on every-participant-exit anymore)."""
    scope_list = list(scopes)
    conn = get_connection()
    try:
        # Pick the owner first.
        if owner_agent_id is None:
            if not scope_list:
                raise RoomError(
                    "create_room requires either owner_agent_id or at least "
                    "one entry in scopes"
                )
            owner_aid = _resolve_scope_to_agent_id(scope_list[0], conn=conn)
            if not owner_aid:
                raise RoomError(
                    f"create_room: owner scope {scope_list[0]!r} does not "
                    "resolve to a known agent"
                )
            guest_scopes = scope_list[1:]
        else:
            owner_aid = owner_agent_id
            guest_scopes = scope_list

        name = rooms_names.pick_unique(_room_exists)
        topic_text = topic or name
        now = now_ms()
        conn.execute(
            "INSERT INTO rooms (id, owner_agent_id, topic, status, created_at, closed_at) "
            "VALUES (?, ?, ?, 'open', ?, NULL)",
            (name, owner_aid, topic_text, now),
        )
        # Enroll non-owner scopes as agent guests.
        for s in guest_scopes:
            base, _peer = _split_scope(s)
            aid = _resolve_scope_to_agent_id(base, conn=conn)
            if not aid or aid == owner_aid:
                continue
            display = base
            conn.execute(
                "INSERT OR IGNORE INTO guest_list "
                "(room_id, guest_kind, guest_ref, display_name, subscriptions) "
                "VALUES (?, 'agent', ?, ?, '{}')",
                (name, aid, display),
            )
        # Seed transcript events. The session_start kind announces the
        # room and the owner; opener is recorded as the author so the
        # legacy "room opened by …" thread shows up.
        opener_ref = identity.resolve_ref(opener, conn=conn, create_users=True) \
            or identity.SYSTEM_REF
        _insert_transcript(
            conn, room_id=name, author=opener_ref, kind="session_start",
            body=f"room opened by {opener}", ts=now,
        )
        for s in guest_scopes:
            base, _peer = _split_scope(s)
            aid = _resolve_scope_to_agent_id(base, conn=conn)
            if not aid:
                continue
            _insert_transcript(
                conn, room_id=name, author=aid, kind="join",
                body=f"agent:{base} joined", ts=now,
            )
        conn.commit()
        row = conn.execute("SELECT * FROM rooms WHERE id=?", (name,)).fetchone()
    finally:
        conn.close()
    try:
        from awm.services.embeddings import index_room
        index_room(name)
    except Exception:
        pass
    return _row_to_room(row)


def get_room(room_id: str) -> Room | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_room(row) if row else None


def search_rooms(
    query: str | None = None,
    *,
    status: str = "active",
    participating_scope: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Room]:
    """Search rooms by topic/transcript (keyword + semantic) with structural
    filters. Defaults to status='active' (pass 'all' for archived/closed too;
    ``'active'`` is accepted as an alias for the legacy ``'open'`` status to
    keep existing callers working). ``participating_scope`` narrows to rooms
    where the given scope is an active participant (owner or guest).
    ``query`` LIKEs on topic, id, and transcript bodies, with semantic top-up
    from the embeddings table.

    v37: rooms membership lives on ``guest_list`` (owner + guests); free-text
    body content lives on ``room_transcripts``.
    """
    # 'active' is the new default name for the legacy 'open' status.
    effective_status = "open" if status == "active" else status
    conn = get_connection()
    try:
        sql = "SELECT DISTINCT r.* FROM rooms r"
        params: list = []
        join_clauses: list[str] = []
        where: list[str] = []
        if participating_scope:
            agent_id = _resolve_scope_to_agent_id(participating_scope, conn=conn)
            if not agent_id:
                return []
            join_clauses.append(
                "LEFT JOIN guest_list g ON g.room_id = r.id"
            )
            where.append(
                "(r.owner_agent_id = ? "
                " OR (g.guest_kind='agent' AND g.guest_ref = ?))"
            )
            params.extend([agent_id, agent_id])
        if query:
            join_clauses.append(
                "LEFT JOIN room_transcripts t ON t.room_id = r.id"
            )
        if effective_status and effective_status != "all":
            where.append("r.status = ?")
            params.append(effective_status)
        if query:
            where.append("(r.topic LIKE ? OR r.id LIKE ? OR t.body LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like, like])
        if join_clauses:
            sql += " " + " ".join(join_clauses)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    rooms = [_row_to_room(r) for r in rows]

    if not query:
        return rooms

    keyword_keys = {r.id for r in rooms}

    def _materialize(room_id: str):
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM rooms WHERE id = ?", (room_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        if effective_status and effective_status != "all" and row["status"] != effective_status:
            return None
        # participating_scope filter is structural; semantic shouldn't bypass it
        if participating_scope:
            conn = get_connection()
            try:
                agent_id = _resolve_scope_to_agent_id(participating_scope, conn=conn)
                if not agent_id:
                    return None
                rp = conn.execute(
                    "SELECT 1 FROM rooms r "
                    "LEFT JOIN guest_list g ON g.room_id = r.id "
                    "WHERE r.id = ? AND ("
                    "  r.owner_agent_id = ? "
                    "  OR (g.guest_kind='agent' AND g.guest_ref = ?)"
                    ") LIMIT 1",
                    (room_id, agent_id, agent_id),
                ).fetchone()
            finally:
                conn.close()
            if rp is None:
                return None
        return _row_to_room(row)

    try:
        from awm.services.embeddings import hybrid_augment
        return hybrid_augment(
            query, source_type="room",
            keyword_hits=rooms, keyword_keys=keyword_keys,
            materialize=_materialize,
        )
    except Exception:
        return rooms


def close_room(room_id: str, *, kill_agents: bool = False) -> Room:
    room = get_room(room_id)
    if room is None:
        raise RoomNotFound(f"no such room: {room_id}")
    if room.status == "closed":
        return room
    now = now_ms()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE rooms SET status='closed', closed_at=? WHERE id=?",
            (now, room_id),
        )
        _insert_transcript(
            conn, room_id=room_id, author=identity.SYSTEM_REF,
            kind="system", body="room closed", ts=now,
        )
        conn.commit()
        row = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    finally:
        conn.close()
    _broadcast(room_id, {"type": "room_closed", "ts": ms_to_iso(now)})
    if kill_agents and _close_room_kill_callback is not None:
        try:
            _close_room_kill_callback(room_id)
        except Exception:  # noqa: BLE001
            pass
    return _row_to_room(row)


def archive_room(room_id: str) -> Room:
    """Soft-archive a room. Refuses if the owner agent is still
    ``allocated`` or ``active`` — the owner is the only ever-active
    participant in v37."""
    room = get_room(room_id)
    if room is None:
        raise RoomNotFound(f"no such room: {room_id}")
    if room.status == "archived":
        return room
    conn = get_connection()
    try:
        owner = conn.execute(
            "SELECT a.id, a.status, p.name, a.scope "
            "FROM rooms r JOIN agents a ON a.id = r.owner_agent_id "
            "JOIN projects p ON p.id = a.project_id WHERE r.id=?",
            (room_id,),
        ).fetchone()
        blocking: list[str] = []
        if owner is not None and owner["status"] in ("allocated", "active"):
            blocking.append(f"{owner['name']}/{owner['scope']}")
        if blocking:
            raise RoomArchiveBlocked(room_id, blocking)
        now = now_ms()
        if room.closed_at is None:
            conn.execute(
                "UPDATE rooms SET status='archived', closed_at=? WHERE id=?",
                (now, room_id),
            )
        else:
            conn.execute(
                "UPDATE rooms SET status='archived' WHERE id=?", (room_id,),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_room(row)


# ---------------------------------------------------------------------------
# Participants (legacy compatibility) + Guest list (v37 native)
# ---------------------------------------------------------------------------

def list_participants(room_id: str, *, active_only: bool = True) -> list[Participant]:
    """Legacy adapter: render owner + guest_list as the historical
    ``Participant`` shape. The owner is rendered as ``kind='scope'`` with
    the identifier ``project/scope``."""
    conn = get_connection()
    try:
        room = conn.execute(
            "SELECT owner_agent_id, created_at FROM rooms WHERE id=?", (room_id,),
        ).fetchone()
        if room is None:
            return []
        joined_iso = ms_to_iso(room["created_at"]) or ""
        out: list[Participant] = []
        owner_label = display_for_ref(room["owner_agent_id"], conn=conn)
        out.append(Participant(
            room_id=room_id, kind="scope", identifier=owner_label,
            joined_at=joined_iso, left_at=None,
        ))
        guests = conn.execute(
            "SELECT guest_kind, guest_ref, display_name FROM guest_list WHERE room_id=?",
            (room_id,),
        ).fetchall()
        for g in guests:
            label = display_for_ref(g["guest_ref"], conn=conn)
            kind_out = "scope" if g["guest_kind"] == "agent" else g["guest_kind"]
            out.append(Participant(
                room_id=room_id, kind=kind_out, identifier=label,
                joined_at=joined_iso, left_at=None,
            ))
    finally:
        conn.close()
    return out


def add_participant(room_id: str, kind: str, identifier: str) -> Participant:
    """Legacy adapter: add a participant as either an agent or user guest.

    ``kind='scope'`` resolves the identifier to an agent uuid; ``'user'``
    resolves to a users row (created on demand). 'subscriber' and
    'shadow_peer' from the old model are dropped silently — connection-
    level subscribers are tracked in-memory via ``subscribe_room``, not in
    guest_list."""
    if kind in ("subscriber", "shadow_peer"):
        # Nothing to persist — subscribe_room manages in-memory state.
        return Participant(
            room_id=room_id, kind=kind, identifier=identifier,
            joined_at=ms_to_iso(now_ms()) or "", left_at=None,
        )
    room = get_room(room_id)
    if room is None:
        raise RoomNotFound(f"no such room: {room_id}")
    if room.status == "closed":
        raise RoomClosed(f"room {room_id} is closed")
    conn = get_connection()
    try:
        if kind == "scope":
            aid = _resolve_scope_to_agent_id(identifier, conn=conn)
            if not aid:
                raise RoomError(f"unknown scope identifier: {identifier!r}")
            guest_kind = "agent"
            guest_ref = aid
            display = identifier
        elif kind == "user":
            uid = identity.user_id_for_username(identifier, conn=conn, create_if_missing=True)
            if not uid:
                raise RoomError(f"could not resolve user identifier: {identifier!r}")
            guest_kind = "user"
            guest_ref = uid
            display = identifier
        else:
            raise ValueError(f"invalid participant kind: {kind!r}")

        # Owner is never a guest — silently drop self-add attempts.
        owner_row = conn.execute(
            "SELECT owner_agent_id FROM rooms WHERE id=?", (room_id,),
        ).fetchone()
        if owner_row and owner_row[0] == guest_ref:
            return Participant(
                room_id=room_id, kind=kind, identifier=identifier,
                joined_at=ms_to_iso(now_ms()) or "", left_at=None,
            )

        now = now_ms()
        conn.execute(
            "INSERT OR IGNORE INTO guest_list "
            "(room_id, guest_kind, guest_ref, display_name, subscriptions) "
            "VALUES (?, ?, ?, ?, '{}')",
            (room_id, guest_kind, guest_ref, display),
        )
        _insert_transcript(
            conn, room_id=room_id, author=guest_ref, kind="join",
            body=f"{kind}:{identifier} joined", ts=now,
        )
        conn.commit()
    finally:
        conn.close()
    participant = Participant(
        room_id=room_id, kind=kind, identifier=identifier,
        joined_at=ms_to_iso(now_ms()) or "", left_at=None,
    )
    _broadcast(room_id, {"type": "participant_joined",
                         "participant": participant.to_dict()})
    return participant


def remove_participant(room_id: str, kind: str, identifier: str) -> bool:
    """Remove a guest from a room. Returns True if a row was deleted."""
    if kind in ("subscriber", "shadow_peer"):
        return False
    conn = get_connection()
    try:
        if kind == "scope":
            aid = _resolve_scope_to_agent_id(identifier, conn=conn)
            if not aid:
                return False
            guest_kind = "agent"
            guest_ref = aid
        elif kind == "user":
            uid = identity.user_id_for_username(identifier, conn=conn)
            if not uid:
                return False
            guest_kind = "user"
            guest_ref = uid
        else:
            return False
        cur = conn.execute(
            "DELETE FROM guest_list "
            "WHERE room_id=? AND guest_kind=? AND guest_ref=?",
            (room_id, guest_kind, guest_ref),
        )
        if cur.rowcount > 0:
            now = now_ms()
            _insert_transcript(
                conn, room_id=room_id, author=guest_ref, kind="leave",
                body=f"{kind}:{identifier} left", ts=now,
            )
        conn.commit()
    finally:
        conn.close()
    if cur.rowcount > 0:
        _broadcast(room_id, {
            "type": "participant_left",
            "participant": {"kind": kind, "identifier": identifier},
        })
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Posting + transcript
# ---------------------------------------------------------------------------

def _insert_transcript(conn, *, room_id: str, author: str, kind: str,
                       body: str, ts: int, meta: dict | None = None) -> str:
    """Insert a row into ``room_transcripts``. Returns the new id."""
    if kind == "text":
        kind = "message"
    tid = str(_uuid.uuid4())
    meta_json = json.dumps(meta) if meta else "{}"
    conn.execute(
        "INSERT INTO room_transcripts "
        "(id, room_id, author, kind, body, meta, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tid, room_id, author, kind, body or "", meta_json, ts),
    )
    return tid


def post_transcript(room_id: str, *, author: str, kind: str = "message",
                    body: str = "", meta: dict | None = None,
                    to_scope: str | None = None) -> Post:
    """v37-native post entry point. ``author`` is the polymorphic ref
    (agents.id | users.id | 'system'); callers using the legacy string
    form should go through ``post`` instead. Dispatches to participants
    the same way the legacy ``post`` did."""
    room = get_room(room_id)
    if room is None:
        raise RoomNotFound(f"no such room: {room_id}")
    if room.status == "closed":
        raise RoomClosed(f"room {room_id} is closed")
    now = now_ms()
    conn = get_connection()
    try:
        tid = _insert_transcript(
            conn, room_id=room_id, author=author, kind=kind,
            body=body, ts=now, meta=meta,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM room_transcripts WHERE id=?", (tid,),
        ).fetchone()
        post_obj = _row_to_post(row, conn=conn)
    finally:
        conn.close()
    _broadcast(room_id, {"type": "post", "post": post_obj.to_dict()})
    _dispatch_to_participants(room_id, post_obj, to_scope=to_scope)

    # Refresh the room embedding every ~10th post so semantic search tracks
    # evolving discussion without paying the embed cost on every message.
    if hash(tid) % 10 == 0:
        try:
            from awm.services.embeddings import index_room
            index_room(room_id)
        except Exception:
            pass
    return post_obj


def post(room_id: str, *, author: str, body: str, kind: str = "message",
         to_scope: str | None = None) -> Post:
    """Legacy entry point. ``author`` is a string in the legacy vocabulary
    (``agent:proj/scope``, ``user:name``, ``system``, …) and gets
    resolved via ``identity.resolve_ref`` to a target id before insert.
    Returns a Post with the legacy string author for backwards compat."""
    if kind == "text":
        kind = "message"
    ref = identity.resolve_ref(author, create_users=True) or identity.SYSTEM_REF
    return post_transcript(
        room_id, author=ref, kind=kind, body=body, to_scope=to_scope,
    )


def _dispatch_to_participants(room_id: str, post_obj: Post, *,
                              to_scope: str | None) -> None:
    """Push the post into the appropriate agent input queues + shadow-peer
    forwarders. Mirrors the legacy dispatch contract — author starting
    with ``agent:`` defers to the owner only unless ``to_scope`` is set."""
    is_agent_author = post_obj.author.startswith("agent:")
    conn = get_connection()
    try:
        owner_row = conn.execute(
            "SELECT a.id, p.name, a.scope "
            "FROM rooms r JOIN agents a ON a.id = r.owner_agent_id "
            "JOIN projects p ON p.id = a.project_id WHERE r.id=?",
            (room_id,),
        ).fetchone()
        guest_rows = conn.execute(
            "SELECT g.guest_kind, g.guest_ref, "
            "       p.name AS proj, a.scope AS scope_name "
            "FROM guest_list g "
            "LEFT JOIN agents a ON a.id = g.guest_ref AND g.guest_kind = 'agent' "
            "LEFT JOIN projects p ON p.id = a.project_id "
            "WHERE g.room_id = ?",
            (room_id,),
        ).fetchall()
    finally:
        conn.close()

    targets: list[tuple[str, str | None]] = []
    if owner_row is not None:
        targets.append((f"{owner_row['name']}/{owner_row['scope']}", None))
    for g in guest_rows:
        if g["guest_kind"] != "agent" or not g["proj"]:
            continue
        targets.append((f"{g['proj']}/{g['scope_name']}", None))

    for scope_key, peer in targets:
        if to_scope is not None and scope_key != to_scope:
            continue
        if is_agent_author and to_scope is None:
            continue  # v1 deferral: agent outputs don't feed other agents
        if (is_agent_author and to_scope == scope_key
                and post_obj.author == f"agent:{scope_key}"):
            continue  # don't echo poster's own output back into its stdin
        if peer is None or peer == _local_peer_id():
            if _local_scope_dispatcher is not None:
                try:
                    _local_scope_dispatcher(room_id, scope_key, post_obj)
                except Exception:  # noqa: BLE001
                    pass
        else:
            if _remote_scope_dispatcher is not None:
                try:
                    _remote_scope_dispatcher(peer, room_id, scope_key, post_obj)
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# Transcript read
# ---------------------------------------------------------------------------

def history(room_id: str, *, limit_chars: int = 1024,
            before_ts: str | None = None) -> list[Post]:
    """Trailing transcript window. ``before_ts`` is interpreted as ISO
    text (legacy callers) and converted to ms for the query."""
    if get_room(room_id) is None:
        raise RoomNotFound(f"no such room: {room_id}")
    sql = "SELECT * FROM room_transcripts WHERE room_id=?"
    params: list = [room_id]
    if before_ts is not None:
        before_ms = identity.iso_to_ms(before_ts)
        if before_ms is not None:
            sql += " AND ts < ?"
            params.append(before_ms)
    sql += " ORDER BY ts DESC, id DESC"
    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
        out: list[Post] = []
        total = 0
        for row in rows:
            body_len = len(row["body"] or "")
            if out and total + body_len > limit_chars:
                break
            out.append(_row_to_post(row, conn=conn))
            total += body_len
    finally:
        conn.close()
    return list(reversed(out))


def get_post(post_id: str) -> Post | None:
    """Lookup a transcript row by its uuid. Legacy ``42@peer`` ids are not
    supported in v37 — return None for those."""
    if not isinstance(post_id, str) or "@" in post_id:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM room_transcripts WHERE id=?", (post_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_post(row, conn=conn)
    finally:
        conn.close()


def auto_close_for_scope(scope_key: str) -> list[str]:
    """Close any open rooms whose owner is the agent named ``scope_key``.
    Used by agent_instances._waiter_loop on retirement.

    v37 semantics shift here: a room closes when its owner retires
    (single-owner model). The old close_on_exit + last-scope-leaves
    heuristic was a multi-scope holdover."""
    proj_scope = scope_key
    if "/" not in proj_scope:
        return []
    project, scope = proj_scope.split("/", 1)
    aid = identity.agent_id_for_scope(project, scope, active_only=False)
    if not aid:
        return []
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id FROM rooms WHERE owner_agent_id=? AND status='open'",
            (aid,),
        ).fetchall()
    finally:
        conn.close()
    closed: list[str] = []
    for r in rows:
        try:
            close_room(r["id"])
            closed.append(r["id"])
        except RoomError:
            continue
    return closed


# ---------------------------------------------------------------------------
# WS subscriber pump
# ---------------------------------------------------------------------------

_WS_QUEUE_MAX = 256


async def run_subscriber_session(
    websocket,
    room_id: str,
    user_as: str,
) -> None:
    """Drive a fully-attached WS subscriber for ``room_id``. Owns the
    queue/subscriber/broadcast loop and transcript backlog send. WS is
    pure transport — not a guest_list participant in v37."""
    from awm._lib import ws_envelope as env

    room = get_room(room_id)
    if room is None:
        await websocket.send_text(json.dumps(env.error(f"no such room: {room_id}")))
        await websocket.close(code=1008, reason="no such room")
        return

    queue: asyncio.Queue = asyncio.Queue(maxsize=_WS_QUEUE_MAX)
    await subscribe_room(room_id, queue)

    backlog = history(room_id, limit_chars=4096)
    await websocket.send_text(json.dumps(
        env.history([p.to_dict() for p in backlog])
    ))

    async def writer():
        while True:
            ev = await queue.get()
            if env.is_lagged(ev):
                try:
                    await websocket.send_text(json.dumps(ev))
                except Exception:
                    return
                await websocket.close(code=1011, reason="lagged")
                return
            try:
                await websocket.send_text(json.dumps(ev))
            except Exception:
                return

    async def reader():
        from fastapi import WebSocketDisconnect
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps(env.error("invalid JSON")))
                continue
            mtype = msg.get("type")
            if mtype == "post":
                body = msg.get("body", "")
                kind = msg.get("kind", "message")
                to_scope = msg.get("to") or None
                try:
                    post(room_id, author=user_as, body=body,
                         kind=kind, to_scope=to_scope)
                except RoomError as exc:
                    await websocket.send_text(json.dumps(env.error(str(exc))))
            elif mtype == "control":
                action = msg.get("action")
                if action == "close":
                    try:
                        close_room(room_id)
                    except RoomError as exc:
                        await websocket.send_text(json.dumps(env.error(str(exc))))
                elif action == "kill":
                    try:
                        close_room(room_id, kill_agents=True)
                    except RoomError as exc:
                        await websocket.send_text(json.dumps(env.error(str(exc))))
                else:
                    await websocket.send_text(json.dumps(
                        env.error(f"unknown control action: {action}")))
            elif mtype == "ping":
                await websocket.send_text(json.dumps(env.pong()))
            else:
                await websocket.send_text(json.dumps(
                    env.error(f"unknown envelope type: {mtype}")))

    writer_task = asyncio.create_task(writer())
    reader_task = asyncio.create_task(reader())
    try:
        await asyncio.wait(
            {writer_task, reader_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        writer_task.cancel()
        reader_task.cancel()
        await unsubscribe_room(room_id, queue)
        try:
            if websocket.client_state.name != "DISCONNECTED":
                await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# ScopeBusyError compatibility shim
# ---------------------------------------------------------------------------
# agent_instances.py used to import ScopeBusyError from rooms. v37 moves
# that into agent_instances itself; the shim below keeps any straggler
# import path working until the next cleanup pass.

class ScopeBusyError(RoomError):
    pass
