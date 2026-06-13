"""Scope-channel operation definitions for the gateway manifest.

A scope IS the channel. These replace the old room_*, inbox_*, and session_*
families with one small surface:
  - ``scope_post``        — append a post (message / journal / system).
  - ``scope_fetch``       — fetch/search a scope's posts (the search/fetch
                            pattern; ``query`` → hybrid search, ``kind`` filter).
  - ``scope_subscribe``   — enroll a guest (scope or user) as a subscriber.
  - ``scope_unsubscribe`` — remove a subscriber.

``scope_post`` is also the cross-service entry the agents service calls to
broadcast an agent's rendered output into its scope channel.
"""

from awm.scopes import channel


SCOPE_CHANNEL_MANIFEST_FUNCTIONS = [
    {
        "name": "scope_post",
        "description": (
            "Post to a scope's channel. kind ∈ message|journal|system. "
            "Journal entries are the scope's own debrief; structured fields go in meta."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "author", "type": "string", "required": True},
            {"name": "body", "type": "string", "required": True},
            {"name": "kind", "type": "string", "required": False},
            {"name": "meta", "type": "object", "required": False},
            {"name": "to_scope", "type": "string", "required": False},
        ],
    },
    {
        "name": "scope_fetch",
        "description": (
            "Fetch or search a scope's posts. Pass scope for one channel; add "
            "query for hybrid keyword+semantic search; omit scope to search "
            "across scopes; kind filters (e.g. 'journal'); post_id fetches one."
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
        ],
    },
    {
        "name": "scope_subscribe",
        "description": "Subscribe a guest (another scope 'project/scope', or 'user:<name>') to a scope's channel.",
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "guest", "type": "string", "required": True},
            {"name": "display_name", "type": "string", "required": False},
        ],
    },
    {
        "name": "scope_unsubscribe",
        "description": "Remove a guest from a scope's channel.",
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "guest", "type": "string", "required": True},
        ],
    },
]


def _handle_scope_post(args: dict) -> dict:
    post = channel.post(
        args["project"], args["scope"],
        author=args["author"], body=args["body"],
        kind=args.get("kind", "message"),
        meta=args.get("meta"),
        to_scope=args.get("to_scope"),
    )
    return {"post": post.to_dict()}


def _handle_scope_fetch(args: dict) -> dict:
    post_id = args.get("post_id")
    if post_id:
        p = channel.get_post(post_id)
        return {"posts": [p.to_dict()] if p else [], "total": 1 if p else 0}
    posts = channel.fetch(
        project=args.get("project"),
        scope=args.get("scope"),
        kind=args.get("kind"),
        query=args.get("query"),
        author=args.get("author"),
        limit=int(args.get("limit", 50)),
        offset=int(args.get("offset", 0)),
        before_ts=args.get("before_ts"),
    )
    return {"posts": [p.to_dict() for p in posts], "total": len(posts)}


def _handle_scope_subscribe(args: dict) -> dict:
    sub = channel.subscribe(
        args["project"], args["scope"], args["guest"],
        display_name=args.get("display_name"),
    )
    return {"subscriber": sub.to_dict()}


def _handle_scope_unsubscribe(args: dict) -> dict:
    removed = channel.unsubscribe(args["project"], args["scope"], args["guest"])
    return {"removed": removed}


SCOPE_CHANNEL_HANDLERS = {
    "scope_post": _handle_scope_post,
    "scope_fetch": _handle_scope_fetch,
    "scope_subscribe": _handle_scope_subscribe,
    "scope_unsubscribe": _handle_scope_unsubscribe,
}
