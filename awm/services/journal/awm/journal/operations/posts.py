"""Post-channel operation definitions for the gateway manifest.

A scope IS the channel. Each entry's ``name`` is the service-internal handle
(``/svc/journal/fn/<name>``) and its ``tool`` is the published ``journal_<verb>``:
  - ``post``        — append a post (message / journal / system).
  - ``fetch``       — fetch/search a scope's posts (``query`` → hybrid search,
                      ``kind`` filter).
  - ``subscribe``   — enroll a guest (scope or user) as a subscriber.
  - ``unsubscribe`` — remove a subscriber.
"""

import logging
from typing import Any

from awm.claudedaemon import roster
from awm.journal import channel, owners, render

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
            "Pass scope for one channel; add query to rank posts by relevance "
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
        return {"posts": [p.to_dict()] if p else [], "total": 1 if p else 0}
    common = dict(
        project=args.get("project"),
        scope=args.get("scope"),
        kind=kind,
        author=args.get("author"),
        limit=int(args.get("limit", 50)),
        offset=int(args.get("offset", 0)),
        before_ts=args.get("before_ts"),
        order=args.get("order") or (
            "desc" if kind == "journal" and not args.get("query") else None),
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


def _handle_subscribe(args: dict) -> dict:
    sub = channel.subscribe(
        args["project"], args["scope"], args["guest"],
        display_name=args.get("display_name"),
    )
    return {"subscriber": sub.to_dict()}


def _handle_unsubscribe(args: dict) -> dict:
    removed = channel.unsubscribe(args["project"], args["scope"], args["guest"])
    return {"removed": removed}


POST_HANDLERS = {
    "post": _handle_post,
    "fetch": _handle_fetch,
    "subscribe": _handle_subscribe,
    "unsubscribe": _handle_unsubscribe,
}
