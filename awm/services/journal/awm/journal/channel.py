"""The scope channel — a scope IS the channel.

There is no separate rooms/messages/session_logs machinery. Every scope owns
one append-only post log (``scope_posts``); messages, journal (debrief)
entries, and system notices are all rows there, differentiated by ``kind``.
Other scopes/users subscribe to a channel (``scope_subscribers``); the owner is
implicit (the scope itself). Raw agent acts are NOT here; a session's own
transcript is read through the transcripts service.

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
  - ``goal``    — what the user is after at this level; see :mod:`awm.journal.goals`.

All SQL goes through :class:`JournalDAO`.
"""

from __future__ import annotations

import json
import uuid as _uuid
from dataclasses import dataclass

from awm.journal import owners, search_index
from awm.journal.dao import JournalDAO
from awm.journal.refs import SYSTEM_REF, ms_to_iso, now_ms, iso_to_ms


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
    match: dict | None = None  # search hits only: {score, snippet}

    def to_dict(self) -> dict:
        d = {
            "id": self.id, "project": self.project, "scope": self.scope,
            "author": self.author, "kind": self.kind, "body": self.body,
            "meta": self.meta, "ts": self.ts,
        }
        if self.match is not None:
            d["match"] = self.match
        return d


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

def _author_to_stored(display: str) -> str:
    """Normalize a caller-supplied author to the stored natural-key form.

      - 'system' / '' → SYSTEM_REF
      - 'agent:proj/scope' / 'scope:proj/scope' → 'agent:proj/scope'
      - 'user:name' → 'user:name'
      - 'proj/scope' → 'agent:proj/scope'
      - bare username → 'user:<name>'
    """
    if not display or display == "system":
        return SYSTEM_REF
    if display.startswith("agent:") or display.startswith("user:"):
        return display
    if display.startswith("scope:"):
        return "agent:" + display[len("scope:"):]
    if "/" in display:
        return f"agent:{display}"
    return f"user:{display}"


def edge_origin(as_: str | None) -> str | None:
    """``as_`` when the edge stamped it as a peer identity (``peer`` or ``peer:<node>``), else ``None``."""
    if as_ == "peer" or (as_ or "").startswith("peer:"):
        return as_
    return None


def is_foreign(as_: str | None) -> bool:
    """Whether the edge stamped ``as_`` as a foreign node, by the gateway gate's rule.

    A ``peer:<node>`` stamp is foreign unless the peer book calls ``<node>``
    domestic; a node the book does not know, or a stamp naming no node, is
    foreign. The bare legacy ``peer`` and an absent identity are not.
    """
    if not isinstance(as_, str) or not as_.startswith("peer:"):
        return False
    from awm import config
    node = config.caller_peer(as_)
    record = config.peer_record(node) if node else None
    return record is None or record["relation"] != "domestic"


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
# Cross-service emitter (the `posts` pub/sub topic)
# ---------------------------------------------------------------------------

_emitter = None  # Callable[[dict], None] | None


def set_emitter(fn) -> None:
    """Register a fire-and-forget emitter called on every new post.

    The journal service declares a ``posts`` emitter; on start the hub adapter
    sets this to a thread-safe scheduler that ``emit``s ``{project, scope,
    post}`` over the gateway. ``post()`` runs in a worker thread, so the registered callable must hand
    the coroutine to the service's event loop itself."""
    global _emitter
    _emitter = fn


# ---------------------------------------------------------------------------
# Post
# ---------------------------------------------------------------------------

