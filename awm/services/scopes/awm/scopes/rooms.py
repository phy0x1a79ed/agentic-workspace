"""Rooms: agent-owned conversations with a single owning agent + a guest
list + an append-only transcript.

v1 modular model (per SCHEMA_HANDOFF.md):
- Rooms table uses ``owner_project`` + ``owner_scope`` natural keys (no owner_agent_id uuid).
- ``guest_list.guest_ref`` is a natural-key string: 'project/scope' for agents or
  'user:<name>' for users. ``guest_kind`` ∈ {'agent', 'user'}.
- ``room_transcripts.author`` is a natural-key literal ('agent:proj/scope',
  'user:name', 'system') or the SYSTEM_REF sentinel.

All SQL goes through ScopesDAO. The in-process event bus, dispatcher hooks,
and WS subscriber pump are preserved from the legacy shape; only DB access
is re-keyed.
"""

from __future__ import annotations

import asyncio
import json
import uuid as _uuid
from dataclasses import dataclass
from typing import Callable, Iterable

from awm.scopes.dao import ScopesDAO
from awm.scopes import rooms_names
from awm.scopes.identity import (
    SYSTEM_REF,
    display_for_ref,
    ms_to_iso,
    now_ms,
)


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
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Room:
    id: str
    host_peer_id: str
    created_at: str           # ISO TEXT for API stability
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
    """API-shape: ``kind`` is 'scope' (owner agent), 'user' (guest), or 'agent' (guest).
    ``identifier`` is 'project/scope' for agents or the username for users."""
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
    """API-shape. ``author`` is a human-readable string:
    'agent:proj/scope', 'user:name', or 'system'."""
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
# Local peer identity stub
# ---------------------------------------------------------------------------

def _local_peer_id() -> str:
    """Federation is retired; every room is local-host."""
    return "local"


# ---------------------------------------------------------------------------
# Author normalization helpers (natural keys → display strings)
# ---------------------------------------------------------------------------

def _author_ref_to_display(author_ref: str) -> str:
    """Render the stored natural-key author to the legacy display form
    ('agent:proj/scope', 'user:name', 'system')."""
    if not author_ref or author_ref == SYSTEM_REF:
        return "system"
    # Already a prefixed natural key — return as-is.
    if author_ref.startswith("agent:") or author_ref.startswith("user:"):
        return author_ref
    if author_ref.startswith("scope:"):
        return "agent:" + author_ref[len("scope:"):]
    # Bare 'proj/scope' form (should not be stored, but defensively handle)
    if "/" in author_ref:
        return f"agent:{author_ref}"
    return author_ref


def _display_to_author_ref(display: str, *, conn=None) -> str:
    """Normalize a caller-supplied legacy display string to the stored form.

    Identity resolution rules (natural keys only):
      - 'system' / '' → SYSTEM_REF
      - 'agent:proj/scope' or 'scope:proj/scope' → 'agent:proj/scope'
      - 'user:name' → 'user:name'
      - 'proj/scope' → 'agent:proj/scope'
      - bare username → 'user:<name>' (created on demand)
    """
    if not display or display == "system":
        return SYSTEM_REF
    if display.startswith("agent:") or display.startswith("user:"):
        return display
    if display.startswith("scope:"):
        return "agent:" + display[len("scope:"):]
    if "/" in display:
        return f"agent:{display}"
    # Bare username — resolve or create
    from awm.scopes.identity import user_id_for_username
    uid = user_id_for_username(display, conn=conn, create_if_missing=True)
    if uid:
        from awm.scopes.identity import username_for_user_id
        name = username_for_user_id(uid, conn=conn)
        return f"user:{name or display}"
    return f"user:{display}"


# ---------------------------------------------------------------------------
# Row → dataclass adapters
# ---------------------------------------------------------------------------

def _row_to_room(row) -> Room:
    return Room(
        id=row["id"],
        host_peer_id=_local_peer_id(),
        created_at=ms_to_iso(row["created_at"]) or "",
        closed_at=ms_to_iso(row["closed_at"]),
        topic=row["topic"],
        status=row["status"],
        close_on_exit=False,
    )


