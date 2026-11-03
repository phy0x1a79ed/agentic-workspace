"""History-render and index-maintenance operation definitions for the gateway manifest."""

from awm.journal import export, private, render, search_index

_CLI_HTTP = ["cli", "http"]

RENDER_MANIFEST_FUNCTIONS = [
    {
        "name": "render",
        "tool": "journal_render",
        "effect": "read",
        "description": (
            "A scope's history from the journal, as markdown: its own latest "
            "journal entries first, then its project's. The startup ritual reads it."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
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


def _handle_render(args: dict, as_: str | None = None) -> dict:
    return render.render(args["project"], args["scope"], as_=as_)


def _handle_export(args: dict, as_: str | None = None) -> dict:
    return export.export_page(args.get("cursor") or None, args.get("limit"),
                              private.exclusion(as_))


def _handle_reindex(args: dict) -> dict:
    return search_index.run_reindex(force=bool(args.get("force", False)),
                                    dry_run=bool(args.get("dry_run", False)))


RENDER_HANDLERS = {
    "render": _handle_render,
    "export": _handle_export,
    "reindex": _handle_reindex,
}
