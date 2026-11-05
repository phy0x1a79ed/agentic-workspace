"""The scope channel — a scope IS the channel.

There is no separate rooms/messages/session_logs machinery. Every scope owns
one post log (``scope_posts``); messages, journal (debrief) entries,
briefs and precedents are all rows there, told apart by their tags
(:mod:`awm.journal.tags`). Raw agent acts are NOT here; a session's own
transcript is read through the transcripts service.

Addressing is the scope's natural key ``(project, scope)``. For legacy
non-agent targets (a user/project/workspace inbox) the channel need not be a
literal worktree scope: ``project=''`` marks a non-literal channel whose ref
lives verbatim in ``scope`` (e.g. ``'user:alice'``, ``'project:awm'``,
``'workspace'``).

``reply_to`` takes a list (fleet.md R107 item 5). A comment names one post: it
lives in the channel of its thread's first post, wherever the commenter sits,
and ``thread_id`` names that first post. A debrief journal names every earlier
journal it built on: those are its citations (sessions.md R14, ``post_cites``),
and it stays in its writer's channel. Both links point one way.

All SQL goes through :class:`JournalDAO`.
"""

from __future__ import annotations

import json
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from awm.config import privacy
from awm.journal import builtin_tags, indexed, owners, tags as tagmod
from awm.journal.dao import JournalDAO
from awm.journal.refs import SYSTEM_REF
from awm.persistence.timestamps import iso_to_ms, ms_to_iso, now_ms


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
    tag_ids: list[str]
    body: str
    meta: dict
    ts: str              # ISO TEXT
    reply_to: str | None = None   # a comment's parent
    thread_id: str | None = None
    cites: list[str] = field(default_factory=list)  # a debrief journal's citations
    match: dict | None = None  # search hits only: {score, snippet}
    comments: int | None = None  # search hits and thread reads only
    version: int = 1

    @property
    def kind(self) -> str:
        """The old ``kind``, derived from the tags for readers that still ask for it."""
        return tagmod.derived_kind(self.tag_ids)

    @property
    def tags(self) -> list[str]:
        return tagmod.names_of(self.tag_ids)

    def has(self, tag_id: str) -> bool:
        return tag_id in self.tag_ids

    @property
    def root(self) -> str:
        return self.thread_id or self.id

    def to_dict(self) -> dict:
        d = {
            "id": self.id, "project": self.project, "scope": self.scope,
            "author": self.author, "tags": self.tags, "kind": self.kind,
            "body": self.body, "meta": self.meta, "ts": self.ts,
        }
        if self.reply_to:
            d["reply_to"] = [self.reply_to]
            d["thread"] = self.thread_id
        elif self.cites:
            d["reply_to"] = list(self.cites)
        if self.comments is not None:
            d["comments"] = self.comments
        if self.version > 1:
            d["version"] = self.version
        if self.match is not None:
            d["match"] = self.match
        return d


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
    """``as_`` when the edge stamped it as a peer identity, else ``None``."""
    return as_ if privacy.from_peer(as_) else None


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


def cites_of_posts(post_ids: list[str]) -> dict[str, list[str]]:
    """Each post's citations, in the order it gave them."""
    out: dict[str, list[str]] = {pid: [] for pid in post_ids}
    for i in range(0, len(post_ids), 500):
        chunk = post_ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for r in JournalDAO().query_all(
                f"SELECT post_id, cited_id FROM post_cites WHERE post_id IN ({marks})"
                " ORDER BY post_id, ord", chunk):
            out[r["post_id"]].append(r["cited_id"])
    return out


