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
from awm.journal import bounds, channel, owners, private, render, versions

log = logging.getLogger("awm.journal.posts")


POST_MANIFEST_FUNCTIONS = [
    {
        "name": "edit",
        "tool": "journal_edit",
        "effect": "write",
        "description": (
            "Correct a post in place. Give old and new to replace one passage, or body for the "
            "whole text. The journal keeps every earlier version with its diff; reads show "
            "the latest. Use this instead of posting an addendum."
        ),
        "params": [
            {"name": "post_id", "type": "string", "required": True},
            {"name": "reason", "type": "string", "required": False},
            {"name": "old", "type": "string", "required": False,
             "description": "A passage that occurs exactly once in the post."},
            {"name": "new", "type": "string", "required": False},
            {"name": "body", "type": "string", "required": False,
             "description": "The whole new text. Pass this argument LAST."},
        ],
    },
    {
        "name": "history",
        "tool": "journal_history",
        "effect": "read",
        "description": "Every version of a post, oldest first, each with its diff from the one before.",
        "params": [{"name": "post_id", "type": "string", "required": True}],
    },
    {
        "name": "addenda",
        "tool": "journal_addenda",
        "effect": "write",
        "description": (
            "List the posts that open as an addendum or correction to an earlier post of the "
            "same scope, with the post each corrects. With apply, fold each into its target as "
            "a new version and retire the addendum from reads."
        ),
        "params": [
            {"name": "project", "type": "string", "required": False},
            {"name": "scope", "type": "string", "required": False},
            {"name": "apply", "type": "boolean", "required": False},
        ],
    },
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
    bounds.write(bounds.of(args, as_), args["project"], args["scope"])
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


def _handle_fetch(args: dict, as_: str | None = None) -> dict:
    kind = args.get("kind")
    post_id = args.get("post_id")
    bound = bounds.of(args, as_)
    if post_id:
        p = channel.get_post(post_id)
        if p is not None and (private.hides(as_, p.project, p.scope)
                              or not bound.may_read(bounds.edges.here(), p.project, p.scope)):
            p = None
        return {"posts": [p.to_dict()] if p else [], "total": 1 if p else 0}
    project, scope = bounds.narrow(bound, args.get("project"), args.get("scope"))
    common = dict(
        project=project,
        scope=scope,
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
    bounds.write(bounds.of(args, as_), args["project"], args["scope"])
    if private.hides(as_, args["project"], args["scope"]):
        raise ValueError(f"no scope {args['project']}/{args['scope']}")
    sub = channel.subscribe(
        args["project"], args["scope"], args["guest"],
        display_name=args.get("display_name"),
    )
    return {"subscriber": sub.to_dict()}


def _handle_unsubscribe(args: dict, as_: str | None = None) -> dict:
    bounds.write(bounds.of(args, as_), args["project"], args["scope"])
    removed = channel.unsubscribe(args["project"], args["scope"], args["guest"])
    return {"removed": removed}


def _post_for_write(args: dict, as_: str | None):
    p = channel.get_post(args["post_id"])
    if p is None or private.hides(as_, p.project, p.scope):
        raise ValueError(f"no post {args['post_id']}")
    bounds.write(bounds.of(args, as_), p.project, p.scope)
    return p


def _editor(args: dict, as_: str | None) -> str:
    return channel.edge_origin(as_) or str(args.get("editor") or args.get("_caller_scope") or "system")


def _handle_edit(args: dict, as_: str | None = None) -> dict:
    _post_for_write(args, as_)
    return versions.edit(args["post_id"], editor=_editor(args, as_), body=args.get("body"),
                         old=args.get("old"), new=args.get("new"), reason=str(args.get("reason") or ""))


def _handle_history(args: dict, as_: str | None = None) -> dict:
    p = channel.get_post(args["post_id"])
    if p is None or private.hides(as_, p.project, p.scope):
        raise ValueError(f"no post {args['post_id']}")
    bounds.read(bounds.of(args, as_), p.project, p.scope)
    return versions.history(args["post_id"])


def _handle_addenda(args: dict, as_: str | None = None) -> dict:
    bound = bounds.of(args, as_)
    if bound.write_scope is not None or as_:
        raise bounds.Refused("only a trusted session on this node sweeps the addenda")
    return versions.sweep(args.get("project"), args.get("scope"), apply=bool(args.get("apply")),
                          editor=_editor(args, as_))


POST_HANDLERS = {
    "edit": _handle_edit,
    "history": _handle_history,
    "addenda": _handle_addenda,
    "post": _handle_post,
    "fetch": _handle_fetch,
    "subscribe": _handle_subscribe,
    "unsubscribe": _handle_unsubscribe,
}
