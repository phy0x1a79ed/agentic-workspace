"""Scope operation definitions for the gateway manifest."""

from awm.scopes.models import ScopeCreateRequest, ScopeUpdateRequest
from awm.scopes import scopes


# Manifest function descriptors (serializable dicts for API_MANIFEST["functions"])
SCOPE_MANIFEST_FUNCTIONS = [
    {
        "name": "scope_create",
        "tool": "scope_create",
        "description": (
            "Create a new scope (worktree + .awm/ metadata) for a project. "
            "The new branch defaults to feat/<scope>; pass branch_name to name "
            "it explicitly (e.g. a plain branch like 'release' or 'dev')."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "from_branch", "type": "string", "required": False},
            {"name": "branch_name", "type": "string", "required": False},
            {"name": "context", "type": "string", "required": False},
        ],
    },
    {
        "name": "scope_search",
        "tool": "scope_search",
        "description": (
            "Search scopes (hybrid keyword + semantic). "
            "Defaults to status='active'; pass status='all' for the full history."
        ),
        "params": [
            {"name": "query", "type": "string", "required": False},
            {"name": "status", "type": "string", "required": False},
            {"name": "project", "type": "string", "required": False},
            {"name": "limit", "type": "integer", "required": False},
            {"name": "offset", "type": "integer", "required": False},
        ],
    },
    {
        "name": "scope_complete",
        "tool": "scope_complete",
        "description": "Complete (retire) a scope. Optionally merge and clean up the worktree.",
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "merge", "type": "boolean", "required": False},
            {"name": "cleanup", "type": "boolean", "required": False},
        ],
    },
    {
        "name": "scope_delete",
        "tool": "scope_delete",
        "description": (
            "Delete a scope and clean up its worktree and branch. Refuses when "
            "the scope's .awm/data annex clone holds content that exists nowhere "
            "else; pass force=true to accept that loss."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "force", "type": "boolean", "required": False},
        ],
    },
    {
        "name": "scope_heal",
        "tool": "scope_heal",
        "description": (
            "Idempotent repair pass over active scopes: enforce tier-3 = .awm/ "
            "only (strip @.awm/context.md imports, drop untracked AGENTS.md / "
            "CLAUDE.md, recreate context.md and the per-scope opencode config) "
            "and bring .awm/data to the project's current data mode — cloning "
            "the git-annex data repo for scopes still on the legacy shared "
            "symlink. Pass dry_run=true to preview."
        ),
        "params": [
            {"name": "project", "type": "string", "required": False},
            {"name": "dry_run", "type": "boolean", "required": False},
        ],
    },
    {
        "name": "scope_repair",
        "tool": "scope_repair",
        "description": "Reconcile an on-disk worktree with a missing agents DB row.",
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
        ],
    },
    {
        "name": "scope_sync",
        "tool": "scope_sync",
        "description": "Sync a scope's feature branch with a base branch via merge or rebase.",
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "strategy", "type": "string", "required": False},
            {"name": "from_branch", "type": "string", "required": False},
        ],
    },
    {
        "name": "scope_gather",
        "tool": "scope_gather",
        "description": (
            "Fan-in: merge each peripheral scope's branch into a hub scope's "
            "branch (runs in the hub worktree, which must be clean and on the "
            "hub branch). Per-peripheral conflicts are aborted and reported; "
            "the batch continues. Local-only — no push. strategy='merge' only. "
            "data=true also fans in each peripheral's .awm/data annex branch "
            "(that leg publishes content to the canonical data repo first, so "
            "it is NOT local-only)."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "hub", "type": "string", "required": True},
            {"name": "peripherals", "type": "array", "required": True},
            {"name": "strategy", "type": "string", "required": False},
            {"name": "data", "type": "boolean", "required": False},
        ],
    },
    {
        "name": "scope_scatter",
        "tool": "scope_scatter",
        "description": (
            "Fan-out: merge a hub scope's branch into each peripheral scope's "
            "branch (each merge runs in that peripheral's worktree). A dirty or "
            "off-branch peripheral is skipped; conflicts are aborted and "
            "reported; the batch continues. Local-only — no push. "
            "strategy='merge' only. data=true also fans the hub's .awm/data "
            "annex branch out to each peripheral (that leg publishes content "
            "to the canonical data repo first, so it is NOT local-only)."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "hub", "type": "string", "required": True},
            {"name": "peripherals", "type": "array", "required": True},
            {"name": "strategy", "type": "string", "required": False},
            {"name": "data", "type": "boolean", "required": False},
        ],
    },
    {
        "name": "scope_data_status",
        "tool": "scope_data_status",
        "description": (
            "Report a scope's .awm/data view: mode (annex clone | legacy shared "
            "symlink | missing), data branch, revision, how far it has drifted "
            "from the canonical data branch, and whether it has uncommitted "
            "changes."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
        ],
    },
    {
        "name": "scope_data_snapshot",
        "tool": "scope_data_snapshot",
        "description": (
            "Commit whatever the scope has written under .awm/data. Bulk files "
            "go to the annex content store, small text to ordinary git, "
            "deletions are picked up."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "message", "type": "string", "required": False},
        ],
    },
    {
        "name": "scope_data_promote",
        "tool": "scope_data_promote",
        "description": (
            "Promote a scope's data branch into the project's canonical data "
            "branch. Snapshots, publishes content + location log, reconciles "
            "with the canonical branch, then fast-forwards it. All-or-nothing: "
            "a conflict or a concurrent promotion returns cleanly and changes "
            "nothing."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "message", "type": "string", "required": False},
        ],
    },
    {
        "name": "project_data_init",
        "tool": "project_data_init",
        "description": (
            "Convert <workspace>/data/<project> from a naked directory into a "
            "git-annex repo — the per-project opt-in for versioned, isolated, "
            "transactional data. Idempotent. Refuses while any scope of the "
            "project is active. Secrets and .env files are excluded so they are "
            "never annexed and never leave this machine."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "dry_run", "type": "boolean", "required": False},
        ],
    },
    {
        "name": "awm_refresh",
        "tool": "scope_refresh",
        "description": "Re-generate .awm/history.md and .awm/artifacts.md for a scope.",
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
        ],
    },
]


