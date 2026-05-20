"""Rooms: multi-participant conversations with scope-keyed agents and
human/peer subscribers.

A room is identified by a verb-noun name (``babbling-brook``), is anchored
to a single host peer (the one that created it), and owns:

- A **participant set**: rows in ``room_participants`` keyed by
  ``(kind, identifier)`` where ``kind`` is ``'scope'`` (an agent),
  ``'subscriber'`` (a live WS attachment, identified by a connection id),
  or ``'shadow_peer'`` (a remote peer that has joined as a subscriber).
- A **transcript**: append-only rows in ``room_posts``.

The room service is the *fan-out hub*: when a post arrives at the host
peer, it dispatches to local scopes (via their LiveSession input queue),
remote scopes (POST to that peer's agent input endpoint), local
subscribers (their WS-out queue), and shadow peers (POST to that peer's
room posts endpoint). LiveSession wiring and the WS multiplex layer live
in M1 / M3; this module is the data + dispatch core.

In-process event delivery uses an asyncio-friendly subscriber registry
(``subscribe_room`` / ``unsubscribe_room``) — callers (the rooms WS
endpoint) attach an ``asyncio.Queue`` that receives ``RoomEvent`` dicts
for every post and participant change.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from awm.db import get_connection
from awm.services import rooms_names
from awm.services.network import peers as peer_svc


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class RoomError(Exception):
    """Base class for rooms-service failures."""


class RoomNotFound(RoomError):
    pass


class RoomClosed(RoomError):
    pass


class ScopeBusyError(RoomError):
    """A second LiveSession spawn for an already-running scope was attempted."""


# ---------------------------------------------------------------------------
# Local peer identity for host_peer_id stamping
# ---------------------------------------------------------------------------

def _local_peer_id() -> str:
    ident = peer_svc.get_local_identity()
    if ident is None or not ident.get("peer_id"):
        # Rooms can still be created without an explicit identity;
        # they get stamped 'local' so federation can refuse them.
        return "local"
    return ident["peer_id"]


# ---------------------------------------------------------------------------
# Public dataclasses (mirror models.py shapes but with __dataclass_fields__
# for cheap construction; pydantic models live in awm.models)
# ---------------------------------------------------------------------------

@dataclass
class Room:
    id: str
    host_peer_id: str
    created_at: str
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
    room_id: str
    kind: str           # 'scope' | 'subscriber' | 'shadow_peer'
    identifier: str
    joined_at: str
    left_at: str | None

    def to_dict(self) -> dict:
        return {
            "room_id": self.room_id, "kind": self.kind,
            "identifier": self.identifier, "joined_at": self.joined_at,
            "left_at": self.left_at,
        }


@dataclass
class Post:
    id: int
    room_id: str
    author: str
    body: str
    kind: str
    ts: str

    def to_dict(self) -> dict:
        return {
            "id": self.id, "room_id": self.room_id, "author": self.author,
            "body": self.body, "kind": self.kind, "ts": self.ts,
        }


# ---------------------------------------------------------------------------
# Row → dataclass
# ---------------------------------------------------------------------------

def _row_to_room(row) -> Room:
    keys = row.keys()
    return Room(
        id=row["id"], host_peer_id=row["host_peer_id"],
        created_at=row["created_at"], closed_at=row["closed_at"],
        topic=row["topic"], status=row["status"],
        close_on_exit=bool(row["close_on_exit"]) if "close_on_exit" in keys else False,
    )


def _row_to_participant(row) -> Participant:
    return Participant(
        room_id=row["room_id"], kind=row["kind"], identifier=row["identifier"],
        joined_at=row["joined_at"], left_at=row["left_at"],
    )


def _row_to_post(row) -> Post:
    return Post(
        id=row["id"], room_id=row["room_id"], author=row["author"],
        body=row["body"], kind=row["kind"], ts=row["ts"],
    )


# ---------------------------------------------------------------------------
# In-process event bus for live subscribers
# ---------------------------------------------------------------------------

# room_id → set of asyncio.Queue
_subscribers: dict[str, set[asyncio.Queue]] = {}
_subscribers_lock = asyncio.Lock()


async def subscribe_room(room_id: str, queue: asyncio.Queue) -> None:
    """Register a queue to receive events for ``room_id``. Idempotent."""
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
    """Non-async broadcast — schedules put_nowait on each queue. Drops on
    full queues so a slow consumer can't stall the host. The WS layer
    detects the drop via a ``lagged`` envelope check it owns."""
    bucket = _subscribers.get(room_id)
    if not bucket:
        return
    for q in list(bucket):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # Signal the lag — the consumer is responsible for closing.
            try:
                q.put_nowait({"type": "lagged"})
            except asyncio.QueueFull:
                pass


# ---------------------------------------------------------------------------
# Agent-input dispatch hooks
# ---------------------------------------------------------------------------
# The rooms service stays free of LiveSession's concrete shape so it can
# be unit-tested without the agent runtime. M1 registers a callable here
# that takes (room_id, scope, post) and enqueues into the matching
# LiveSession.input_queue.

_local_scope_dispatcher: Callable[[str, str, Post], None] | None = None
_remote_scope_dispatcher: Callable[[str, str, str, Post], None] | None = None
_shadow_peer_dispatcher: Callable[[str, str, Post], None] | None = None


def set_local_scope_dispatcher(fn: Callable[[str, str, Post], None] | None) -> None:
    """``fn(room_id, scope_key, post)`` — push to a local LiveSession."""
    global _local_scope_dispatcher
    _local_scope_dispatcher = fn


def set_remote_scope_dispatcher(fn: Callable[[str, str, str, Post], None] | None) -> None:
    """``fn(peer_id, room_id, scope_key, post)`` — forward to a remote peer."""
    global _remote_scope_dispatcher
    _remote_scope_dispatcher = fn


def set_shadow_peer_dispatcher(fn: Callable[[str, str, Post], None] | None) -> None:
    """``fn(peer_id, room_id, post)`` — push to a remote peer that subscribes
    to a locally-hosted room."""
    global _shadow_peer_dispatcher
    _shadow_peer_dispatcher = fn


# ---------------------------------------------------------------------------
# Scope identifier helpers
# ---------------------------------------------------------------------------

def _split_scope(ident: str) -> tuple[str, str | None]:
    """Return (local_scope, peer_id_or_None) for a participant identifier
    like ``'awm/research'`` or ``'awm/research@dev-xaw'``."""
    if "@" in ident:
        base, peer = ident.rsplit("@", 1)
        return base, peer
    return ident, None


# ---------------------------------------------------------------------------
# Room CRUD
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _room_exists(name: str) -> bool:
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1 FROM rooms WHERE id = ?", (name,)).fetchone()
    finally:
        conn.close()
    return row is not None


def create_room(*, topic: str | None = None,
                scopes: Iterable[str] = (),
                opener: str = "user:operator",
                host_peer_id: str | None = None,
                close_on_exit: bool = False) -> Room:
    """Create a new room with an auto-generated name. ``scopes`` are
    enrolled as agent participants (an ``identifier`` of ``project/scope``
    or ``project/scope@peer``). ``opener`` is recorded as the author of
    the seeded system ``join`` post. ``close_on_exit`` flips the room to
    ``closed`` automatically when the last scope participant's session
    exits — useful for one-off jobs."""
    name = rooms_names.pick_unique(_room_exists)
    host = host_peer_id or _local_peer_id()
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO rooms (id, host_peer_id, created_at, topic, close_on_exit) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, host, now, topic, 1 if close_on_exit else 0),
        )
        for s in scopes:
            conn.execute(
                "INSERT INTO room_participants (room_id, kind, identifier, joined_at) "
                "VALUES (?, 'scope', ?, ?)",
                (name, s, now),
            )
        conn.execute(
            "INSERT INTO room_posts (room_id, author, body, kind, ts) "
            "VALUES (?, ?, ?, 'system', ?)",
            (name, "system", f"room opened by {opener}", now),
        )
        for s in scopes:
            conn.execute(
                "INSERT INTO room_posts (room_id, author, body, kind, ts) "
                "VALUES (?, ?, ?, 'join', ?)",
                (name, f"agent:{s}", f"scope:{s} joined", now),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM rooms WHERE id = ?", (name,)).fetchone()
    finally:
        conn.close()
    return _row_to_room(row)


def get_room(room_id: str) -> Room | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_room(row) if row else None


def list_rooms(*, status: str | None = "active",
               participating_scope: str | None = None,
               limit: int = 100) -> list[Room]:
    sql = "SELECT DISTINCT r.* FROM rooms r"
    params: list = []
    where: list[str] = []
    if participating_scope:
        sql += (
            " JOIN room_participants rp ON rp.room_id = r.id "
            "AND rp.kind = 'scope' AND rp.identifier = ? "
            "AND rp.left_at IS NULL"
        )
        params.append(participating_scope)
    if status and status != "all":
        where.append("r.status = ?")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.created_at DESC LIMIT ?"
    params.append(limit)
    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_to_room(r) for r in rows]


def search_rooms(query: str, *, limit: int = 20) -> list[Room]:
    """Match rooms by topic or transcript content (LIKE-based)."""
    like = f"%{query}%"
    sql = """\