def post(project: str, scope: str, *, author: str, body: str,
         kind: str = "message", meta: dict | None = None,
         to_scope: str | None = None, origin: str | None = None) -> ScopePost:
    """Append a post to a scope's channel and hand it to the cross-service emitter.

    ``origin`` is an edge-stamped peer identity (see :func:`edge_origin`). It is
    stored verbatim as the author and ``author`` is ignored: a peer's claim about
    who it is never outranks the edge's. The claim is kept in
    ``meta["claimed_author"]`` so a domestic reader can see which agent sent it.
    A caller with no stamp may not send an author of the ``peer:`` shape.
    """
    if origin is None and (author or "").startswith("peer:"):
        raise ValueError("author 'peer:...' is reserved for edge-stamped peer identities")
    owners.require_scope(project, scope)
    now = now_ms()
    pid = str(_uuid.uuid4())
    meta = dict(_coerce_meta(meta))
    if origin and author:
        meta["claimed_author"] = author
    dao = JournalDAO()
    with dao.transaction() as conn:
        author_ref = origin or _author_to_stored(author)
        JournalDAO(conn=conn).execute(
            "INSERT INTO scope_posts "
            "(id, owner_project, owner_scope, author, kind, body, meta, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, project, scope, author_ref, kind, body or "",
             json.dumps(meta), now),
        )
    row = JournalDAO().query_one("SELECT * FROM scope_posts WHERE id=?", (pid,))
    post_obj = _row_to_post(row)

    # One post → one cross-service `emit` on the `posts` topic (fan-out to all
    # subscribers; each filters by its own (project, scope)).
    if _emitter is not None:
        try:
            _emitter({"project": project, "scope": scope,
                      "post": post_obj.to_dict()})
        except Exception:
            pass
    if kind in search_index.INDEXED_KINDS and body:
        search_index.index_post(pid)
    return post_obj


# ---------------------------------------------------------------------------
# Fetch / search (the search/fetch pattern; no "history")
# ---------------------------------------------------------------------------

def get_post(post_id: str) -> ScopePost | None:
    if not isinstance(post_id, str):
        return None
    row = JournalDAO().query_one("SELECT * FROM scope_posts WHERE id=?", (post_id,))
    return _row_to_post(row) if row else None


def _post_filter(project, scope, kind, author, before_ts) -> tuple[str, list]:
    sql, params = "", []
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
        params.append(author if edge_origin(author) else _author_to_stored(author))
    if before_ts is not None:
        bms = iso_to_ms(before_ts)
        if bms is not None:
            sql += " AND ts < ?"
            params.append(bms)
    return sql, params


def fetch(*, project: str | None = None, scope: str | None = None,
          kind: str | None = None, query: str | None = None,
          author: str | None = None, limit: int = 50, offset: int = 0,
          before_ts: str | None = None, order: str | None = None) -> list[ScopePost]:
    """Pull / search posts.

    - ``scope`` given, no ``query`` → that channel's recent posts.
    - ``query`` given → :func:`search`, ranked by relevance (within the scope
      if given, else cross-scope).
    - ``kind`` narrows by post kind (e.g. ``'journal'`` for debrief entries).
    - ``author`` narrows by stored/display author ref.
    - ``order`` ∈ ``'asc'`` | ``'desc'`` forces oldest- or newest-first. With no
      ``order`` the default is oldest→newest for a single channel and
      newest-first cross-scope. Use ``order='desc'`` with ``limit`` to
      pull the *last N* posts of a channel (e.g. the 5 most recent journal
      entries / session logs).
    """
    if query:
        return search(project=project, scope=scope, kind=kind, query=query, author=author,
                      limit=limit, offset=offset, before_ts=before_ts, order=order)[0]
    where, params = _post_filter(project, scope, kind, author, before_ts)
    if order in ("asc", "desc"):
        direction = order.upper()
    else:
        direction = "ASC" if scope is not None else "DESC"
    rows = JournalDAO().query_all(
        f"SELECT * FROM scope_posts WHERE 1=1{where} ORDER BY ts {direction} LIMIT ? OFFSET ?",
        [*params, limit, offset])
    return [_row_to_post(r) for r in rows]


