"""Post-channel operation definitions for the gateway manifest.

A scope IS the channel. Each entry's ``name`` is the service-internal handle
(``/svc/journal/fn/<name>``) and its ``tool`` is the published ``journal_<verb>``:
  - ``post``        — append a post (message / journal / system).
  - ``fetch``       — fetch/search a scope's posts (``query`` → hybrid search,
                      ``kind`` filter).
  - ``subscribe``   — enroll a guest (scope or user) as a subscriber.
  - ``unsubscribe`` — remove a subscriber.
  - ``inbox``       — a bounded one-line-per-post digest of what a scope has not read.
  - ``ack``         — move a scope's read cursor.
"""

import logging
from typing import Any

from awm.claudedaemon import roster
from awm.journal import channel, inbox, owners, private, render

log = logging.getLogger("awm.journal.posts")


POST_MANIFEST_FUNCTIONS = [
    {
        "name": "post",
        "tool": "journal_post",
        "effect": "write",
        "description": (
            "Post to a scope's channel. kind ∈ message|journal|system. "
            "Journal entries are the scope's own debrief; structured fields go in meta. "
            "Pass all structured params (kind, meta, to_scope) BEFORE the free-text "
            "body, which must be the LAST argument — see the body param note."
        ),
        # body is declared LAST on purpose. The harness serializes a tool call as
        # ordered <parameter> blocks; a long multi-line value can bleed past its
        # closing tag and swallow any parameter emitted AFTER it. Keeping the one
        # long free-text field terminal means a bleed has no trailing param to
        # corrupt (kind/meta survive). Do not move body up.
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "author", "type": "string", "required": True},
            {"name": "kind", "type": "string", "required": False},
            {"name": "meta", "type": "object", "required": False},
            {"name": "to_scope", "type": "string", "required": False},
            {"name": "body", "type": "string", "required": True,
             "description": "Free-text post content. Pass this argument LAST — "
                            "emit every other parameter before it."},
        ],
    },
    {
        "name": "fetch",
        "tool": "journal_fetch",
        "effect": "read",
        "timeout": 60,
        "category": "journals",
        "description": (
            "Fetch or search a scope's posts — messages and journal entries. "
            "Pass project and scope for one channel (a literal scope needs its "
            "project: scope names repeat across projects); add query to rank posts by relevance "
            "(meaning and keywords over each post's full text, best first, each "
            "hit with match.snippet — a limit of ~10 is plenty; the reply's "
            "`semantic` says which index ranked them: local, or none for a "
            "substring match); omit scope to "
            "search across scopes; kind filters (e.g. "
            "kind='journal' for session logs / debrief entries); post_id fetches "
            "one. Without a query, kind='journal' reads newest-first "
            "(e.g. last 5 awm session logs: project='awm', kind='journal', "
            "limit=5)."
        ),
        "params": [
            {"name": "project", "type": "string", "required": False},
            {"name": "scope", "type": "string", "required": False},
            {"name": "kind", "type": "string", "required": False},
            {"name": "query", "type": "string", "required": False},
            {"name": "author", "type": "string", "required": False},
            {"name": "post_id", "type": "string", "required": False},
            {"name": "limit", "type": "integer", "required": False},
            {"name": "offset", "type": "integer", "required": False},
            {"name": "before_ts", "type": "string", "required": False},
            {"name": "order", "type": "string", "required": False,
             "description": "'asc' (oldest-first) or 'desc' (newest-first). Default: 'desc' for kind='journal', else 'asc' within one scope."},
        ],
    },
    {
        "name": "subscribe",
        "tool": "journal_subscribe",
        "effect": "write",
        "description": "Subscribe a guest (another scope 'project/scope', or 'user:<name>') to a scope's channel.",
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "guest", "type": "string", "required": True},
            {"name": "display_name", "type": "string", "required": False},
        ],
    },
    {
        "name": "unsubscribe",
        "tool": "journal_unsubscribe",
        "effect": "write",
        "description": "Remove a guest from a scope's channel.",
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "guest", "type": "string", "required": True},
        ],
    },
    {
        "name": "inbox",
        "tool": "journal_inbox",
        "effect": "read",
        "description": (
            "What a scope has not read yet: one line per post, newest first, bounded. "
            "Each line has the post id, author, age, a gist (its title, leading "
            "heading or first sentence), whether it looks like it asks for action, "
            "and its length. Read one in full with fetch(post_id=…). The read "
            "cursor belongs to the scope, so the next session there starts where "
            "this one acknowledged. Posts the scope wrote itself are never unread. "
            "Mark read with ack(through=<ack_through>)."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "kind", "type": "string", "required": False,
             "description": "Post kind to list. Default 'message'."},
            {"name": "limit", "type": "integer", "required": False,
             "description": f"Posts to list. Default {inbox.DEFAULT_LIMIT}, at most {inbox.MAX_LIMIT}."},
            {"name": "all", "type": "boolean", "required": False,
             "description": "List read posts too, each marked read. Default false."},
        ],
    },
    {
        "name": "ack",
        "tool": "journal_ack",
        "effect": "write",
        "description": (
            "Mark a scope's posts read through one post: it and every post before "
            "it. Pass the ack_through id that inbox returned, so a post that "
            "arrived after you read the digest stays unread. Without through, "
            "marks everything in the channel read. The cursor never moves back."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "through", "type": "string", "required": False,
             "description": "A post id of this channel. Default: its newest post."},
        ],
    },
]


def _with_session(meta: Any, caller_pid: Any) -> Any:
    """``meta`` with ``session`` set to the posting session's id, from the pid the
    gateway stamped. A ``session`` the caller supplied is dropped either way."""
    rec = roster.session_by_pid(caller_pid) if isinstance(caller_pid, int) else None
    session = rec.get("sessionId") if rec else None
    if not isinstance(meta, dict):
        return {"session": session} if session else meta
    meta = {k: v for k, v in meta.items() if k != "session"}
    return {**meta, "session": session} if session else meta


