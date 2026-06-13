"""Session operation definitions for the gateway manifest."""

from awm.scopes.models import SessionLogCreateRequest
from awm.scopes import sessions


SESSION_MANIFEST_FUNCTIONS = [
    {
        "name": "session_log",
        "description": "Log a session entry and record metadata in DB.",
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "summary", "type": "string", "required": True},
            {"name": "title", "type": "string", "required": False},
            {"name": "decisions", "type": "array", "required": False},
            {"name": "issues", "type": "array", "required": False},
            {"name": "next_steps", "type": "array", "required": False},
            {"name": "agent_id", "type": "string", "required": False},
            {"name": "skill_path", "type": "string", "required": False},
            {"name": "outcome", "type": "string", "required": False},
            {"name": "deviations", "type": "string", "required": False},
            {"name": "suggestions", "type": "string", "required": False},
        ],
    },
    {
        "name": "session_resolve",
        "description": "Mark a session log entry as resolved.",
        "params": [
            {"name": "session_id", "type": "integer", "required": True},
            {"name": "resolution", "type": "string", "required": True},
        ],
    },
    {
        "name": "session_search",
        "description": (
            "Search session logs (previews). Defaults to status='open'; "
            "pass status='all' for full history. Use session_get for full content."
        ),
        "params": [
            {"name": "project", "type": "string", "required": False},
            {"name": "scope", "type": "string", "required": False},
            {"name": "skill_path", "type": "string", "required": False},
            {"name": "query", "type": "string", "required": False},
            {"name": "status", "type": "string", "required": False},
            {"name": "limit", "type": "integer", "required": False},
            {"name": "offset", "type": "integer", "required": False},
        ],
    },
    {
        "name": "session_get",
        "description": "Get a session log entry by ID with full content.",
        "params": [
            {"name": "session_id", "type": "integer", "required": True},
        ],
    },
]


def _handle_session_log(args: dict) -> dict:
    req = SessionLogCreateRequest(
        project=args["project"],
        scope=args["scope"],
        summary=args["summary"],
        title=args.get("title"),
        decisions=args.get("decisions", []),
        issues=args.get("issues", []),
        next_steps=args.get("next_steps", []),
        agent_id=args.get("agent_id", "unknown"),
        skill_path=args.get("skill_path"),
        outcome=args.get("outcome"),
        deviations=args.get("deviations"),
        suggestions=args.get("suggestions"),
    )
    result = sessions.log_session(req)
    return result.model_dump()


def _handle_session_resolve(args: dict) -> dict:
    result = sessions.resolve_session(args["session_id"], args["resolution"])
    return result.model_dump()


def _handle_session_search(args: dict) -> dict:
    result = sessions.search_sessions(
        project=args.get("project"),
        scope=args.get("scope"),
        skill_path=args.get("skill_path"),
        query=args.get("query"),
        status=args.get("status", "open"),
        limit=int(args.get("limit", 50)),
        offset=int(args.get("offset", 0)),
    )
    return result.model_dump()


def _handle_session_get(args: dict) -> dict:
    result = sessions.get_session(args["session_id"])
    return result.model_dump()


SESSION_HANDLERS = {
    "session_log": _handle_session_log,
    "session_resolve": _handle_session_resolve,
    "session_search": _handle_session_search,
    "session_get": _handle_session_get,
}