def _handle_scope_create(args: dict) -> dict:
    req = ScopeCreateRequest(
        project=args["project"],
        scope=args["scope"],
        from_branch=args.get("from_branch"),
        branch_name=args.get("branch_name"),
        context=args.get("context"),
    )
    result = scopes.create_scope(req)
    return result.model_dump()


def _handle_scope_search(args: dict) -> dict:
    result = scopes.search_scopes(
        query=args.get("query"),
        status=args.get("status", "active"),
        project=args.get("project"),
        limit=int(args.get("limit", 50)),
        offset=int(args.get("offset", 0)),
    )
    return result.model_dump()


def _handle_scope_complete(args: dict) -> dict:
    req = ScopeUpdateRequest(
        action="complete",
        merge=bool(args.get("merge", False)),
        cleanup=bool(args.get("cleanup", False)),
    )
    result = scopes.update_scope(args["project"], args["scope"], req)
    return result.model_dump()


def _handle_scope_delete(args: dict) -> dict:
    result = scopes.delete_scope(
        args["project"], args["scope"], force=bool(args.get("force", False)),
    )
    return result.model_dump()


def _handle_scope_heal(args: dict) -> dict:
    report = scopes.heal_scopes(
        project=args.get("project"),
        dry_run=bool(args.get("dry_run", False)),
    )
    return {"scopes": report, "count": len(report),
            "dry_run": bool(args.get("dry_run", False))}


def _handle_scope_data_status(args: dict) -> dict:
    return scopes.data_status(args["project"], args["scope"])


def _handle_scope_data_snapshot(args: dict) -> dict:
    return scopes.data_snapshot(args["project"], args["scope"], args.get("message"))


def _handle_scope_data_promote(args: dict) -> dict:
    return scopes.data_promote(args["project"], args["scope"], args.get("message"))


def _handle_project_data_init(args: dict) -> dict:
    return scopes.project_data_init(
        args["project"], dry_run=bool(args.get("dry_run", False)),
    )


def _handle_scope_repair(args: dict) -> dict:
    result = scopes.repair_scope(args["project"], args["scope"])
    return result.model_dump()


def _handle_scope_sync(args: dict) -> dict:
    from awm.scopes.models import ScopeSyncRequest
    req = ScopeSyncRequest(
        strategy=args.get("strategy", "merge"),
        from_branch=args.get("from_branch"),
    )
    result = scopes.sync_scope(args["project"], args["scope"], req)
    return result.model_dump()


def _handle_scope_gather(args: dict) -> dict:
    result = scopes.gather_scope(
        args["project"], args["hub"], args["peripherals"],
        strategy=args.get("strategy", "merge"),
        data=bool(args.get("data", False)),
    )
    return result.model_dump()


def _handle_scope_scatter(args: dict) -> dict:
    result = scopes.scatter_scope(
        args["project"], args["hub"], args["peripherals"],
        strategy=args.get("strategy", "merge"),
        data=bool(args.get("data", False)),
    )
    return result.model_dump()


def _handle_awm_refresh(args: dict) -> dict:
    return scopes.awm_refresh(args["project"], args["scope"])


SCOPE_HANDLERS = {
    "scope_create": _handle_scope_create,
    "scope_search": _handle_scope_search,
    "scope_complete": _handle_scope_complete,
    "scope_delete": _handle_scope_delete,
    "scope_heal": _handle_scope_heal,
    "scope_repair": _handle_scope_repair,
    "scope_data_status": _handle_scope_data_status,
    "scope_data_snapshot": _handle_scope_data_snapshot,
    "scope_data_promote": _handle_scope_data_promote,
    "project_data_init": _handle_project_data_init,
    "scope_sync": _handle_scope_sync,
    "scope_gather": _handle_scope_gather,
    "scope_scatter": _handle_scope_scatter,
    "awm_refresh": _handle_awm_refresh,
}
