"""Post-channel operation definitions for the gateway manifest.

A scope IS the channel. Each entry's ``name`` is the service-internal handle
(``/svc/journal/fn/<name>``) and its ``tool`` is the published ``journal_<verb>``:
  - ``post``        — append a post with its tags, or a comment with ``reply_to``.
  - ``fetch``       — fetch/search a scope's posts (``query`` → hybrid search,
                      ``tags`` filter; a post_id read returns its thread).
  - ``subscribe``   — enroll a guest (scope or user) as a subscriber.
  - ``unsubscribe`` — remove a subscriber.
  - ``reads``       — the journals a session read, its citation candidates.
  - ``inbox``       — a bounded one-line-per-post digest of what a scope has not read.
  - ``ack``         — move a scope's read cursor.
"""

import logging
from typing import Any

from awm.gatewayclient import sessions as caller
from awm.journal import channel, inbox, kinds, owners, private, sessions, tagsync
from awm.journal import tags as tagmod

log = logging.getLogger("awm.journal.posts")


POST_MANIFEST_FUNCTIONS = [
    {
        "name": "post",
        "tool": "journal_post",
        "effect": "write",
        "description": (
            "Post to a scope's channel. tags come from the vocabulary (tag_list); a post "
            "with none is a message. tags=[journal] is the scope's own debrief; structured "
            "fields go in meta. reply_to=[<post id>] makes the post a comment: it lands "
            "in the channel of its thread's first post, and that post's author gets it in "
            "its inbox. On a journal, reply_to lists the journals it built on. "
            "Tag a post precedent when it records a decision Tony made or "
            "overrode that holds beyond the session: post it at project='' "
            "scope='workspace', with a body that holds the context, the question and "
            "the decision. Search precedents before you ask Tony a preference question. "
            "Pass all structured params (tags, meta, reply_to) BEFORE the "
            "free-text body, which must be the LAST argument — see the body param note."
        ),
        # body is declared LAST on purpose. The harness serializes a tool call as
        # ordered <parameter> blocks; a long multi-line value can bleed past its
        # closing tag and swallow any parameter emitted AFTER it. Keeping the one
        # long free-text field terminal means a bleed has no trailing param to
        # corrupt (kind/meta survive). Do not move body up.
        "params": [
            {"name": "project", "type": "string", "required": False,
             "description": "Required unless reply_to is given."},
            {"name": "scope", "type": "string", "required": False,
             "description": "Required unless reply_to is given."},
            {"name": "author", "type": "string", "required": True},
            {"name": "tags", "type": "array", "required": False,
             "description": "Tag names from the vocabulary. An unknown tag is refused."},
            {"name": "kind", "type": "string", "required": False,
             "description": "Old alias for one tag, such as journal. Use tags."},
            {"name": "reply_to", "type": "array", "required": False,
             "description": "Post ids. A comment names the one post it answers. A debrief "
                            "journal names every earlier journal it built on: its citations, "
                            "picked from journal reads. A journal stays in its own scope."},
            {"name": "meta", "type": "object", "required": False,
             "description": "Structured fields. meta.session names the session a journal covers; "
                            "omit it for your own session."},
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
            "search across scopes; a query returns each thread's first post with its "
            "comment count. tags filters with 'and' and '!' (e.g. tags='journal' for "
            "session logs, '!closed and complaint' for open complaints); post_id fetches "
            "one with its thread of comments. Without a query, tags='journal' reads "
            "newest-first (e.g. last 5 awm session logs: project='awm', tags='journal', "
            "limit=5)."
        ),
        "params": [
            {"name": "project", "type": "string", "required": False},
            {"name": "scope", "type": "string", "required": False},
            {"name": "tags", "type": "string", "required": False,
             "description": "A tag filter: names joined by 'and', '!' negates one."},
            {"name": "kind", "type": "string", "required": False,
             "description": "Old alias for one tag. Use tags."},
            {"name": "query", "type": "string", "required": False},
            {"name": "author", "type": "string", "required": False},
            {"name": "post_id", "type": "string", "required": False},
            {"name": "limit", "type": "integer", "required": False},
            {"name": "offset", "type": "integer", "required": False},
            {"name": "before_ts", "type": "string", "required": False},
            {"name": "order", "type": "string", "required": False,
             "description": "'asc' (oldest-first) or 'desc' (newest-first). Default: 'desc' for tags='journal', else 'asc' within one scope."},
        ],
    },
    {
        "name": "reads",
        "tool": "journal_reads",
        "effect": "read",
        "description": (
            "The journals a session read through fetch, newest first: the candidates "
            "to cite at debrief. Also lists the citations its own journals already make. "
            "session defaults to yours."
        ),
        "params": [
            {"name": "session", "type": "string", "required": False},
            {"name": "limit", "type": "integer", "required": False},
        ],
    },
    {
        "name": "note_read",
        "tool": "journal_note_read",
        "effect": "write",
        "description": (
            "Record that the calling session read one journal post through another "
            "service, such as search get (sessions.md R15). For services, not agents."
        ),
        "params": [{"name": "post_id", "type": "string", "required": True}],
        "surfaces": ["cli", "http"],
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
            "and its length. It also lists comments on threads the scope started, "
            "wherever they live. Read one in full with fetch(post_id=…). The read "
            "cursor belongs to the scope, so the next session there starts where "
            "this one acknowledged. Posts the scope wrote itself are never unread. "
            "Mark read with ack(through=<ack_through>)."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "tags", "type": "string", "required": False,
             "description": "A tag filter. Default 'message'."},
            {"name": "kind", "type": "string", "required": False,
             "description": "Old alias for one tag. Use tags."},
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


def _caller_session(caller_pid: Any) -> str | None:
    """The R9 id of the session behind the pid the gateway stamped."""
    rec = caller.session_of(caller_pid)
    return sessions.r9(rec.get("session_id")) if rec else None


def _with_session(meta: Any, caller_pid: Any) -> Any:
    """``meta`` with ``session`` stamped in R9 form (R14, R17)."""
    return sessions.stamp(meta, _caller_session(caller_pid))


def refuse_clashed(as_: str | None) -> None:
    """Refuse a peer's read while a tag clash stands between it and this node (R107 item 6)."""
    from awm import config
    peer = config.caller_peer(as_) if isinstance(as_, str) and as_.startswith("peer") else None
    if tagsync.blocked(peer):
        raise PermissionError(f"a tag clash stands between {peer} and this node; reads "
                              "between them wait until an agent renames the newer tag")


def peer_gate(as_: str | None) -> str | None:
    """The tag a caller's every read must carry, or None for no such limit.

    Today's rule: a foreign peer reads journal posts only. fleet.md R107 item 3
    replaces it with the trust lane's bounds, and then no tag decides read access.
    """
    return kinds.JOURNAL if channel.is_foreign(as_) else None


def _visible(as_: str | None):
    gate = peer_gate(as_)
    return lambda p: (gate is None or p.has(gate)) and not private.hides_post(as_, p)


def _handle_post(args: dict, as_: str | None = None) -> dict:
    project, scope = args.get("project"), args.get("scope")
    tag_ids = channel.post_tag_ids(args.get("kind"), args.get("tags"))
    meta, cited = sessions.fold_cites(_with_session(args.get("meta"), args.get("_caller_pid")),
                                      tag_ids)
    targets = channel.reply_targets(args.get("reply_to"))
    targets += [c for c in cited if c not in targets]
    for t in targets:
        target = channel.get_post(t)
        if target is None or not _visible(as_)(target):
            raise ValueError(f"no post {t} to reply to")
    if targets and kinds.JOURNAL not in tag_ids:
        parent = channel.get_post(targets[0])
        root = channel.get_post(parent.root) or parent
        project, scope = root.project, root.scope
    if not project or not scope:
        raise ValueError("a post needs project and scope, or a reply_to to comment on")
    if private.hides(as_, project, scope):
        raise ValueError(f"no scope {project}/{scope}")
    post = channel.post(
        project, scope,
        author=args["author"], body=args["body"],
        kind=args.get("kind"), tags=args.get("tags"),
        meta=meta,
        reply_to=targets,
        to_scope=args.get("to_scope"),
        origin=channel.edge_origin(as_),
    )
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
    refuse_clashed(as_)
    gate = peer_gate(as_)
    foreign = gate is not None
    expr = tagmod.kind_filter(args.get("kind"), args.get("tags"))
    if gate is not None:
        terms = tagmod.parse_filter(expr)
        for neg, name in terms:
            tag = tagmod.by_name(name)
            if not neg and tag is not None and tag.id in tagmod.TYPE_TAGS and tag.id != gate:
                raise PermissionError("peers may read journal posts only")
        expr = [*(("!" if neg else "") + n for neg, n in terms), tagmod.get(gate).name]
    visible = _visible(as_)
    post_id = args.get("post_id")
    if post_id:
        p = channel.get_post(post_id)
        if p is not None and not visible(p):
            p = None  # answer as for a post that does not exist, so ids cannot be probed
        if p is None:
            return {"posts": [], "total": 0}
        if not foreign:
            _note_reads(args, [p])
        thread = channel.nest(p, channel.thread_posts(p.root), visible)
        p.comments = _count(thread)
        out = {"posts": [p.to_dict()], "total": 1, "thread": thread}
        return out
    journal_first = tagmod.parse_filter(expr) == [(False, tagmod.get(kinds.JOURNAL).name)]
    common = dict(
        project=_channel_project(args.get("project"), args.get("scope")),
        scope=args.get("scope"),
        tags=expr,
        author=args.get("author"),
        limit=int(args.get("limit", 50)),
        offset=int(args.get("offset", 0)),
        before_ts=args.get("before_ts"),
        order=args.get("order") or ("desc" if journal_first and not args.get("query") else None),
        exclude=private.exclusion(as_),
    )
    degraded = semantic = None
    if args.get("query"):
        posts, degraded, semantic = channel.search(query=args["query"], **common)
        if foreign:
            for p in posts:
                p.comments = _count(channel.nest(p, channel.thread_posts(p.id), visible))
    else:
        posts = channel.fetch(**common)
    if not foreign:
        _note_reads(args, posts)
    out = {"posts": [p.to_dict() for p in posts], "total": len(posts)}
    if semantic:
        out["semantic"] = semantic
    if degraded:
        out["degraded"] = degraded
    return out


def _count(tree: list[dict]) -> int:
    return sum(1 + _count(c["replies"]) for c in tree)


def _handle_note_read(args: dict, as_: str | None = None) -> dict:
    post = channel.get_post(str(args["post_id"]))
    if post is None or not _visible(as_)(post):
        return {"ok": True, "recorded": 0}
    return {"ok": True, "recorded": sessions.record_reads(
        _caller_session(args.get("_caller_pid")), [post])}


def _note_reads(args: dict, posts: list) -> None:
    """Record the journals this fetch handed the calling session (R15). Never fails the fetch."""
    try:
        sessions.record_reads(_caller_session(args.get("_caller_pid")), posts)
    except Exception as exc:  # noqa: BLE001
        log.info("journal reads not recorded: %s", exc)


def _handle_reads(args: dict, as_: str | None = None) -> dict:
    if channel.is_foreign(as_):
        raise PermissionError("a session's reads stay on its own node")
    named = args.get("session")
    session = sessions.r9(named) if named else _caller_session(args.get("_caller_pid"))
    if session is None:
        raise ValueError(f"{named!r} is not a session id" if named else
                         "no calling session; pass session")
    rows = sessions.reads(session, int(args.get("limit", 200)))
    return {"session": session, "reads": rows, "total": len(rows), "cites": sessions.cites_of(session)}


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
    return inbox.digest(project, scope, tags=args.get("tags"), kind=args.get("kind"),
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
    "reads": _handle_reads,
    "note_read": _handle_note_read,
    "subscribe": _handle_subscribe,
    "unsubscribe": _handle_unsubscribe,
    "inbox": _handle_inbox,
    "ack": _handle_ack,
}