def _row_to_post(row, tag_ids: list[str] | None = None,
                 cites: list[str] | None = None) -> ScopePost:
    meta = _coerce_meta(row["meta"])
    if tag_ids is None:
        tag_ids = tagmod.of_posts([row["id"]])[row["id"]]
    if cites is None:
        cites = cites_of_posts([row["id"]])[row["id"]]
    keys = row.keys()
    return ScopePost(
        id=row["id"], project=row["owner_project"], scope=row["owner_scope"],
        author=_author_to_display(row["author"]), tag_ids=tag_ids,
        body=row["body"] or "", meta=meta, ts=ms_to_iso(row["ts"]) or "",
        reply_to=row["reply_to"] if "reply_to" in keys else None,
        thread_id=row["thread_id"] if "thread_id" in keys else None,
        cites=cites,
        version=int(row["version"]) if "version" in keys and row["version"] else 1,
    )


def rows_to_posts(rows) -> list[ScopePost]:
    """Posts from ``scope_posts`` rows, with one tag lookup for them all."""
    rows = list(rows)
    ids = [r["id"] for r in rows]
    by_post, cites = tagmod.of_posts(ids), cites_of_posts(ids)
    return [_row_to_post(r, by_post[r["id"]], cites[r["id"]]) for r in rows]


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

def post_tag_ids(tags: Any = None, extra: Iterable[str] = ()) -> list[str]:
    """The tag ids a new post carries: its ``tags`` and the ``extra`` ids the
    journal's own code adds. A post that names no type tag is a message.
    """
    ids = tagmod.resolve(tagmod.names_arg(tags))
    for t in extra:
        if t not in ids:
            ids.append(t)
    if not any(t in tagmod.TYPE_TAGS for t in ids):
        ids.append(builtin_tags.MESSAGE)
    return ids


def post(project: str, scope: str, *, author: str, body: str,
         tags: Any = None, meta: dict | None = None,
         reply_to: str | list[str] | None = None,
         origin: str | None = None, extra_tags: list[str] = (),
         ts: int | None = None) -> ScopePost:
    """Append a post to a scope's channel and hand it to the cross-service emitter.

    ``origin`` is an edge-stamped peer identity (see :func:`edge_origin`). It is
    stored verbatim as the author and ``author`` is ignored: a peer's claim about
    who it is never outranks the edge's. The claim is kept in
    ``meta["claimed_author"]`` so a reader can see which agent sent it.
    A caller with no stamp may not send an author of the ``peer:`` shape.

    ``reply_to`` on a journal lists the journals it cites, each of which must
    be a journal (sessions.md R17); the journal stays in ``project``/``scope``.
    On any other post it names the one post the comment answers, and the post
    lands in the channel of its thread's first post, whatever
    ``project``/``scope`` say. ``extra_tags`` are tag ids the journal's own code adds.
    """
    if origin is None and (author or "").startswith("peer:"):
        raise ValueError("author 'peer:...' is reserved for edge-stamped peer identities")
    tag_ids = post_tag_ids(tags, extra_tags)
    targets = reply_targets(reply_to)
    cites: list[str] = []
    reply_to = None
    if builtin_tags.JOURNAL in tag_ids:
        cites = require_journals(targets)
    elif len(targets) > 1:
        raise ValueError("a comment answers one post; only a debrief journal lists several")
    elif targets:
        reply_to = targets[0]
    thread_id = None
    if reply_to:
        parent = get_post(reply_to)
        if parent is None:
            raise ValueError(f"no post {reply_to} to reply to")
        thread_id = parent.root
        root = parent if parent.thread_id is None else get_post(thread_id) or parent
        project, scope = root.project, root.scope
    owners.require_scope(project, scope)
    now = ts if ts is not None else now_ms()
    pid = str(_uuid.uuid4())
    meta = dict(_coerce_meta(meta))
    if origin and author:
        meta["claimed_author"] = author
    dao = JournalDAO()
    with dao.transaction() as conn:
        author_ref = origin or _author_to_stored(author)
        JournalDAO(conn=conn).execute(
            "INSERT INTO scope_posts "
            "(id, owner_project, owner_scope, author, body, meta, ts, reply_to, thread_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, project, scope, author_ref, body or "", json.dumps(meta), now,
             reply_to or None, thread_id),
        )
        tagmod.attach(conn, pid, tag_ids)
        JournalDAO(conn=conn).executemany(
            "INSERT INTO post_cites (post_id, cited_id, ord) VALUES (?, ?, ?)",
            [(pid, c, i) for i, c in enumerate(cites)])
    post_obj = get_post(pid)

    # One post → one cross-service `emit` on the `posts` topic (fan-out to all
    # subscribers; each filters by its own (project, scope)).
    if _emitter is not None:
        try:
            _emitter({"project": project, "scope": scope,
                      "post": post_obj.to_dict()})
        except Exception:
            pass
    return post_obj


