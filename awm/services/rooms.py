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
peer, it dispatches to local scopes (via their AgentInstance input queue),
remote scopes (POST to that peer's agent input endpoint), local
subscribers (their WS-out queue), and shadow peers (POST to that peer's
room posts endpoint). AgentInstance wiring and the WS multiplex layer live
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
from awm.services.replication.schema import new_uuid, next_legacy_id


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
    """A second AgentInstance spawn for an already-running scope was attempted."""


class RoomArchiveBlocked(RoomError):
    """``archive_room`` refused because the room still has active scope
    participants. Inspect ``.blocking_scopes`` for the identifiers."""

    def __init__(self, room_id: str, blocking_scopes: list[str]):
        self.room_id = room_id
        self.blocking_scopes = blocking_scopes
        super().__init__(
            f"room {room_id} has {len(blocking_scopes)} active scope "
            f"participant(s): {', '.join(blocking_scopes)}"
        )


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
    kind: str           # 'scope' | 'subscriber' | 'shadow_peer' | 'voice'
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
        id=row["legacy_id"], room_id=row["room_id"], author=row["author"],
        body=row["body"], kind=row["kind"], ts=row["ts"],
    )


def _insert_post(conn, *, room_id: str, author: str, body: str,
                 kind: str, ts: str) -> str:
    """Insert a row into ``room_posts`` with a freshly minted uuid PK and
    per-peer monotonic ``legacy_id``. Returns the uuid so callers can
    re-fetch the row when they need the full Post object.

    The conn must be inside an active transaction; uuid/legacy_id minting
    relies on WAL serializing writers."""
    origin = _local_peer_id()
    uid = new_uuid()
    legacy = next_legacy_id(conn, "room_posts", origin)
    conn.execute(
        "INSERT INTO room_posts "
        "(uuid, legacy_id, origin_peer, room_id, author, body, kind, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, legacy, origin, room_id, author, body, kind, ts),
    )
    return uid


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
# The rooms service stays free of AgentInstance's concrete shape so it can
# be unit-tested without the agent runtime. M1 registers a callable here
# that takes (room_id, scope, post) and enqueues into the matching
# AgentInstance.input_queue.

_local_scope_dispatcher: Callable[[str, str, Post], None] | None = None
_remote_scope_dispatcher: Callable[[str, str, str, Post], None] | None = None
_shadow_peer_dispatcher: Callable[[str, str, Post], None] | None = None


def set_local_scope_dispatcher(fn: Callable[[str, str, Post], None] | None) -> None:
    """``fn(room_id, scope_key, post)`` — push to a local AgentInstance."""
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
    like ``'awm/research'`` or ``'awm/research@crux'``."""
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
        _insert_post(conn, room_id=name, author="system",
                     body=f"room opened by {opener}", kind="system", ts=now)
        for s in scopes:
            _insert_post(conn, room_id=name, author=f"agent:{s}",
                         body=f"scope:{s} joined", kind="join", ts=now)
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
        _insert_post(conn, room_id=room_id, author="system",
                     body="room closed", kind="system", ts=now)
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


def archive_room(room_id: str) -> Room:
    """Soft-archive a room: flip status to 'archived' so it drops out of
    default listings. Refuses if any participant with ``kind='scope'`` is
    still active (``left_at IS NULL``) — raise :class:`RoomArchiveBlocked`
    carrying the blocking scope identifiers.

    Closed rooms and active rooms with no remaining scope participants
    are both archivable. Already-archived rooms return unchanged."""
    room = get_room(room_id)
    if room is None:
        raise RoomNotFound(f"no such room: {room_id}")
    if room.status == "archived":
        return room
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT identifier FROM room_participants "
            "WHERE room_id = ? AND kind = 'scope' AND left_at IS NULL",
            (room_id,),
        ).fetchall()
        blocking = [r["identifier"] for r in rows]
        if blocking:
            raise RoomArchiveBlocked(room_id, blocking)
        now = _now()
        # Preserve closed_at if already set; stamp it for active→archived.
        if room.closed_at is None:
            conn.execute(
                "UPDATE rooms SET status = 'archived', closed_at = ? WHERE id = ?",
                (now, room_id),
            )
        else:
            conn.execute(
                "UPDATE rooms SET status = 'archived' WHERE id = ?",
                (room_id,),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    finally:
        conn.close()
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
    if kind not in ("scope", "subscriber", "shadow_peer", "voice"):
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
        _insert_post(conn, room_id=room_id, author=f"{kind}:{identifier}",
                     body=f"{kind}:{identifier} joined", kind="join", ts=now)
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
            _insert_post(conn, room_id=room_id,
                         author=f"{kind}:{identifier}",
                         body=f"{kind}:{identifier} left",
                         kind="leave", ts=now)
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
    - Local scope participants receive it as a AgentInstance input frame
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
        uid = _insert_post(conn, room_id=room_id, author=author,
                           body=body, kind=kind, ts=now)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM room_posts WHERE uuid = ?", (uid,),
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
    sql += " ORDER BY ts DESC, uuid DESC"
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
    that were closed. Called from agent_instances._waiter_loop."""
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


def get_post(post_id: int | str) -> Post | None:
    """Return a post by its public ``legacy_id``. Accepts ``42`` or
    ``'42@<peer>'``; bare ints resolve to the local peer."""
    legacy_id, peer = peer_svc.parse_id_ref(post_id)
    origin = peer if peer is not None else _local_peer_id()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM room_posts WHERE legacy_id = ? AND origin_peer = ?",
            (legacy_id, origin),
        ).fetchone()
    finally:
        conn.close()
    return _row_to_post(row) if row else None


# ---------------------------------------------------------------------------
# WS subscriber pump
# ---------------------------------------------------------------------------

_WS_QUEUE_MAX = 256


async def run_subscriber_session(
    websocket,
    room_id: str,
    user_as: str,
) -> None:
    """Drive a fully-attached WS subscriber for ``room_id``.

    Owns the queue/subscriber/broadcast loop: subscribes the socket to
    the room's event stream, registers a ``subscriber`` participant,
    streams transcript backlog, and spawns the reader + writer tasks.
    The API handler that calls into this function should already have
    authenticated and ``await websocket.accept(...)``ed before calling.

    Anything WS-protocol-shaped (envelope serialization, ``X-Awm-As`` →
    author rewriting) is owned here, NOT by the API layer. The API
    handler shrinks to ``auth + accept + delegate``.
    """
    # Imported here so the services layer doesn't grow a runtime dep on
    # the API package's envelope module shape (it's free-standing).
    from awm import ws_envelope as env

    room = get_room(room_id)
    if room is None:
        await websocket.send_text(json.dumps(env.error(f"no such room: {room_id}")))
        await websocket.close(code=1008, reason="no such room")
        return

    # The WS is pure UI transport — it is NOT a room participant.
    # `subscribe_room` plugs the queue into the broadcast fanout; that's the
    # only side-effect a transcript viewer should have. Logging subscribe /
    # unsubscribe as join/leave posts pollutes the transcript every time a
    # tab refreshes or focuses a different room.
    queue: asyncio.Queue = asyncio.Queue(maxsize=_WS_QUEUE_MAX)
    await subscribe_room(room_id, queue)

    # Send transcript backlog.
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
                kind = msg.get("kind", "text")
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