SELECT DISTINCT r.*
FROM rooms r
LEFT JOIN room_posts p ON p.room_id = r.id
WHERE r.topic LIKE ? OR r.id LIKE ? OR p.body LIKE ?
ORDER BY r.created_at DESC
LIMIT ?
"""
    conn = get_connection()
    try:
        rows = conn.execute(sql, (like, like, like, limit)).fetchall()
    finally:
        conn.close()
    return [_row_to_room(r) for r in rows]


def close_room(room_id: str, *, kill_agents: bool = False) -> Room:
    room = get_room(room_id)
    if room is None:
        raise RoomNotFound(f"no such room: {room_id}")
    if room.status == "closed":
        return room
    now = _now()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE rooms SET status = 'closed', closed_at = ? WHERE id = ?",
            (now, room_id),
        )
        conn.execute(
            "INSERT INTO room_posts (room_id, author, body, kind, ts) "
            "VALUES (?, 'system', 'room closed', 'system', ?)",
            (room_id, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    finally:
        conn.close()
    _broadcast(room_id, {"type": "room_closed", "ts": now})
    if kill_agents:
        # The orchestration layer (M1/M5) registers a callback here.
        cb = _close_room_kill_callback
        if cb is not None:
            try:
                cb(room_id)
            except Exception:  # noqa: BLE001
                pass
    return _row_to_room(row)


_close_room_kill_callback: Callable[[str], None] | None = None


def set_close_room_kill_callback(fn: Callable[[str], None] | None) -> None:
    """Called from ``close_room(..., kill_agents=True)``. Set by M1."""
    global _close_room_kill_callback
    _close_room_kill_callback = fn


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------

def list_participants(room_id: str, *, active_only: bool = True) -> list[Participant]:
    sql = "SELECT * FROM room_participants WHERE room_id = ?"
    params: list = [room_id]
    if active_only:
        sql += " AND left_at IS NULL"
    sql += " ORDER BY joined_at"
    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_to_participant(r) for r in rows]


def add_participant(room_id: str, kind: str, identifier: str) -> Participant:
    if kind not in ("scope", "subscriber", "shadow_peer"):
        raise ValueError(f"invalid participant kind: {kind!r}")
    room = get_room(room_id)
    if room is None:
        raise RoomNotFound(f"no such room: {room_id}")
    if room.status == "closed":
        raise RoomClosed(f"room {room_id} is closed")
    now = _now()
    conn = get_connection()
    try:
        # Re-join: clear left_at if the row already exists.
        existing = conn.execute(
            "SELECT * FROM room_participants WHERE room_id = ? AND kind = ? AND identifier = ?",
            (room_id, kind, identifier),
        ).fetchone()
        if existing is not None:
            if existing["left_at"] is not None:
                conn.execute(
                    "UPDATE room_participants SET left_at = NULL, joined_at = ? "
                    "WHERE room_id = ? AND kind = ? AND identifier = ?",
                    (now, room_id, kind, identifier),
                )
            row = conn.execute(
                "SELECT * FROM room_participants WHERE room_id = ? AND kind = ? AND identifier = ?",
                (room_id, kind, identifier),
            ).fetchone()
        else:
            conn.execute(
                "INSERT INTO room_participants (room_id, kind, identifier, joined_at) "
                "VALUES (?, ?, ?, ?)",
                (room_id, kind, identifier, now),
            )
            row = conn.execute(
                "SELECT * FROM room_participants WHERE room_id = ? AND kind = ? AND identifier = ?",
                (room_id, kind, identifier),
            ).fetchone()
        conn.execute(
            "INSERT INTO room_posts (room_id, author, body, kind, ts) "
            "VALUES (?, ?, ?, 'join', ?)",
            (room_id, f"{kind}:{identifier}", f"{kind}:{identifier} joined", now),
        )
        conn.commit()
    finally:
        conn.close()
    participant = _row_to_participant(row)
    _broadcast(room_id, {"type": "participant_joined",
                         "participant": participant.to_dict()})
    return participant


def remove_participant(room_id: str, kind: str, identifier: str) -> bool:
    now = _now()
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE room_participants SET left_at = ? "
            "WHERE room_id = ? AND kind = ? AND identifier = ? AND left_at IS NULL",
            (now, room_id, kind, identifier),
        )
        if cur.rowcount > 0:
            conn.execute(
                "INSERT INTO room_posts (room_id, author, body, kind, ts) "
                "VALUES (?, ?, ?, 'leave', ?)",
                (room_id, f"{kind}:{identifier}", f"{kind}:{identifier} left", now),
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
# Posting
# ---------------------------------------------------------------------------

def post(room_id: str, *, author: str, body: str, kind: str = "text",
         to_scope: str | None = None) -> Post:
    """Append a post to ``room_id`` and dispatch to participants.

    Dispatch rules:
    - All live subscribers (local WS + shadow peers) receive the post.
    - Local scope participants receive it as a LiveSession input frame
      iff the author is non-agent OR ``to_scope`` matches the scope.
    - Remote scope participants are forwarded via the remote_scope
      dispatcher (M3).
    - Agent authors do **not** auto-feed other agents in v1 unless the
      poster explicitly addresses them via ``to_scope``.
    """
    room = get_room(room_id)
    if room is None:
        raise RoomNotFound(f"no such room: {room_id}")
    if room.status == "closed":
        raise RoomClosed(f"room {room_id} is closed")
    now = _now()
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO room_posts (room_id, author, body, kind, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (room_id, author, body, kind, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM room_posts WHERE id = ?", (cur.lastrowid,),
        ).fetchone()
    finally:
        conn.close()
    post_obj = _row_to_post(row)
    _broadcast(room_id, {"type": "post", "post": post_obj.to_dict()})

    # Dispatch to scope and shadow-peer participants.
    is_agent_author = author.startswith("agent:")
    for p in list_participants(room_id, active_only=True):
        if p.kind == "scope":
            if to_scope is not None and p.identifier != to_scope:
                continue
            if is_agent_author and to_scope is None:
                # v1 deferral: agent outputs don't feed other agents.
                continue
            if is_agent_author and to_scope == p.identifier and \
                    author == f"agent:{p.identifier}":
                # Don't echo the poster's own output to its own stdin.
                continue
            base_scope, peer = _split_scope(p.identifier)
            if peer is None or peer == _local_peer_id():
                if _local_scope_dispatcher is not None:
                    try:
                        _local_scope_dispatcher(room_id, base_scope, post_obj)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                if _remote_scope_dispatcher is not None:
                    try:
                        _remote_scope_dispatcher(peer, room_id, base_scope, post_obj)
                    except Exception:
                        pass
        elif p.kind == "shadow_peer":
            if _shadow_peer_dispatcher is not None:
                try:
                    _shadow_peer_dispatcher(p.identifier, room_id, post_obj)
                except Exception:
                    pass
        # 'subscriber' participants are addressed by _broadcast above.

    return post_obj


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

def history(room_id: str, *, limit_chars: int = 1024,
            before_ts: str | None = None) -> list[Post]:
    """Return the trailing transcript of ``room_id`` whose combined body
    sizes do not exceed ``limit_chars``.

    Posts are returned oldest→newest within the trimmed window.
    """
    if get_room(room_id) is None:
        raise RoomNotFound(f"no such room: {room_id}")
    sql = "SELECT * FROM room_posts WHERE room_id = ?"
    params: list = [room_id]
    if before_ts is not None:
        sql += " AND ts < ?"
        params.append(before_ts)
    sql += " ORDER BY ts DESC, id DESC"
    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out: list[Post] = []
    total = 0
    for row in rows:
        body_len = len(row["body"] or "")
        if out and total + body_len > limit_chars:
            break
        out.append(_row_to_post(row))
        total += body_len
    return list(reversed(out))


def auto_close_for_scope(scope_key: str) -> list[str]:
    """Close any active ``close_on_exit`` rooms whose participating scopes
    are all gone after ``scope_key`` left. Returns the list of room IDs
    that were closed. Called from sessions_live._waiter_loop."""
    closed: list[str] = []
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT r.id FROM rooms r "
            "JOIN room_participants p ON p.room_id = r.id "
            "WHERE r.status = 'active' AND r.close_on_exit = 1 "
            "AND p.kind = 'scope' AND p.identifier = ?",
            (scope_key,),
        ).fetchall()
        for r in rows:
            room_id = r["id"]
            # Are there other active scope participants still running?
            other = conn.execute(
                "SELECT COUNT(*) AS n FROM room_participants p "
                "WHERE p.room_id = ? AND p.kind = 'scope' "
                "AND p.identifier != ? AND p.left_at IS NULL",
                (room_id, scope_key),
            ).fetchone()
            if other["n"] > 0:
                continue
            closed.append(room_id)
    finally:
        conn.close()
    for rid in closed:
        try:
            close_room(rid)
        except RoomError:
            continue
    return closed


def get_post(post_id: int) -> Post | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM room_posts WHERE id = ?", (post_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_post(row) if row else None