def reply_targets(raw: Any) -> list[str]:
    """``reply_to`` as a list of post ids: one id, or a list of them."""
    if raw in (None, "", []):
        return []
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, (list, tuple)) or not all(isinstance(x, str) and x for x in items):
        raise ValueError("reply_to is a post id or a list of post ids")
    return list(dict.fromkeys(items))


def require_journals(targets: list[str]) -> list[str]:
    """``targets`` when each is a journal post (sessions.md R17)."""
    found = tagmod.of_posts(targets)
    for t in targets:
        if builtin_tags.JOURNAL not in found.get(t, []):
            raise ValueError(f"citation {t} is not a journal post. Journal for that session first, "
                             "then cite the new journal (sessions.md R17)")
    return targets


def set_tags(post_id: str, *, add: Any = (), remove: Any = ()) -> ScopePost:
    """Add and remove several tags on one post at once (fleet.md R107 item 7)."""
    p = get_post(post_id)
    if p is None:
        raise ValueError(f"no post {post_id}")
    tagmod.retag(post_id, add=tagmod.names_arg(add), remove=tagmod.names_arg(remove))
    return get_post(post_id)


def mark(post_id: str, tag_id: str) -> None:
    """Add one of the code's own tags (closed, superseded) to a post."""
    dao = JournalDAO()
    with dao.transaction() as conn:
        tagmod.attach(conn, post_id, [tag_id])


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------

def comment_counts(root_ids: list[str]) -> dict[str, int]:
    if not root_ids:
        return {}
    marks = ",".join("?" * len(root_ids))
    out = {i: 0 for i in root_ids}
    for r in JournalDAO().query_all(
            f"SELECT thread_id, COUNT(*) AS n FROM scope_posts WHERE thread_id IN ({marks})"
            " GROUP BY thread_id", root_ids):
        out[r["thread_id"]] = r["n"]
    return out


def thread_posts(root_id: str) -> list[ScopePost]:
    """Every comment of a thread, oldest first."""
    return rows_to_posts(JournalDAO().query_all(
        "SELECT * FROM scope_posts WHERE thread_id=? ORDER BY ts ASC, id ASC", (root_id,)))


def nest(top: ScopePost, comments: list[ScopePost], visible=lambda p: True) -> list[dict]:
    """The comments under ``top`` as a tree: each a post dict with its own ``replies``."""
    children: dict[str, list[ScopePost]] = {}
    for c in comments:
        children.setdefault(c.reply_to, []).append(c)

    def walk(pid: str, seen: set) -> list[dict]:
        out = []
        for c in children.get(pid, []):
            if c.id in seen or not visible(c):
                continue
            seen.add(c.id)
            out.append({**c.to_dict(), "replies": walk(c.id, seen)})
        return out
    return walk(top.id, {top.id})


# ---------------------------------------------------------------------------
# Fetch / search (the search/fetch pattern; no "history")
# ---------------------------------------------------------------------------

def get_post(post_id: str) -> ScopePost | None:
    if not isinstance(post_id, str):
        return None
    row = JournalDAO().query_one("SELECT * FROM scope_posts WHERE id=?", (post_id,))
    return _row_to_post(row) if row else None


