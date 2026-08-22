"""The scope channel — a scope IS the channel.

There is no separate rooms/messages/session_logs machinery. Every scope owns
one append-only post log (``scope_posts``); messages, journal (debrief)
entries, and system notices are all rows there, differentiated by ``kind``.
Other scopes/users subscribe to a channel (``scope_subscribers``); the owner is
implicit (the scope itself). Raw agent acts are NOT here — they belong to the
agent and live in the agents service's own DB; you subscribe to an *agent* for
those, and message a *scope* for these.

Addressing is the scope's natural key ``(project, scope)``. For legacy
non-agent targets (a user/project/workspace inbox) the channel need not be a
literal worktree scope: ``project=''`` marks a non-literal channel whose ref
lives verbatim in ``scope`` (e.g. ``'user:alice'``, ``'project:awm'``,
``'workspace'``).

Post ``kind``:
  - ``message`` — a post by a user or another scope/agent.
  - ``journal`` — a self-post by the owning agent (the debrief entry; its
    structured fields live in ``meta``).
  - ``system``  — a system notice.
  - ``goal``    — what the user is after at this level; see :mod:`awm.scopes.goals`.

All SQL goes through :class:`ScopesDAO`.
"""

from __future__ import annotations

import asyncio
import json
import uuid as _uuid
from dataclasses import dataclass

from awm.scopes.dao import ScopesDAO
from awm.scopes.identity import SYSTEM_REF, ms_to_iso, now_ms, iso_to_ms


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ChannelError(Exception):
    """Base class for scope-channel failures."""


# ---------------------------------------------------------------------------
# Dataclasses (API shape)
# ---------------------------------------------------------------------------

@dataclass
class ScopePost:
    id: str
    project: str
    scope: str
    author: str          # 'agent:proj/scope', 'user:name', or 'system'
    kind: str            # 'message' | 'journal' | 'system' | …
    body: str
    meta: dict
    ts: str              # ISO TEXT

    def to_dict(self) -> dict:
        return {
            "id": self.id, "project": self.project, "scope": self.scope,
            "author": self.author, "kind": self.kind, "body": self.body,
            "meta": self.meta, "ts": self.ts,
        }


@dataclass
class Subscriber:
    project: str
    scope: str
    guest_kind: str      # 'agent' | 'user'
    guest_ref: str       # 'project/scope' (agent) or 'user:<name>'
    display_name: str
    joined_at: str       # ISO TEXT

    def to_dict(self) -> dict:
        return {
            "project": self.project, "scope": self.scope,
            "guest_kind": self.guest_kind, "guest_ref": self.guest_ref,
            "display_name": self.display_name, "joined_at": self.joined_at,
        }


# ---------------------------------------------------------------------------
# Author normalization (caller display ⇄ stored natural-key ref)
# ---------------------------------------------------------------------------

def _author_to_stored(display: str, *, conn=None) -> str:
    """Normalize a caller-supplied author to the stored natural-key form.

      - 'system' / '' → SYSTEM_REF
      - 'agent:proj/scope' / 'scope:proj/scope' → 'agent:proj/scope'
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
    from awm.scopes.identity import user_id_for_username, username_for_user_id
    uid = user_id_for_username(display, conn=conn, create_if_missing=True)
    if uid:
        name = username_for_user_id(uid, conn=conn)
        return f"user:{name or display}"
    return f"user:{display}"


def _author_to_display(author_ref: str) -> str:
    """Render the stored author back to the display form."""
    if not author_ref or author_ref == SYSTEM_REF:
        return "system"
    if author_ref.startswith(("agent:", "user:")):
        return author_ref
    if author_ref.startswith("scope:"):
        return "agent:" + author_ref[len("scope:"):]
    if "/" in author_ref:
        return f"agent:{author_ref}"
    return author_ref


def _channel_key(project: str, scope: str) -> str:
    """In-memory subscriber-bus key for a channel."""
    return f"{project}/{scope}"


def _coerce_meta(raw) -> dict:
    """Decode/normalize a ``meta`` value into a dict, defensively.

    ``meta`` is stored as ``json.dumps(meta)``, so the read path gets a JSON
    string back. A well-behaved post stores a dict; but a harness that hands
    ``meta`` in as a JSON *string* gets it double-encoded, so ``json.loads``
    yields a ``str``. Recover that exact case with one more parse; anything that
    still isn't a dict becomes ``{}`` so a single malformed row can't crash a
    project-wide render or fail pydantic validation on ``ScopePost.meta``.

    Also used on the write path, where ``raw`` may already be a dict (the
    normal case, returned as-is) or a JSON string from a misbehaving harness.
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if isinstance(value, str):  # double-encoded: a JSON string holding JSON
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _row_to_post(row) -> ScopePost:
    meta = _coerce_meta(row["meta"])
    return ScopePost(
        id=row["id"], project=row["owner_project"], scope=row["owner_scope"],
        author=_author_to_display(row["author"]), kind=row["kind"],
        body=row["body"] or "", meta=meta, ts=ms_to_iso(row["ts"]) or "",
    )


