"""Scope operation definitions for the gateway manifest."""

from awm.scopes.models import ScopeCreateRequest, ScopeUpdateRequest
from awm.scopes import scopes, search_index


# Manifest function descriptors (serializable dicts for API_MANIFEST["functions"])
#
# Several verbs carry an explicit `timeout`. The hub's 30 s default was set when
# a scope's data was a symlink; provisioning now runs a real `dvc checkout`, so
# the verbs that touch data need budgets measured in minutes. The work completes
# either way -- it runs in the service, not the RPC -- but a timed-out call
# reports failure for something that succeeded, which is worse than slow.
SCOPE_MANIFEST_FUNCTIONS = [
    {
        "name": "scope_create",
        "tool": "scope_create",
        "description": (
            "Create a new scope (worktree + .awm/ metadata) for a project. "
            "The new branch defaults to feat/<scope>; pass branch_name to name "
            "it explicitly (e.g. a plain branch like 'release' or 'dev'). "
            "Example: project='awm', scope='svc-diplomacy' creates "
            "projects/awm/svc-diplomacy on branch feat/svc-diplomacy."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True,
             "description": "Name of an existing project, the directory under "
                            "projects/ (e.g. 'awm'). Not the workspace or the MCP namespace."},
            {"name": "scope", "type": "string", "required": True,
             "description": "Name for the new scope, unique within the project "
                            "(e.g. 'svc-diplomacy'). Becomes the worktree directory name."},
            {"name": "from_branch", "type": "string", "required": False},
            {"name": "branch_name", "type": "string", "required": False},
            {"name": "context", "type": "string", "required": False},
        ],
        # a cold `dvc checkout` of everything the base branch pins.
        "timeout": 1800.0,
    },
    {
        "name": "scope_search",
        "tool": "scope_search",
        "description": (
            "Search scopes: name matches first, then scopes ranked by the "
            "meaning of their goals and context. "
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
        "name": "scope_reindex",
        "tool": "scope_reindex",
        "description": (
            "Bring the search index of posts, scopes and projects in line with "
            "the tables: embeds what is missing, changed or made by another "
            "model, and drops what no longer exists. Returns counts per type. "
            "dry_run reports without writing; force re-embeds everything."
        ),
        "params": [
            {"name": "dry_run", "type": "boolean", "required": False},
            {"name": "force", "type": "boolean", "required": False},
        ],
        "surfaces": ["cli", "http"],
        "timeout": 3600.0,
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
            "the worktree has uncommitted changes -- which now covers data as "
            "well as code, since a data chunk is committed via its .dvc pin; "
            "pass force=true to discard them."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "force", "type": "boolean", "required": False},
        ],
        # rmtree over a fully materialised data tree.
        "timeout": 600.0,
    },
    {
        "name": "scope_heal",
        "tool": "scope_heal",
        "description": (
            "Idempotent repair pass over active scopes: enforce tier-3 = .awm/ "
            "only (strip @.awm/context.md imports, drop untracked AGENTS.md / "
            "CLAUDE.md, recreate context.md and the per-scope opencode config) "
            "and bring the scope's data view to whatever its checkout implies "
            "— installing the DVC merge driver + hooks and materialising the "
            "pinned chunks, or leaving an unconverted project on the legacy "
            "shared symlink. Pass dry_run=true to preview."
        ),
        "params": [
            {"name": "project", "type": "string", "required": False},
            {"name": "dry_run", "type": "boolean", "required": False},
        ],
        # may provision every scope in a project.
        "timeout": 1800.0,
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
            "A peripheral's data pins ride its code branch, so this merges "
            "data too -- there is no separate data leg any more."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "hub", "type": "string", "required": True},
            {"name": "peripherals", "type": "array", "required": True},
            {"name": "strategy", "type": "string", "required": False},
        ],
        # each merge fires a post-merge `dvc checkout`.
        "timeout": 900.0,
    },
    {
        "name": "scope_scatter",
        "tool": "scope_scatter",
        "description": (
            "Fan-out: merge a hub scope's branch into each peripheral scope's "
            "branch (each merge runs in that peripheral's worktree). A dirty or "
            "off-branch peripheral is skipped; conflicts are aborted and "
            "reported; the batch continues. Local-only — no push. "
            "strategy='merge' only. The hub's data pins ride its branch, so "
            "this fans out data too -- there is no separate data leg any more."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "hub", "type": "string", "required": True},
            {"name": "peripherals", "type": "array", "required": True},
            {"name": "strategy", "type": "string", "required": False},
        ],
        # each merge fires a post-merge `dvc checkout`.
        "timeout": 900.0,
    },
    {
        "name": "scope_data_status",
        "tool": "scope_data_status",
        "description": (
            "Report a scope's data view: mode (dvc | legacy shared symlink | "
            "missing), the commit that pins the data, which chunks it pins, "
            "which of those are materialised on disk, and whether the workspace "
            "still matches the pins. There is no separate data branch to drift "
            "— the code revision IS the data revision."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
        ],
        # `dvc status` walks every pin.
        "timeout": 300.0,
    },
    {
        "name": "scope_data_mount",
        "tool": "scope_data_mount",
        "description": (
            "Choose which data chunks this scope materialises on disk, e.g. "
            "['data/pipeline/model']. Every chunk the branch pins stays pinned, "
            "hashed and backed up regardless — this only decides what costs "
            "inodes and checkout time here, so a scope can pin a 30 GB archive "
            "it never reads. Pass no chunks to materialise everything. "
            "To COMMIT data, use ordinary git: `dvc add <path>` then commit the "
            "generated .dvc pin alongside your code."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "chunks", "type": "array", "required": False},
        ],
        # changing the mount list re-checks-out.
        "timeout": 900.0,
    },
    {
        "name": "scope_data_gc",
        "tool": "scope_data_gc",
        "description": (
            "Reclaim shared-cache space by deleting objects no listed project "
            "references. DRY RUN BY DEFAULT — pass dry_run=false to delete. "
            "The cache is shared workspace-wide: name every DVC-wired project "
            "either in projects (its data survives) or in exclude (its data is "
            "dropped on purpose), or the call is refused. Keeps every output "
            "pinned by any commit, staged index or worktree file, plus any "
            "object touched within the grace window. Refuses while a pinned "
            ".dir manifest is missing from the cache, unless its id is in "
            "accept_missing. Takes no repo locks; only one gc runs at a time."
        ),
        "params": [
            {"name": "projects", "type": "array", "required": True},
            {"name": "exclude", "type": "array", "required": False},
            {"name": "accept_missing", "type": "array", "required": False},
            {"name": "dry_run", "type": "boolean", "required": False},
            {"name": "keep", "type": "string", "required": False},
        ],
        "timeout": 600.0,
    },
    {
        "name": "awm_refresh",
        "tool": "scope_refresh",
        "description": "Re-generate .awm/history.md for a scope.",
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
    return result.model_dump(exclude={"degraded"} if result.degraded is None else None)


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


def _handle_data_gc(args: dict) -> dict:
    return scopes.data_gc(
        args["projects"],
        dry_run=bool(args.get("dry_run", True)),
        keep=args.get("keep", "all-commits"),
        exclude=args.get("exclude"),
        accept_missing=args.get("accept_missing"),
    )


def _handle_scope_data_mount(args: dict) -> dict:
    return scopes.data_mount(args["project"], args["scope"], args.get("chunks"))


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
    )
    return result.model_dump()


def _handle_scope_scatter(args: dict) -> dict:
    result = scopes.scatter_scope(
        args["project"], args["hub"], args["peripherals"],
        strategy=args.get("strategy", "merge"),
    )
    return result.model_dump()


def _handle_scope_reindex(args: dict) -> dict:
    return search_index.run_reindex(force=bool(args.get("force", False)),
                                    dry_run=bool(args.get("dry_run", False)))


def _handle_awm_refresh(args: dict) -> dict:
    return scopes.awm_refresh(args["project"], args["scope"])


SCOPE_HANDLERS = {
    "scope_create": _handle_scope_create,
    "scope_search": _handle_scope_search,
    "scope_reindex": _handle_scope_reindex,
    "scope_complete": _handle_scope_complete,
    "scope_delete": _handle_scope_delete,
    "scope_heal": _handle_scope_heal,
    "scope_repair": _handle_scope_repair,
    "scope_data_status": _handle_scope_data_status,
    "scope_data_mount": _handle_scope_data_mount,
    "scope_data_gc": _handle_data_gc,
    "scope_sync": _handle_scope_sync,
    "scope_gather": _handle_scope_gather,
    "scope_scatter": _handle_scope_scatter,
    "awm_refresh": _handle_awm_refresh,
}