def _post_filter(project, scope, author, before_ts,
                 exclude: tuple[str, list] = ("", []), tags: Any = None) -> tuple[str, list]:
    sql, params = exclude[0], list(exclude[1])
    if project is not None:
        sql += " AND owner_project = ?"
        params.append(project)
    if scope is not None:
        sql += " AND owner_scope = ?"
        params.append(scope)
    tag_filter = tags
    tag_sql, tag_params = tagmod.filter_sql(tag_filter)
    sql += tag_sql
    params += tag_params
    if not any(name.lower() == "folded" for _, name in tagmod.parse_filter(tag_filter)):
        # An addendum folded into its target (versions.fold) stays out of the reads.
        sql += (" AND NOT EXISTS (SELECT 1 FROM post_tags fp"
                " WHERE fp.post_id = scope_posts.id AND fp.tag_id = ?)")
        params.append(builtin_tags.FOLDED)
    if author:
        sql += " AND author = ?"
        params.append(author if privacy.from_peer(author) else _author_to_stored(author))
    if before_ts is not None:
        bms = iso_to_ms(before_ts)
        if bms is not None:
            sql += " AND ts < ?"
            params.append(bms)
    return sql, params


def fetch(*, project: str | None = None, scope: str | None = None,
          query: str | None = None,
          author: str | None = None, limit: int = 50, offset: int = 0,
          before_ts: str | None = None, order: str | None = None,
          exclude: tuple[str, list] = ("", []), tags: Any = None) -> list[ScopePost]:
    """Pull / search posts.

    - ``scope`` given, no ``query`` → that channel's recent posts.
    - ``query`` given → :func:`search`, ranked by relevance (within the scope
      if given, else cross-scope).
    - ``tags`` narrows by a tag filter such as ``'journal and !closed'``.
    - ``author`` narrows by stored/display author ref.
    - ``order`` ∈ ``'asc'`` | ``'desc'`` forces oldest- or newest-first. With no
      ``order`` the default is oldest→newest for a single channel and
      newest-first cross-scope. Use ``order='desc'`` with ``limit`` to
      pull the *last N* posts of a channel (e.g. the 5 most recent journal
      entries / session logs).
    - ``exclude`` is a WHERE fragment from :func:`awm.journal.private.exclusion`.
    """
    if query:
        return search(project=project, scope=scope, query=query, author=author,
                      limit=limit, offset=offset, before_ts=before_ts, order=order,
                      exclude=exclude, tags=tags)[0]
    where, params = _post_filter(project, scope, author, before_ts, exclude, tags)
    if order in ("asc", "desc"):
        direction = order.upper()
    else:
        direction = "ASC" if scope is not None else "DESC"
    rows = JournalDAO().query_all(
        f"SELECT * FROM scope_posts WHERE 1=1{where} ORDER BY ts {direction} LIMIT ? OFFSET ?",
        [*params, limit, offset])
    return rows_to_posts(rows)


#: The search archive's name for the posts the journal exports to it.
SEARCH_SOURCE = "journals"
SEARCH_TIMEOUT_S = 30.0
#: The most hits search returns per section.
SEARCH_MAX = 100


def ranked(query: str, *, project: str | None, scope: str | None,
           before_ts: str | None, want: int) -> tuple[list[dict], dict | None]:
    """The search service's hits on the journal's own posts, best first: its ``exact``
    section, then ``related``. Raises when search cannot answer for the source."""
    from awm.gatewayclient import call_sync
    journals: dict = {}
    if project:
        journals["project"] = project
    if scope:
        journals["scope"] = scope
    if before_ts:
        journals["ts"] = {"$lt": before_ts}
    text = {"text": query, SEARCH_SOURCE: journals}
    res = call_sync("search", "query", {"query": text, "max": max(1, min(want, SEARCH_MAX))},
                    timeout=SEARCH_TIMEOUT_S)
    answer = res.get(SEARCH_SOURCE) or {}
    if answer.get("error") or "exact" not in answer:
        raise RuntimeError(f"search source {SEARCH_SOURCE}: {answer.get('error') or 'missing'}")
    hits, seen = [], set()
    for h in [*answer["exact"], *(answer.get("related") or [])]:
        if h.get("id") and h["id"] not in seen:
            seen.add(h["id"])
            hits.append(h)
    return hits, None