def _row_to_subscriber(row) -> Subscriber:
    return Subscriber(
        project=row["owner_project"], scope=row["owner_scope"],
        guest_kind=row["guest_kind"], guest_ref=row["guest_ref"],
        display_name=row["display_name"] or "",
        joined_at=ms_to_iso(row["joined_at"]) or "",
    )


# ---------------------------------------------------------------------------
# In-process event bus for live (WS) subscribers
# ---------------------------------------------------------------------------

_subscribers: dict[str, set[asyncio.Queue]] = {}
_subscribers_lock = asyncio.Lock()


async def attach_live(project: str, scope: str, queue: asyncio.Queue) -> None:
    async with _subscribers_lock:
        _subscribers.setdefault(_channel_key(project, scope), set()).add(queue)


async def detach_live(project: str, scope: str, queue: asyncio.Queue) -> None:
    async with _subscribers_lock:
        key = _channel_key(project, scope)
        bucket = _subscribers.get(key)
        if bucket is None:
            return
        bucket.discard(queue)
        if not bucket:
            _subscribers.pop(key, None)


def _broadcast(project: str, scope: str, event: dict) -> None:
    bucket = _subscribers.get(_channel_key(project, scope))
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
# Delivery hook (agents service registers a sink to enqueue posts into stdin)
# ---------------------------------------------------------------------------

_delivery_sink = None  # Callable[(project, scope, ScopePost)] | None


def set_delivery_sink(fn) -> None:
    """Register an optional sink that receives every post destined for a
    channel whose owner scope runs a live agent. The agents service sets this
    (in-process when co-resident; otherwise it subscribes over the gateway)."""
    global _delivery_sink
    _delivery_sink = fn


# ---------------------------------------------------------------------------
# Cross-service emitter (the `posts` pub/sub topic the agents service subs to)
# ---------------------------------------------------------------------------

_emitter = None  # Callable[[dict], None] | None


def set_emitter(fn) -> None:
    """Register a fire-and-forget emitter called on every new post.

    The scopes service declares a ``posts`` emitter; on start the hub adapter
    sets this to a thread-safe scheduler that ``emit``s ``{project, scope,
    post}`` over the gateway. The agents service subscribes to it to feed human
    messages into a live agent's stdin (a live subscription, not a poll).
    ``post()`` runs in a worker thread, so the registered callable must hand
    the coroutine to the service's event loop itself."""
    global _emitter
    _emitter = fn


# ---------------------------------------------------------------------------
# Embeddings (degrade gracefully)
# ---------------------------------------------------------------------------

def _index_post(post_id: str, text: str) -> None:
    """Upsert a post embedding (source_type='post'). Silent no-op on failure."""
    try:
        from awm.persistence.embeddings import upsert_embedding
        from awm.persistence.databases import get_connection
        conn = get_connection("scopes")
        try:
            upsert_embedding(conn, "post", post_id, text[:500])
        finally:
            conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Post
# ---------------------------------------------------------------------------

def post(project: str, scope: str, *, author: str, body: str,
         kind: str = "message", meta: dict | None = None,
         to_scope: str | None = None) -> ScopePost:
    """Append a post to a scope's channel and fan it out to live subscribers
    and (if registered) the agent-delivery sink."""
    now = now_ms()
    pid = str(_uuid.uuid4())
    meta = _coerce_meta(meta)
    dao = ScopesDAO()
    with dao.transaction() as conn:
        author_ref = _author_to_stored(author, conn=conn)
        ScopesDAO(conn=conn).execute(
            "INSERT INTO scope_posts "
            "(id, owner_project, owner_scope, author, kind, body, meta, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, project, scope, author_ref, kind, body or "",
             json.dumps(meta), now),
        )
    row = ScopesDAO().query_one("SELECT * FROM scope_posts WHERE id=?", (pid,))
    post_obj = _row_to_post(row)

    _broadcast(project, scope, {"type": "post", "post": post_obj.to_dict()})
    # One post → one cross-service `emit` on the `posts` topic (fan-out to all
    # subscribers; each filters by its own (project, scope)).
    if _emitter is not None:
        try:
            _emitter({"project": project, "scope": scope,
                      "post": post_obj.to_dict()})
        except Exception:
            pass
    if _delivery_sink is not None and not (to_scope and to_scope != f"{project}/{scope}"):
        try:
            _delivery_sink(project, scope, post_obj)
        except Exception:
            pass
    # Index journals + messages + goals for hybrid search (cheap; bodies are
    # short). A goal left out here would be findable by keyword but not by
    # meaning, which is the half that matters for retrieving one.
    if kind in ("journal", "message", "goal") and body:
        _index_post(pid, f"{post_obj.author} {body}")
    return post_obj


