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
        "tool": "scope_post",
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
        # corrupt (kind/meta survive). Sibling ops (agent_post, scope_create) put
        # their free-text field last for the same reason. Do not move body up.
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
        "name": "scope_fetch",
        "tool": "scope_fetch",
        "description": (
            "Fetch or search a scope's posts — messages and journal entries. "
            "Pass scope for one channel; add query for hybrid keyword+semantic "
            "search; omit scope to search across scopes; kind filters (e.g. "
            "kind='journal' for session logs / debrief entries); post_id fetches "
            "one. For the most recent / last N entries pass order='desc' with "
            "limit (e.g. last 5 awm session logs: project='awm', kind='journal', "
            "limit=5, order='desc')."
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
             "description": "'asc' (oldest-first) or 'desc' (newest-first). Use 'desc' with limit for the last N."},
        ],
    },
    {
        "name": "scope_subscribe",
        "tool": "scope_subscribe",
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
        "tool": "scope_unsubscribe",
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
        order=args.get("order"),
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