def _row_to_post(row) -> Post:
    return Post(
        id=row["id"],
        room_id=row["room_id"],
        author=_author_ref_to_display(row["author"]),
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
# Dispatcher hooks (registered by agents service)
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
# Scope identifier helpers
# ---------------------------------------------------------------------------

def _split_scope(ident: str) -> tuple[str, None]:
    """Strip any '@peer' suffix (federation is retired). Returns (base, None)."""
    if "@" in ident:
        base, _ = ident.rsplit("@", 1)
        return base, None
    return ident, None


# ---------------------------------------------------------------------------
# Embeddings helpers (degrade gracefully)
# ---------------------------------------------------------------------------

def _index_room(room_id: str) -> None:
    """Upsert a room embedding in the scopes DB. Silently no-ops on failure."""
    try:
        from awm.persistence.embeddings import upsert_embedding
        from awm.persistence.databases import get_connection
        dao = ScopesDAO()
        row = dao.query_one(
            "SELECT id, topic FROM rooms WHERE id=?", (room_id,)
        )
        if row is None:
            return
        text = row["topic"] or row["id"]
        # Append recent transcript snippets
        snippets = dao.query_all(
            "SELECT body FROM room_transcripts WHERE room_id=? "
            "ORDER BY ts DESC LIMIT 10",
            (room_id,),
        )
        if snippets:
            text += " " + " ".join(s["body"] for s in snippets if s["body"])
        conn = get_connection("scopes")
        try:
            upsert_embedding(conn, "room", room_id, text[:500])
        finally:
            conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Room CRUD
# ---------------------------------------------------------------------------

def _room_name_exists(name: str) -> bool:
    dao = ScopesDAO()
    return dao.query_one("SELECT 1 FROM rooms WHERE id=?", (name,)) is not None


def create_room(*, topic: str | None = None,
                scopes: Iterable[str] = (),
                opener: str = "user:operator",
                host_peer_id: str | None = None,
                close_on_exit: bool = False,
                owner_project: str | None = None,
                owner_scope: str | None = None,
                owner_agent_id: str | None = None) -> Room:
    """Create a new room. v1 requires a natural-key owner: pass
    ``owner_project``+``owner_scope`` (preferred) or the first entry in
    ``scopes`` as 'project/scope'. ``owner_agent_id`` is accepted for
    legacy callers but ignored (natural keys only). Remaining ``scopes``
    entries enroll as agent guests."""
    scope_list = list(scopes)

    # Determine owner (natural key)
    if owner_project and owner_scope:
        own_proj, own_scope = owner_project, owner_scope
        guest_scopes = scope_list
    else:
        if not scope_list:
            raise RoomError(
                "create_room requires either owner_project/owner_scope or "
                "at least one entry in scopes"
            )
        first = scope_list[0]
        base, _ = _split_scope(first)
        if "/" not in base:
            raise RoomError(
                f"create_room: scope {first!r} must be 'project/scope'"
            )
        own_proj, own_scope = base.split("/", 1)
        guest_scopes = scope_list[1:]

    name = rooms_names.pick_unique(_room_name_exists)
    topic_text = topic or name
    now = now_ms()

    # Normalize opener to stored author form
    opener_ref = _display_to_author_ref(opener)

    dao = ScopesDAO()
    with dao.transaction() as conn:
        dao2 = ScopesDAO(conn=conn)
        dao2.execute(
            "INSERT INTO rooms "
            "(id, owner_project, owner_scope, topic, status, created_at, closed_at) "
            "VALUES (?, ?, ?, ?, 'open', ?, NULL)",
            (name, own_proj, own_scope, topic_text, now),
        )
        # Enroll guest scopes
        for s in guest_scopes:
            base, _ = _split_scope(s)
            if "/" not in base:
                continue
            g_proj, g_sc = base.split("/", 1)
            # Don't enroll the owner as a guest
            if g_proj == own_proj and g_sc == own_scope:
                continue
            guest_ref = f"{g_proj}/{g_sc}"
            dao2.execute(
                "INSERT OR IGNORE INTO guest_list "
                "(room_id, guest_kind, guest_ref, display_name, subscriptions) "
                "VALUES (?, 'agent', ?, ?, '{}')",
                (name, guest_ref, base),
            )
        # Seed transcript
        _insert_transcript_conn(conn, room_id=name, author=opener_ref,
                                kind="session_start",
                                body=f"room opened by {opener}", ts=now)
        for s in guest_scopes:
            base, _ = _split_scope(s)
            if "/" not in base:
                continue
            g_proj, g_sc = base.split("/", 1)
            if g_proj == own_proj and g_sc == own_scope:
                continue
            agent_ref = f"agent:{g_proj}/{g_sc}"
            _insert_transcript_conn(conn, room_id=name, author=agent_ref,
                                    kind="join",
                                    body=f"agent:{base} joined", ts=now)

    dao3 = ScopesDAO()
    row = dao3.query_one("SELECT * FROM rooms WHERE id=?", (name,))
    room = _row_to_room(row)

    _index_room(name)
    return room


def get_room(room_id: str) -> Room | None:
    dao = ScopesDAO()
    row = dao.query_one("SELECT * FROM rooms WHERE id=?", (room_id,))
    return _row_to_room(row) if row else None


def search_rooms(
    query: str | None = None,
    *,
    status: str = "active",
    participating_scope: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Room]:
    """Search rooms by topic/transcript (keyword + semantic). Defaults to
    status='active' (alias for 'open'). ``participating_scope`` narrows to
    rooms where the given 'project/scope' is owner or guest.
    """
    effective_status = "open" if status == "active" else status
    dao = ScopesDAO()

    sql = "SELECT DISTINCT r.* FROM rooms r"
    params: list = []
    join_clauses: list[str] = []
    where: list[str] = []

    if participating_scope:
        ps = participating_scope
        if ps.startswith("scope:"):
            ps = ps[len("scope:"):]
        elif ps.startswith("agent:"):
            ps = ps[len("agent:"):]
        if "/" not in ps:
            return []
        ps_proj, ps_sc = ps.split("/", 1)
        ps_key = f"{ps_proj}/{ps_sc}"
        join_clauses.append("LEFT JOIN guest_list g ON g.room_id = r.id")
        where.append(
            "((r.owner_project = ? AND r.owner_scope = ?) "
            " OR (g.guest_kind='agent' AND g.guest_ref = ?))"
        )
        params.extend([ps_proj, ps_sc, ps_key])

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

    rows = dao.query_all(sql, params)
    rooms = [_row_to_room(r) for r in rows]

    if not query:
        return rooms

    keyword_keys = {r.id for r in rooms}

    def _materialize(room_id: str):
        d = ScopesDAO()
        r = d.query_one("SELECT * FROM rooms WHERE id=?", (room_id,))
        if r is None:
            return None
        if effective_status and effective_status != "all" and r["status"] != effective_status:
            return None
        if participating_scope:
            ps = participating_scope
            if ps.startswith(("scope:", "agent:")):
                ps = ps.split(":", 1)[1]
            if "/" not in ps:
                return None
            ps_proj, ps_sc = ps.split("/", 1)
            ps_key = f"{ps_proj}/{ps_sc}"
            match = d.query_one(
                "SELECT 1 FROM rooms r "
                "LEFT JOIN guest_list g ON g.room_id = r.id "
                "WHERE r.id = ? AND ("
                "  (r.owner_project = ? AND r.owner_scope = ?) "
                "  OR (g.guest_kind='agent' AND g.guest_ref = ?)"
                ") LIMIT 1",
                (room_id, ps_proj, ps_sc, ps_key),
            )
            if match is None:
                return None
        return _row_to_room(r)

    try:
        from awm.persistence.embeddings import hybrid_augment
        from awm.persistence.databases import get_connection
        conn = get_connection("scopes")
        try:
            return hybrid_augment(
                conn, query,
                source_type="room",
                keyword_hits=rooms, keyword_keys=keyword_keys,
                materialize=_materialize,
            )
        finally:
            conn.close()
    except Exception:
        return rooms


def close_room(room_id: str, *, kill_agents: bool = False) -> Room:
    room = get_room(room_id)
    if room is None:
        raise RoomNotFound(f"no such room: {room_id}")
    if room.status == "closed":
        return room
    now = now_ms()
    dao = ScopesDAO()
    with dao.transaction() as conn:
        dao2 = ScopesDAO(conn=conn)
        dao2.execute(
            "UPDATE rooms SET status='closed', closed_at=? WHERE id=?",
            (now, room_id),
        )
        _insert_transcript_conn(conn, room_id=room_id, author=SYSTEM_REF,
                                kind="system", body="room closed", ts=now)
    row = ScopesDAO().query_one("SELECT * FROM rooms WHERE id=?", (room_id,))
    _broadcast(room_id, {"type": "room_closed", "ts": ms_to_iso(now)})
    if kill_agents and _close_room_kill_callback is not None:
        try:
            _close_room_kill_callback(room_id)
        except Exception:
            pass
    return _row_to_room(row)


def archive_room(room_id: str) -> Room:
    """Soft-archive a room. Refuses if the owner agent is still
    'allocated' or 'active' (checks via the agents table in scopes DB)."""
    room = get_room(room_id)
    if room is None:
        raise RoomNotFound(f"no such room: {room_id}")
    if room.status == "archived":
        return room

    dao = ScopesDAO()
    # Check owner agent status
    owner = dao.query_one(
        "SELECT a.status FROM agents a "
        "JOIN projects p ON p.id = a.project_id "
        "WHERE p.name=? AND a.scope=? "
        "ORDER BY a.created_at DESC LIMIT 1",
        (room.host_peer_id, room.topic),  # these are wrong — need owner from rooms row
    )
    # Correct approach: re-query the rooms row for owner_project/owner_scope
    room_row = dao.query_one("SELECT * FROM rooms WHERE id=?", (room_id,))
    if room_row is None:
        raise RoomNotFound(f"no such room: {room_id}")

    own_proj = room_row["owner_project"]
    own_sc = room_row["owner_scope"]
    owner_agent = dao.query_one(
        "SELECT a.status FROM agents a "
        "JOIN projects p ON p.id = a.project_id "
        "WHERE p.name=? AND a.scope=? AND a.status IN ('allocated','active') "
        "LIMIT 1",
        (own_proj, own_sc),
    )
    if owner_agent is not None:
        raise RoomArchiveBlocked(room_id, [f"{own_proj}/{own_sc}"])

    now = now_ms()
    with dao.transaction() as conn:
        dao2 = ScopesDAO(conn=conn)
        if room.closed_at is None:
            dao2.execute(
                "UPDATE rooms SET status='archived', closed_at=? WHERE id=?",
                (now, room_id),
            )
        else:
            dao2.execute(
                "UPDATE rooms SET status='archived' WHERE id=?", (room_id,),
            )
    row = ScopesDAO().query_one("SELECT * FROM rooms WHERE id=?", (room_id,))
    return _row_to_room(row)


# ---------------------------------------------------------------------------
# Participants (API surface)
# ---------------------------------------------------------------------------

def list_participants(room_id: str, *, active_only: bool = True) -> list[Participant]:
    """Render owner + guest_list as the Participant shape.
    Owner is rendered as kind='scope' with identifier 'project/scope'."""
    dao = ScopesDAO()
    room_row = dao.query_one(
        "SELECT owner_project, owner_scope, created_at FROM rooms WHERE id=?",
        (room_id,),
    )
    if room_row is None:
        return []
    joined_iso = ms_to_iso(room_row["created_at"]) or ""
    out: list[Participant] = []
    own_label = f"{room_row['owner_project']}/{room_row['owner_scope']}"
    out.append(Participant(
        room_id=room_id, kind="scope", identifier=own_label,
        joined_at=joined_iso, left_at=None,
    ))
    guests = dao.query_all(
        "SELECT guest_kind, guest_ref, display_name FROM guest_list WHERE room_id=?",
        (room_id,),
    )
    for g in guests:
        # guest_ref is the natural key: 'project/scope' or 'user:<name>'
        ref = g["guest_ref"]
        if g["guest_kind"] == "agent":
            kind_out = "scope"
            identifier = ref  # already 'project/scope'
        else:
            kind_out = g["guest_kind"]
            identifier = ref  # 'user:<name>'
        out.append(Participant(
            room_id=room_id, kind=kind_out, identifier=identifier,
            joined_at=joined_iso, left_at=None,
        ))
    return out


def add_participant(room_id: str, kind: str, identifier: str) -> Participant:
    """Add a participant as either an agent or user guest.

    ``kind='scope'`` accepts 'project/scope'; ``'user'`` accepts a username.
    'subscriber'/'shadow_peer' are in-memory only (no DB insert).
    """
    if kind in ("subscriber", "shadow_peer"):
        return Participant(
            room_id=room_id, kind=kind, identifier=identifier,
            joined_at=ms_to_iso(now_ms()) or "", left_at=None,
        )
    room = get_room(room_id)
    if room is None:
        raise RoomNotFound(f"no such room: {room_id}")
    if room.status == "closed":
        raise RoomClosed(f"room {room_id} is closed")

    dao = ScopesDAO()
    if kind == "scope":
        ident = identifier
        if ident.startswith("agent:"):
            ident = ident[len("agent:"):]
        elif ident.startswith("scope:"):
            ident = ident[len("scope:"):]
        if "/" not in ident:
            raise RoomError(f"unknown scope identifier: {identifier!r}")
        g_proj, g_sc = ident.split("/", 1)
        guest_kind = "agent"
        guest_ref = f"{g_proj}/{g_sc}"
        display = guest_ref
    elif kind == "user":
        # Strip user: prefix if present
        name = identifier
        if name.startswith("user:"):
            name = name[len("user:"):]
        # Ensure user exists
        from awm.scopes.identity import user_id_for_username
        uid = user_id_for_username(name, create_if_missing=True)
        if not uid:
            raise RoomError(f"could not resolve user identifier: {identifier!r}")
        guest_kind = "user"
        guest_ref = f"user:{name}"
        display = name
    else:
        raise ValueError(f"invalid participant kind: {kind!r}")

    # Don't let the owner add themselves as a guest
    room_row = dao.query_one("SELECT owner_project, owner_scope FROM rooms WHERE id=?", (room_id,))
    if room_row and kind == "scope":
        if f"{room_row['owner_project']}/{room_row['owner_scope']}" == guest_ref:
            return Participant(
                room_id=room_id, kind=kind, identifier=identifier,
                joined_at=ms_to_iso(now_ms()) or "", left_at=None,
            )

    now = now_ms()
    with dao.transaction() as conn:
        dao2 = ScopesDAO(conn=conn)
        dao2.execute(
            "INSERT OR IGNORE INTO guest_list "
            "(room_id, guest_kind, guest_ref, display_name, subscriptions) "
            "VALUES (?, ?, ?, ?, '{}')",
            (room_id, guest_kind, guest_ref, display),
        )
        author_ref = f"agent:{guest_ref}" if guest_kind == "agent" else guest_ref
        _insert_transcript_conn(conn, room_id=room_id, author=author_ref,
                                kind="join",
                                body=f"{kind}:{identifier} joined", ts=now)

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
    dao = ScopesDAO()
    if kind == "scope":
        ident = identifier
        if ident.startswith(("agent:", "scope:")):
            ident = ident.split(":", 1)[1]
        if "/" not in ident:
            return False
        g_proj, g_sc = ident.split("/", 1)
        guest_kind = "agent"
        guest_ref = f"{g_proj}/{g_sc}"
    elif kind == "user":
        name = identifier
        if name.startswith("user:"):
            name = name[len("user:"):]
        guest_kind = "user"
        guest_ref = f"user:{name}"
    else:
        return False

    now = now_ms()
    deleted = False
    with dao.transaction() as conn:
        dao2 = ScopesDAO(conn=conn)
        cur_rows = dao2.query_all(
            "SELECT room_id FROM guest_list "
            "WHERE room_id=? AND guest_kind=? AND guest_ref=?",
            (room_id, guest_kind, guest_ref),
        )
        if cur_rows:
            dao2.execute(
                "DELETE FROM guest_list "
                "WHERE room_id=? AND guest_kind=? AND guest_ref=?",
                (room_id, guest_kind, guest_ref),
            )
            author_ref = f"agent:{guest_ref}" if guest_kind == "agent" else guest_ref
            _insert_transcript_conn(conn, room_id=room_id, author=author_ref,
                                    kind="leave",
                                    body=f"{kind}:{identifier} left", ts=now)
            deleted = True

    if deleted:
        _broadcast(room_id, {
            "type": "participant_left",
            "participant": {"kind": kind, "identifier": identifier},
        })
    return deleted


# ---------------------------------------------------------------------------
# Posting + transcript
# ---------------------------------------------------------------------------

def _insert_transcript_conn(conn, *, room_id: str, author: str, kind: str,
                             body: str, ts: int, meta: dict | None = None) -> str:
    """Insert a row into room_transcripts via the given connection."""
    if kind == "text":
        kind = "message"
    tid = str(_uuid.uuid4())
    meta_json = json.dumps(meta) if meta else "{}"
    dao = ScopesDAO(conn=conn)
    dao.execute(
        "INSERT INTO room_transcripts "
        "(id, room_id, author, kind, body, meta, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tid, room_id, author, kind, body or "", meta_json, ts),
    )
    return tid


def post_transcript(room_id: str, *, author: str, kind: str = "message",
                    body: str = "", meta: dict | None = None,
                    to_scope: str | None = None) -> Post:
    """Native post entry point. ``author`` is either a natural-key ref
    ('agent:proj/scope', 'user:name', 'system') or a uuid (legacy callers).
    Dispatches to participant input queues."""
    room = get_room(room_id)
    if room is None:
        raise RoomNotFound(f"no such room: {room_id}")
    if room.status == "closed":
        raise RoomClosed(f"room {room_id} is closed")

    now = now_ms()
    # Normalize author to stored form
    author_ref = _display_to_author_ref(author)

    dao = ScopesDAO()
    with dao.transaction() as conn:
        tid = _insert_transcript_conn(conn, room_id=room_id, author=author_ref,
                                      kind=kind, body=body, ts=now, meta=meta)

    row = ScopesDAO().query_one(
        "SELECT * FROM room_transcripts WHERE id=?", (tid,),
    )
    post_obj = _row_to_post(row)

    _broadcast(room_id, {"type": "post", "post": post_obj.to_dict()})
    _dispatch_to_participants(room_id, post_obj, to_scope=to_scope)

    # Re-index every ~10th post for semantic search
    if hash(tid) % 10 == 0:
        _index_room(room_id)

    return post_obj


def post(room_id: str, *, author: str, body: str, kind: str = "message",
         to_scope: str | None = None) -> Post:
    """Legacy entry point — ``author`` in legacy vocabulary; resolved to
    stored form by post_transcript."""
    if kind == "text":
        kind = "message"
    return post_transcript(room_id, author=author, kind=kind, body=body,
                           to_scope=to_scope)


def _dispatch_to_participants(room_id: str, post_obj: Post, *,
                              to_scope: str | None) -> None:
    """Push post into agent input queues matching dispatch rules."""
    is_agent_author = post_obj.author.startswith("agent:")
    dao = ScopesDAO()
    room_row = dao.query_one(
        "SELECT owner_project, owner_scope FROM rooms WHERE id=?", (room_id,),
    )
    guests = dao.query_all(
        "SELECT guest_kind, guest_ref FROM guest_list WHERE room_id=?",
        (room_id,),
    )

    targets: list[str] = []
    if room_row:
        targets.append(f"{room_row['owner_project']}/{room_row['owner_scope']}")
    for g in guests:
        if g["guest_kind"] == "agent":
            targets.append(g["guest_ref"])  # already 'project/scope'

    for scope_key in targets:
        if to_scope is not None and scope_key != to_scope:
            continue
        if is_agent_author and to_scope is None:
            continue  # agent outputs don't broadcast to other agents
        if (is_agent_author and to_scope == scope_key
                and post_obj.author == f"agent:{scope_key}"):
            continue  # don't echo poster back to itself
        if _local_scope_dispatcher is not None:
            try:
                _local_scope_dispatcher(room_id, scope_key, post_obj)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Transcript read
# ---------------------------------------------------------------------------

def history(room_id: str, *, limit_chars: int = 1024,
            before_ts: str | None = None) -> list[Post]:
    """Trailing transcript window. ``before_ts`` is ISO text."""
    if get_room(room_id) is None:
        raise RoomNotFound(f"no such room: {room_id}")
    dao = ScopesDAO()
    sql = "SELECT * FROM room_transcripts WHERE room_id=?"
    params: list = [room_id]
    if before_ts is not None:
        from awm.scopes.identity import iso_to_ms
        before_ms = iso_to_ms(before_ts)
        if before_ms is not None:
            sql += " AND ts < ?"
            params.append(before_ms)
    sql += " ORDER BY ts DESC, id DESC"
    rows = dao.query_all(sql, params)
    out: list[Post] = []
    total = 0
    for row in rows:
        body_len = len(row["body"] or "")
        if out and total + body_len > limit_chars:
            break
        out.append(_row_to_post(row))
        total += body_len
    return list(reversed(out))


def get_post(post_id: str) -> Post | None:
    if not isinstance(post_id, str) or "@" in post_id:
        return None
    dao = ScopesDAO()
    row = dao.query_one(
        "SELECT * FROM room_transcripts WHERE id=?", (post_id,),
    )
    if row is None:
        return None
    return _row_to_post(row)


def auto_close_for_scope(scope_key: str) -> list[str]:
    """Close open rooms owned by the given 'project/scope'. Returns closed IDs."""
    if "/" not in scope_key:
        return []
    project, scope = scope_key.split("/", 1)
    dao = ScopesDAO()
    rows = dao.query_all(
        "SELECT id FROM rooms WHERE owner_project=? AND owner_scope=? AND status='open'",
        (project, scope),
    )
    closed: list[str] = []
    for r in rows:
        try:
            close_room(r["id"])
            closed.append(r["id"])
        except RoomError:
            continue
    return closed


def rooms_for_scope(scope_key: str) -> list[str]:
    """Return IDs of open rooms where 'project/scope' is owner or guest."""
    if "/" not in scope_key:
        return []
    project, scope = scope_key.split("/", 1)
    guest_ref = f"{project}/{scope}"
    dao = ScopesDAO()
    rows = dao.query_all(
        "SELECT DISTINCT r.id FROM rooms r "
        "LEFT JOIN guest_list g ON g.room_id = r.id "
        "WHERE r.status='open' AND ("
        "  (r.owner_project=? AND r.owner_scope=?) "
        "  OR (g.guest_kind='agent' AND g.guest_ref=?)"
        ")",
        (project, scope, guest_ref),
    )
    return [r["id"] for r in rows]


def room_agents_kill_on_close(room_id: str) -> list[tuple[str, str]]:
    """Return (project, scope) pairs for all agent participants of a room.

    These are the scopes the agents service should SIGTERM when the room closes.
    Includes both the owner and all agent guests.
    """
    dao = ScopesDAO()
    room_row = dao.query_one(
        "SELECT owner_project, owner_scope FROM rooms WHERE id=?", (room_id,),
    )
    if room_row is None:
        return []
    result: list[tuple[str, str]] = [
        (room_row["owner_project"], room_row["owner_scope"])
    ]
    guests = dao.query_all(
        "SELECT guest_ref FROM guest_list WHERE room_id=? AND guest_kind='agent'",
        (room_id,),
    )
    for g in guests:
        ref = g["guest_ref"]
        if "/" in ref:
            proj, sc = ref.split("/", 1)
            result.append((proj, sc))
    return result


def ensure_agent_room(project: str, scope: str) -> str:
    """Get-or-create the owned room for a given scope. Returns the room_id.

    Looks for an existing open room owned by (project, scope); creates one
    if none found.
    """
    dao = ScopesDAO()
    row = dao.query_one(
        "SELECT id FROM rooms WHERE owner_project=? AND owner_scope=? "
        "AND status='open' ORDER BY created_at DESC LIMIT 1",
        (project, scope),
    )
    if row:
        return row["id"]
    room = create_room(owner_project=project, owner_scope=scope,
                       topic=f"room for {project}/{scope}")
    return room.id


# ---------------------------------------------------------------------------
# WS subscriber pump
# ---------------------------------------------------------------------------

_WS_QUEUE_MAX = 256


async def run_subscriber_session(
    websocket,
    room_id: str,
    user_as: str,
) -> None:
    """Drive a fully-attached WS subscriber for ``room_id``."""
    from awm.scopes import ws_envelope as env

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
# Compat shim
# ---------------------------------------------------------------------------

class ScopeBusyError(RoomError):
    pass