#: Characters of a post's body around the first match, as the hit's snippet.
SNIPPET_CHARS = 240


def _snippet(body: str, query: str) -> str:
    """The body around the query's first occurrence, else around its first word that occurs, else its start."""
    low = body.lower()
    words = [query.strip(), *query.split()]
    at = next((i for i in (low.find(w.lower()) for w in words if w) if i >= 0), 0)
    start = max(0, at - SNIPPET_CHARS // 4)
    return " ".join(body[start:start + SNIPPET_CHARS].split())


def search(*, query: str, project: str | None = None, scope: str | None = None,
           author: str | None = None, limit: int = 50,
           offset: int = 0, before_ts: str | None = None,
           order: str | None = None, exclude: tuple[str, list] = ("", []),
           tags: Any = None) -> tuple[list[ScopePost], dict | None, str]:
    """Thread first posts matching ``query``, best first, a ``degraded`` block, and
    which engine ranked them: ``search`` (the search service), or ``none`` for a
    substring match.

    The search service ranks the threads the journal exports to it, one document
    per thread (its first post with its comments), so a hit is a first post with
    its comment count (fleet.md R107 item 5). This module then keeps the hits its
    own filters allow, so the tag filter, the caller's privacy exclusion and
    ``author`` hold whatever search returned. ``order`` re-sorts by time. A
    filter without an indexed tag, or a search that cannot answer, falls back to
    a substring match.
    """
    where, params = _post_filter(project, scope, author, before_ts, exclude, tags)
    where += " AND thread_id IS NULL"
    degraded = None
    semantic = "none"
    hits = None
    expr = tags
    if indexed.may_hold(expr):
        narrowed = bool(author or exclude[0] or project == "" or tagmod.parse_filter(expr))
        try:
            hits, degraded = ranked(query, project=project, scope=scope, before_ts=before_ts,
                                    want=SEARCH_MAX if narrowed else offset + limit)
            semantic = "search"
        except Exception as exc:  # noqa: BLE001 — reported, then answered by keyword
            degraded = {"semantic": "error", "error": repr(exc)[:300], "fallback": "keyword"}
    if hits is None:
        rows = JournalDAO().query_all(
            f"SELECT * FROM scope_posts WHERE 1=1{where} AND (body LIKE ? OR EXISTS ("
            "SELECT 1 FROM scope_posts c WHERE c.thread_id = scope_posts.id AND c.body LIKE ?))"
            " ORDER BY ts DESC LIMIT ? OFFSET ?",
            [*params, f"%{query}%", f"%{query}%", limit, offset])
        posts = rows_to_posts(rows)
    else:
        ids = [h["id"] for h in hits]
        marks = ",".join("?" * len(ids))
        allowed = {r["id"]: r for r in JournalDAO().query_all(
            f"SELECT * FROM scope_posts WHERE id IN ({marks}){where}", [*ids, *params])} if ids else {}
        kept = [h for h in hits if h["id"] in allowed]
        posts = rows_to_posts(allowed[h["id"]] for h in kept)
        for rank, p in enumerate(posts, 1):
            p.match = {"rank": rank, "snippet": _snippet(p.body or "", query)}
        posts = posts[offset:offset + limit]
    counts = comment_counts([p.id for p in posts])
    for p in posts:
        p.comments = counts.get(p.id, 0)
    if order in ("asc", "desc"):
        posts.sort(key=lambda p: p.ts, reverse=order == "desc")
    return posts, degraded, semantic