def _handle_post(args: dict, as_: str | None = None) -> dict:
    if private.hides(as_, args["project"], args["scope"]):
        raise ValueError(f"no scope {args['project']}/{args['scope']}")
    post = channel.post(
        args["project"], args["scope"],
        author=args["author"], body=args["body"],
        kind=args.get("kind", "message"),
        meta=_with_session(args.get("meta"), args.get("_caller_pid")),
        to_scope=args.get("to_scope"),
        origin=channel.edge_origin(as_),
    )
    # A journal post is a scope's debrief entry; re-render its history.md so the
    # next session sees it. Best-effort: a render failure (scope dir absent)
    # must never fail the post.
    if args.get("kind") == "journal" and args["project"]:
        try:
            render.render(args["project"], args["scope"])
        except Exception as exc:  # noqa: BLE001
            log.info("history.md not refreshed for %s/%s: %s", args["project"], args["scope"], exc)
    return {"post": post.to_dict()}


def _is_non_literal(scope: str) -> bool:
    """Whether ``scope`` names a non-literal channel (``user:…``, ``project:…``, ``workspace``)."""
    return ":" in scope or scope == "workspace"


def _channel_project(project: Any, scope: Any) -> Any:
    """The project to filter a scope's posts by.

    A literal scope name repeats across projects (``dev`` lives in most of them),
    so a scope with no project would read every project's channel of that name
    as if it were one. Refuse that and name the projects that have it. A
    non-literal channel lives at ``project=''``.
    """
    if not scope or project is not None:
        return project
    if _is_non_literal(scope):
        return ""
    rows = channel.JournalDAO().query_all(
        "SELECT DISTINCT owner_project FROM scope_posts WHERE owner_scope=? "
        "AND owner_project != '' ORDER BY owner_project LIMIT 7", (scope,))
    have = [r["owner_project"] for r in rows]
    named = ", ".join(have[:6]) + (", …" if len(have) > 6 else "")
    raise ValueError(
        f"scope {scope!r} needs project: scope names repeat across projects"
        + (f" ({scope!r} has posts in {named})" if have else "")
        + f". Pass project=<p>. For the messages waiting for you, use "
        f"journal(verb=\"inbox\", args={{project:<p>, scope:{scope!r}}}).")


def _handle_fetch(args: dict, as_: str | None = None) -> dict:
    # The verb's category grants journals; a foreign caller sees no other kind.
    foreign = channel.is_foreign(as_)
    kind = args.get("kind")
    if foreign:
        if kind and kind != "journal":
            raise PermissionError("peers may read journal posts only")
        kind = "journal"
    post_id = args.get("post_id")
    if post_id:
        p = channel.get_post(post_id)
        if p is not None and foreign and p.kind != "journal":
            p = None  # answer as for a post that does not exist, so ids cannot be probed
        if p is not None and private.hides(as_, p.project, p.scope):
            p = None
        return {"posts": [p.to_dict()] if p else [], "total": 1 if p else 0}
    common = dict(
        project=_channel_project(args.get("project"), args.get("scope")),
        scope=args.get("scope"),
        kind=kind,
        author=args.get("author"),
        limit=int(args.get("limit", 50)),
        offset=int(args.get("offset", 0)),
        before_ts=args.get("before_ts"),
        order=args.get("order") or (
            "desc" if kind == "journal" and not args.get("query") else None),
        exclude=private.exclusion(as_),
    )
    degraded = semantic = None
    if args.get("query"):
        posts, degraded, semantic = channel.search(query=args["query"], **common)
    else:
        posts = channel.fetch(**common)
    out = {"posts": [p.to_dict() for p in posts], "total": len(posts)}
    if semantic:
        out["semantic"] = semantic
    if degraded:
        out["degraded"] = degraded
    return out


def _handle_subscribe(args: dict, as_: str | None = None) -> dict:
    if private.hides(as_, args["project"], args["scope"]):
        raise ValueError(f"no scope {args['project']}/{args['scope']}")
    sub = channel.subscribe(
        args["project"], args["scope"], args["guest"],
        display_name=args.get("display_name"),
    )
    return {"subscriber": sub.to_dict()}


def _handle_unsubscribe(args: dict) -> dict:
    removed = channel.unsubscribe(args["project"], args["scope"], args["guest"])
    return {"removed": removed}


def _handle_inbox(args: dict, as_: str | None = None) -> dict:
    project, scope = args["project"], args["scope"]
    if channel.is_foreign(as_) or private.hides(as_, project, scope):
        raise ValueError(f"no scope {project}/{scope}")
    owners.require_scope(project, scope)
    return inbox.digest(project, scope, kind=args.get("kind") or "message",
                        limit=int(args.get("limit") or inbox.DEFAULT_LIMIT),
                        include_read=args.get("all") in (True, "true", "True", "1", 1),
                        exclude=private.exclusion(as_))


def _handle_ack(args: dict, as_: str | None = None) -> dict:
    project, scope = args["project"], args["scope"]
    if channel.is_foreign(as_) or private.hides(as_, project, scope):
        raise ValueError(f"no scope {project}/{scope}")
    owners.require_scope(project, scope)
    by = _with_session({}, args.get("_caller_pid")).get("session") or as_
    return inbox.ack(project, scope, through=args.get("through"), by=by)


POST_HANDLERS = {
    "post": _handle_post,
    "fetch": _handle_fetch,
    "subscribe": _handle_subscribe,
    "unsubscribe": _handle_unsubscribe,
    "inbox": _handle_inbox,
    "ack": _handle_ack,
}
