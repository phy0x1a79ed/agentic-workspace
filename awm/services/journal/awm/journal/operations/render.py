"""History-render and index-maintenance operation definitions for the gateway manifest."""

from awm.journal import export, owners, render, search_index

_CLI_HTTP = ["cli", "http"]

RENDER_MANIFEST_FUNCTIONS = [
    {
        "name": "render",
        "tool": "journal_render",
        "effect": "write",
        "description": (
            "Render a scope's .awm/history.md from the journal: its own latest "
            "entries first, then its project's. Returns the text and the path "
            "written. Pass siblings (the project's other live scopes) to list "
            "them; omit it and the scopes service is asked. write=false returns "
            "the text without storing it."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "siblings", "type": "array", "required": False,
             "description": "Names of the project's other live scopes, in listing order."},
            {"name": "write", "type": "boolean", "required": False,
             "description": "Store the render at the scope's .awm/history.md. Default true."},
        ],
    },
    {
        "name": "export",
        "tool": "journal_export",
        "effect": "read",
        "surfaces": _CLI_HTTP,
        "description": (
            "Page the indexed posts as search documents. Pass the previous page's "
            "next_cursor as cursor. The final page (done=true) also lists every id."
        ),
        "params": [
            {"name": "cursor", "type": "string", "required": False},
            {"name": "limit", "type": "integer", "required": False,
             "description": "Documents per page. Default 200, at most 1000."},
        ],
    },
    {
        "name": "reindex",
        "tool": "journal_reindex",
        "effect": "write",
        "surfaces": _CLI_HTTP,
        "description": (
            "Bring the post search index in line with the post table: embeds what "
            "is missing, changed or made by another model, and drops what no longer "
            "exists. dry_run reports without writing; force re-embeds everything."
        ),
        "params": [
            {"name": "dry_run", "type": "boolean", "required": False},
            {"name": "force", "type": "boolean", "required": False},
        ],
        "timeout": 3600.0,
    },
]


def _handle_render(args: dict) -> dict:
    project, scope = args["project"], args["scope"]
    siblings = args.get("siblings")
    if siblings is None:
        try:
            siblings = owners.scope_siblings(project, scope)
        except Exception:  # noqa: BLE001 — a render without the siblings section beats none
            siblings = []
    elif isinstance(siblings, str):
        siblings = [x.strip() for x in siblings.split(",") if x.strip()]
    write = args.get("write")
    return render.render(project, scope, list(siblings),
                         write=True if write is None else write in (True, "true", "True", "1", 1))


def _handle_export(args: dict) -> dict:
    return export.export_page(args.get("cursor") or None, args.get("limit"))


def _handle_reindex(args: dict) -> dict:
    return search_index.run_reindex(force=bool(args.get("force", False)),
                                    dry_run=bool(args.get("dry_run", False)))


RENDER_HANDLERS = {
    "render": _handle_render,
    "export": _handle_export,
    "reindex": _handle_reindex,
}
