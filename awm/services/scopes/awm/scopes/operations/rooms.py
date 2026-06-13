"""Room operation definitions for the gateway manifest."""

from awm.scopes import rooms as rooms_svc


ROOM_MANIFEST_FUNCTIONS = [
    {
        "name": "room_create",
        "description": "Create a new room with an owning scope and optional guest scopes.",
        "params": [
            {"name": "topic", "type": "string", "required": False},
            {"name": "scopes", "type": "array", "required": False},
            {"name": "opener", "type": "string", "required": False},
            {"name": "close_on_exit", "type": "boolean", "required": False},
        ],
    },
    {
        "name": "room_get",
        "description": "Get room details including participants and recent transcript.",
        "params": [
            {"name": "room_id", "type": "string", "required": True},
        ],
    },
    {
        "name": "room_search",
        "description": "Search rooms (hybrid keyword + semantic). Defaults to status='active'.",
        "params": [
            {"name": "query", "type": "string", "required": False},
            {"name": "status", "type": "string", "required": False},
            {"name": "participating_scope", "type": "string", "required": False},
            {"name": "limit", "type": "integer", "required": False},
            {"name": "offset", "type": "integer", "required": False},
        ],
    },
    {
        "name": "room_post",
        "description": "Post a message to a room transcript.",
        "params": [
            {"name": "room_id", "type": "string", "required": True},
            {"name": "author", "type": "string", "required": True},
            {"name": "body", "type": "string", "required": True},
            {"name": "kind", "type": "string", "required": False},
            {"name": "to_scope", "type": "string", "required": False},
        ],
    },
    {
        "name": "room_invite",
        "description": "Invite a scope to join a room as a guest.",
        "params": [
            {"name": "room_id", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
        ],
    },
    {
        "name": "room_remove",
        "description": "Remove a scope participant from a room.",
        "params": [
            {"name": "room_id", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
        ],
    },
    {
        "name": "room_close",
        "description": "Close a room.",
        "params": [
            {"name": "room_id", "type": "string", "required": True},
            {"name": "kill_agents", "type": "boolean", "required": False},
        ],
    },
    {
        "name": "room_archive",
        "description": "Soft-archive a room. Refused if owner agent is still active.",
        "params": [
            {"name": "room_id", "type": "string", "required": True},
        ],
    },
    {
        "name": "room_history",
        "description": "Get a room's transcript history.",
        "params": [
            {"name": "room_id", "type": "string", "required": True},
            {"name": "limit_chars", "type": "integer", "required": False},
            {"name": "before_ts", "type": "string", "required": False},
        ],
    },
    {
        "name": "room_agents",
        "description": "List agent participants of a room.",
        "params": [
            {"name": "room_id", "type": "string", "required": True},
        ],
    },
    # ---------------------------------------------------------------------------
    # 4 cross-service RPCs for the agents service
    # ---------------------------------------------------------------------------
    {
        "name": "ensureAgentRoom",
        "description": (
            "Get-or-create the owned room for a scope. "
            "Returns {room_id}."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
        ],
    },
    {
        "name": "roomsForScope",
        "description": (
            "Return IDs of open rooms where the given scope is owner or guest. "
            "Returns {rooms: [room_id, ...]}."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
        ],
    },
    {
        "name": "autoCloseForScope",
        "description": (
            "Close open rooms owned by the given scope (called when agent session ends). "
            "Returns {closed: [room_id, ...]}."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
        ],
    },
    {
        "name": "roomAgentsKillOnClose",
        "description": (
            "Return the agent scopes that should be SIGTERM'd when a room closes. "
            "Returns {scopes: [[project, scope], ...]}."
        ),
        "params": [
            {"name": "room_id", "type": "string", "required": True},
        ],
    },
]


def _handle_room_create(args: dict) -> dict:
    room = rooms_svc.create_room(
        topic=args.get("topic"),
        scopes=args.get("scopes", []),
        opener=args.get("opener", "user:operator"),
        close_on_exit=bool(args.get("close_on_exit", False)),
    )
    return room.to_dict()


def _handle_room_get(args: dict) -> dict:
    room = rooms_svc.get_room(args["room_id"])
    if room is None:
        return {"error": f"no such room: {args['room_id']}"}
    participants = rooms_svc.list_participants(args["room_id"], active_only=False)
    recent = rooms_svc.history(args["room_id"], limit_chars=1024)
    return {
        "room": room.to_dict(),
        "participants": [p.to_dict() for p in participants],
        "recent": [p.to_dict() for p in recent],
    }


def _handle_room_search(args: dict) -> dict:
    results = rooms_svc.search_rooms(
        args.get("query"),
        status=args.get("status", "active"),
        participating_scope=args.get("participating_scope"),
        limit=int(args.get("limit", 50)),
        offset=int(args.get("offset", 0)),
    )
    return {"rooms": [r.to_dict() for r in results], "total": len(results)}


def _handle_room_post(args: dict) -> dict:
    post = rooms_svc.post(
        args["room_id"],
        author=args["author"],
        body=args["body"],
        kind=args.get("kind", "message"),
        to_scope=args.get("to_scope"),
    )
    return {"post": post.to_dict()}


def _handle_room_invite(args: dict) -> dict:
    participant = rooms_svc.add_participant(args["room_id"], "scope", args["scope"])
    return {"participant": participant.to_dict()}


def _handle_room_remove(args: dict) -> dict:
    ok = rooms_svc.remove_participant(args["room_id"], "scope", args["scope"])
    return {"removed": ok}


def _handle_room_close(args: dict) -> dict:
    room = rooms_svc.close_room(
        args["room_id"], kill_agents=bool(args.get("kill_agents", False))
    )
    return {"room": room.to_dict()}


def _handle_room_archive(args: dict) -> dict:
    room = rooms_svc.archive_room(args["room_id"])
    return {"room": room.to_dict()}


def _handle_room_history(args: dict) -> dict:
    posts = rooms_svc.history(
        args["room_id"],
        limit_chars=int(args.get("limit_chars", 4096)),
        before_ts=args.get("before_ts"),
    )
    return {"posts": [p.to_dict() for p in posts], "total": len(posts)}


def _handle_room_agents(args: dict) -> dict:
    participants = rooms_svc.list_participants(args["room_id"], active_only=False)
    return {
        "agents": [
            p.to_dict() for p in participants
            if p.kind in ("scope", "shadow_peer")
        ]
    }


# ---------------------------------------------------------------------------
# 4 cross-service RPC handlers (used by agents service)
# ---------------------------------------------------------------------------

def _handle_ensure_agent_room(args: dict) -> dict:
    """ensureAgentRoom(project, scope) → {room_id}"""
    room_id = rooms_svc.ensure_agent_room(args["project"], args["scope"])
    return {"room_id": room_id}


def _handle_rooms_for_scope(args: dict) -> dict:
    """roomsForScope(project, scope) → {rooms: [room_id, ...]}"""
    scope_key = f"{args['project']}/{args['scope']}"
    room_ids = rooms_svc.rooms_for_scope(scope_key)
    return {"rooms": room_ids}


def _handle_auto_close_for_scope(args: dict) -> dict:
    """autoCloseForScope(project, scope) → {closed: [room_id, ...]}"""
    scope_key = f"{args['project']}/{args['scope']}"
    closed = rooms_svc.auto_close_for_scope(scope_key)
    return {"closed": closed}


def _handle_room_agents_kill_on_close(args: dict) -> dict:
    """roomAgentsKillOnClose(room_id) → {scopes: [[project, scope], ...]}"""
    pairs = rooms_svc.room_agents_kill_on_close(args["room_id"])
    return {"scopes": list(pairs)}


ROOM_HANDLERS = {
    "room_create": _handle_room_create,
    "room_get": _handle_room_get,
    "room_search": _handle_room_search,
    "room_post": _handle_room_post,
    "room_invite": _handle_room_invite,
    "room_remove": _handle_room_remove,
    "room_close": _handle_room_close,
    "room_archive": _handle_room_archive,
    "room_history": _handle_room_history,
    "room_agents": _handle_room_agents,
    # Cross-service RPCs
    "ensureAgentRoom": _handle_ensure_agent_room,
    "roomsForScope": _handle_rooms_for_scope,
    "autoCloseForScope": _handle_auto_close_for_scope,
    "roomAgentsKillOnClose": _handle_room_agents_kill_on_close,
}