# ---------------------------------------------------------------------------
# Fetch / search (the search/fetch pattern; no "history")
# ---------------------------------------------------------------------------

def get_post(post_id: str) -> ScopePost | None:
    if not isinstance(post_id, str):
        return None
    row = ScopesDAO().query_one("SELECT * FROM scope_posts WHERE id=?", (post_id,))
    return _row_to_post(row) if row else None


def fetch(*, project: str | None = None, scope: str | None = None,
          kind: str | None = None, query: str | None = None,
          author: str | None = None, limit: int = 50, offset: int = 0,
          before_ts: str | None = None, order: str | None = None) -> list[ScopePost]:
    """Pull / search posts.

    - ``scope`` given, no ``query`` → that channel's recent posts.
    - ``query`` given → hybrid keyword + semantic search (within the scope if
      given, else cross-scope).
    - ``kind`` narrows by post kind (e.g. ``'journal'`` for debrief entries).
    - ``author`` narrows by stored/display author ref.
    - ``order`` ∈ ``'asc'`` | ``'desc'`` forces oldest- or newest-first. With no
      ``order`` the default is oldest→newest for a single channel and
      newest-first for cross-scope/search. Use ``order='desc'`` with ``limit`` to
      pull the *last N* posts of a channel (e.g. the 5 most recent journal
      entries / session logs).
    """
    dao = ScopesDAO()
    sql = "SELECT * FROM scope_posts WHERE 1=1"
    params: list = []
    if project is not None:
        sql += " AND owner_project = ?"
        params.append(project)
    if scope is not None:
        sql += " AND owner_scope = ?"
        params.append(scope)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if author:
        sql += " AND author = ?"
        params.append(_author_to_stored(author))
    if query:
        sql += " AND body LIKE ?"
        params.append(f"%{query}%")
    if before_ts is not None:
        bms = iso_to_ms(before_ts)
        if bms is not None:
            sql += " AND ts < ?"
            params.append(bms)
    single_channel = scope is not None and not query
    if order in ("asc", "desc"):
        direction = order.upper()
    else:
        direction = "ASC" if single_channel else "DESC"
    sql += " ORDER BY ts {} LIMIT ? OFFSET ?".format(direction)
    params.extend([limit, offset])
    rows = dao.query_all(sql, params)
    posts = [_row_to_post(r) for r in rows]

    if not query:
        return posts

    keyword_keys = {p.id for p in posts}

    def _materialize(post_id: str):
        r = ScopesDAO().query_one("SELECT * FROM scope_posts WHERE id=?", (post_id,))
        if r is None:
            return None
        if project is not None and r["owner_project"] != project:
            return None
        if scope is not None and r["owner_scope"] != scope:
            return None
        if kind and r["kind"] != kind:
            return None
        return _row_to_post(r)

    try:
        from awm.persistence.embeddings import hybrid_augment
        from awm.persistence.databases import get_connection
        conn = get_connection("scopes")
        try:
            return hybrid_augment(
                conn, query, source_type="post",
                keyword_hits=posts, keyword_keys=keyword_keys,
                materialize=_materialize,
            )
        finally:
            conn.close()
    except Exception:
        return posts


# ---------------------------------------------------------------------------
# Subscribers
# ---------------------------------------------------------------------------

def _normalize_guest(guest: str) -> tuple[str, str, str]:
    """Map a guest ref to (guest_kind, guest_ref, display_name)."""
    g = guest
    if g.startswith(("agent:", "scope:")):
        g = g.split(":", 1)[1]
    if g.startswith("user:"):
        name = g[len("user:"):]
        return "user", f"user:{name}", name
    if "/" in g:
        return "agent", g, g
    # bare username
    return "user", f"user:{g}", g


