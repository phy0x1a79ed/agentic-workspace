"""Project operation definitions for the gateway manifest."""

from awm.scopes.models import ProjectCreateRequest
from awm.scopes import projects


PROJECT_MANIFEST_FUNCTIONS = [
    {
        "name": "project_create",
        "tool": "project_create",
        "description": "Create a new project with bare repository, worktree, and data dirs.",
        "params": [
            {"name": "name", "type": "string", "required": True},
            {"name": "clone_url", "type": "string", "required": False},
            {"name": "fork_url", "type": "string", "required": False},
        ],
    },
    {
        "name": "project_search",
        "tool": "project_search",
        "description": "List/search projects with per-status scope counts.",
        "params": [
            {"name": "query", "type": "string", "required": False},
            {"name": "active_only", "type": "boolean", "required": False},
            {"name": "limit", "type": "integer", "required": False},
            {"name": "offset", "type": "integer", "required": False},
        ],
    },
]


def _handle_project_create(args: dict) -> dict:
    req = ProjectCreateRequest(
        name=args["name"],
        clone_url=args.get("clone_url"),
        fork_url=args.get("fork_url"),
    )
    result = projects.create_project(req)
    return result.model_dump()


def _handle_project_search(args: dict) -> dict:
    result = projects.search_projects(
        query=args.get("query"),
        active_only=bool(args.get("active_only", False)),
        limit=int(args.get("limit", 50)),
        offset=int(args.get("offset", 0)),
    )
    return result.model_dump()


PROJECT_HANDLERS = {
    "project_create": _handle_project_create,
    "project_search": _handle_project_search,
}