def search(*, query: str, project: str | None = None, scope: str | None = None,
           kind: str | None = None, author: str | None = None, limit: int = 50,
           offset: int = 0, before_ts: str | None = None,
           order: str | None = None) -> tuple[list[ScopePost], dict | None, str]:
    """Posts matching ``query`` by meaning and keyword, best first, a ``degraded`` block,
    and which engine ranked them: ``local``, or ``none`` for a substring match.

    The filters narrow the candidates before ranking. ``order`` re-sorts the
    selected posts by time. A kind the index does not hold (``system``) falls
    back to a substring match.
    """
    where, params = _post_filter(project, scope, kind, author, before_ts)
    degraded = None
    semantic = "none"
    indexed = not kind or kind in search_index.INDEXED_KINDS
    if not indexed:
        ids = None
    else:
        try:
            res = search_index.search("post", query, allowed=f"SELECT id FROM scope_posts WHERE 1=1{where}",
                                      params=params, limit=offset + limit)
            ids, degraded = [h["source_id"] for h in res.hits][offset:], res.degraded
            matches = {h["source_id"]: h for h in res.hits}
            semantic = "local"
        except Exception as exc:  # noqa: BLE001 — reported, then answered by keyword
            ids = None
            degraded = {"semantic": "error", "error": repr(exc)[:300], "fallback": "keyword"}
    if ids is None:
        rows = JournalDAO().query_all(
            f"SELECT * FROM scope_posts WHERE 1=1{where} AND body LIKE ?"
            " ORDER BY ts DESC LIMIT ? OFFSET ?", [*params, f"%{query}%", limit, offset])
        posts = [_row_to_post(r) for r in rows]
    else:
        marks = ",".join("?" * len(ids))
        by_id = {r["id"]: r for r in JournalDAO().query_all(
            f"SELECT * FROM scope_posts WHERE id IN ({marks})", ids)} if ids else {}
        posts = [_row_to_post(by_id[i]) for i in ids if i in by_id]
        for p in posts:
            h = matches[p.id]
            p.match = {"score": h["score"], "snippet": h["snippet"]}
    if order in ("asc", "desc"):
        posts.sort(key=lambda p: p.ts, reverse=order == "desc")
    return posts, degraded, semantic


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
    now = now_ms()
    dao = JournalDAO()
    with dao.transaction() as conn:
        JournalDAO(conn=conn).execute(
            "INSERT OR IGNORE INTO scope_subscribers "
            "(owner_project, owner_scope, guest_kind, guest_ref, display_name, joined_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project, scope, guest_kind, guest_ref,
             display_name or default_name, now),
        )
    row = JournalDAO().query_one(
        "SELECT * FROM scope_subscribers WHERE owner_project=? AND owner_scope=? "
        "AND guest_kind=? AND guest_ref=?",
        (project, scope, guest_kind, guest_ref),
    )
    sub = _row_to_subscriber(row)
    return sub


def unsubscribe(project: str, scope: str, guest: str) -> bool:
    """Remove a subscriber. Returns True if a row was deleted."""
    guest_kind, guest_ref, _ = _normalize_guest(guest)
    dao = JournalDAO()
    existing = dao.query_one(
        "SELECT 1 FROM scope_subscribers WHERE owner_project=? AND owner_scope=? "
        "AND guest_kind=? AND guest_ref=?",
        (project, scope, guest_kind, guest_ref),
    )
    if existing is None:
        return False
    with dao.transaction() as conn:
        JournalDAO(conn=conn).execute(
            "DELETE FROM scope_subscribers WHERE owner_project=? AND owner_scope=? "
            "AND guest_kind=? AND guest_ref=?",
            (project, scope, guest_kind, guest_ref),
        )
    return True


def list_subscribers(project: str, scope: str) -> list[Subscriber]:
    rows = JournalDAO().query_all(
        "SELECT * FROM scope_subscribers WHERE owner_project=? AND owner_scope=? "
        "ORDER BY joined_at",
        (project, scope),
    )
    return [_row_to_subscriber(r) for r in rows]


def channels_for_subscriber(guest: str) -> list[tuple[str, str]]:
    """Return (project, scope) channels the given guest subscribes to."""
    guest_kind, guest_ref, _ = _normalize_guest(guest)
    rows = JournalDAO().query_all(
        "SELECT owner_project, owner_scope FROM scope_subscribers "
        "WHERE guest_kind=? AND guest_ref=?",
        (guest_kind, guest_ref),
    )
    return [(r["owner_project"], r["owner_scope"]) for r in rows]