def subscribe(project: str, scope: str, guest: str,
              display_name: str | None = None) -> Subscriber:
    """Enroll a guest (another scope or a user) as a subscriber of a channel."""
    guest_kind, guest_ref, default_name = _normalize_guest(guest)
    if guest_kind == "user":
        from awm.scopes.identity import user_id_for_username
        user_id_for_username(default_name, create_if_missing=True)
    now = now_ms()
    dao = ScopesDAO()
    with dao.transaction() as conn:
        ScopesDAO(conn=conn).execute(
            "INSERT OR IGNORE INTO scope_subscribers "
            "(owner_project, owner_scope, guest_kind, guest_ref, display_name, joined_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project, scope, guest_kind, guest_ref,
             display_name or default_name, now),
        )
    row = ScopesDAO().query_one(
        "SELECT * FROM scope_subscribers WHERE owner_project=? AND owner_scope=? "
        "AND guest_kind=? AND guest_ref=?",
        (project, scope, guest_kind, guest_ref),
    )
    sub = _row_to_subscriber(row)
    _broadcast(project, scope, {"type": "subscribed", "subscriber": sub.to_dict()})
    return sub


def unsubscribe(project: str, scope: str, guest: str) -> bool:
    """Remove a subscriber. Returns True if a row was deleted."""
    guest_kind, guest_ref, _ = _normalize_guest(guest)
    dao = ScopesDAO()
    existing = dao.query_one(
        "SELECT 1 FROM scope_subscribers WHERE owner_project=? AND owner_scope=? "
        "AND guest_kind=? AND guest_ref=?",
        (project, scope, guest_kind, guest_ref),
    )
    if existing is None:
        return False
    with dao.transaction() as conn:
        ScopesDAO(conn=conn).execute(
            "DELETE FROM scope_subscribers WHERE owner_project=? AND owner_scope=? "
            "AND guest_kind=? AND guest_ref=?",
            (project, scope, guest_kind, guest_ref),
        )
    _broadcast(project, scope, {
        "type": "unsubscribed",
        "subscriber": {"guest_kind": guest_kind, "guest_ref": guest_ref},
    })
    return True


def list_subscribers(project: str, scope: str) -> list[Subscriber]:
    rows = ScopesDAO().query_all(
        "SELECT * FROM scope_subscribers WHERE owner_project=? AND owner_scope=? "
        "ORDER BY joined_at",
        (project, scope),
    )
    return [_row_to_subscriber(r) for r in rows]


def channels_for_subscriber(guest: str) -> list[tuple[str, str]]:
    """Return (project, scope) channels the given guest subscribes to."""
    guest_kind, guest_ref, _ = _normalize_guest(guest)
    rows = ScopesDAO().query_all(
        "SELECT owner_project, owner_scope FROM scope_subscribers "
        "WHERE guest_kind=? AND guest_ref=?",
        (guest_kind, guest_ref),
    )
    return [(r["owner_project"], r["owner_scope"]) for r in rows]


# ---------------------------------------------------------------------------
# WS subscriber pump (live tail + post)
# ---------------------------------------------------------------------------

_WS_QUEUE_MAX = 256


async def run_subscriber_session(websocket, project: str, scope: str,
                                 user_as: str) -> None:
    """Drive a fully-attached WS subscriber for a scope channel."""
    from awm.scopes import ws_envelope as env

    queue: asyncio.Queue = asyncio.Queue(maxsize=_WS_QUEUE_MAX)
    await attach_live(project, scope, queue)

    backlog = fetch(project=project, scope=scope, limit=200)
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
                try:
                    post(project, scope, author=user_as,
                         body=msg.get("body", ""), kind=msg.get("kind", "message"),
                         to_scope=msg.get("to") or None)
                except ChannelError as exc:
                    await websocket.send_text(json.dumps(env.error(str(exc))))
            elif mtype == "ping":
                await websocket.send_text(json.dumps(env.pong()))
            else:
                await websocket.send_text(json.dumps(
                    env.error(f"unknown envelope type: {mtype}")))

    writer_task = asyncio.create_task(writer())
    reader_task = asyncio.create_task(reader())
    try:
        await asyncio.wait(
            {writer_task, reader_task}, return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        writer_task.cancel()
        reader_task.cancel()
        await detach_live(project, scope, queue)
        try:
            if websocket.client_state.name != "DISCONNECTED":
                await websocket.close()
        except Exception:
            pass
