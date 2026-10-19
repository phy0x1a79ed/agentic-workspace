"""Scope operation definitions for the gateway manifest."""

from awm.config import privacy
from awm.scopes.models import ScopeCreateRequest, ScopeUpdateRequest
from awm.scopes import private, scopes, search_index


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
        "effect": "write",
        "description": (
            "Create a new scope (worktree + .awm/ metadata) for a project. "
            "The new branch defaults to feat/<scope>; pass branch_name to name "
            "it explicitly (e.g. a plain branch like 'release' or 'dev'). "
            "A branch that already exists is checked out, not recreated. A branch "
            "that only a remote carries becomes a local branch tracking that remote. "
            "When several remotes carry it, create refuses and names each one. "
            "Pass from_branch='<remote>/<branch>' to pick one. "
            "An unknown project is refused unless new_project=true, which first "
            "creates the project (bare repo, main checkout, data dirs; clone_url "
            "or fork_url to start from a remote); omit scope then to create the "
            "project alone. "
            "A '/' in scope groups it: the worktree lands in that group's "
            "directory and the branch under feat/<group>/. Projects grouped by "
            "component (check scope search) expect a grouped name. "
            "Examples: project='awm', scope='svc-diplomacy' creates "
            "projects/awm/svc-diplomacy on branch feat/svc-diplomacy; "
            "project='metasmith', scope='engine/presentation' creates "
            "projects/metasmith/engine/presentation on branch "
            "feat/engine/presentation."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True,
             "description": "Name of the project, the directory under projects/ "
                            "(e.g. 'awm'); it must exist unless new_project is true. "
                            "Not the workspace or the MCP namespace."},
            {"name": "scope", "type": "string", "required": False,
             "description": "Name for the new scope, unique within the project "
                            "(e.g. 'svc-diplomacy', or 'engine/presentation' to place it in "
                            "the engine group). Becomes the worktree path under the project. "
                            "Required, except with new_project to create the project alone."},
            {"name": "from_branch", "type": "string", "required": False,
             "description": "Start point of a new branch. Default: the project's "
                            "default branch. For a branch that already exists, only "
                            "'<remote>/<branch>' is accepted, and it sets the upstream."},
            {"name": "branch_name", "type": "string", "required": False,
             "description": "Branch to check out. Default feat/<scope>. It may "
                            "already exist locally or on a remote."},
            {"name": "context", "type": "string", "required": False},
            {"name": "new_project", "type": "boolean", "required": False,
             "description": "Create the project first when it does not exist. "
                            "Default false: an unknown project is an error."},
            {"name": "clone_url", "type": "string", "required": False,
             "description": "With new_project: clone this remote as the project's bare repo."},
            {"name": "fork_url", "type": "string", "required": False,
             "description": "With new_project: fork this GitHub repo and clone the fork."},
        ],
        # a cold `dvc checkout` of everything the base branch pins.
        "timeout": 1800.0,
    },
    {
        "name": "scope_search",
        "tool": "scope_search",
        "effect": "read",
        "description": (
            "Search scopes: name matches first, then scopes ranked by the "
            "meaning of their goals and context. Each project's main checkout "
            "is a scope named for its default branch. "
            "Defaults to status='active'; pass status='all' for the full history. "
            "With no project, the reply also lists every project, matched and "
            "ranked by the same query, with its scope counts per status."
        ),
        "params": [
            {"name": "query", "type": "string", "required": False},
            {"name": "status", "type": "string", "required": False},
            {"name": "project", "type": "string", "required": False,
             "description": "Only this project's scopes. Omit to search every project "
                            "and list the projects."},
            {"name": "limit", "type": "integer", "required": False},
            {"name": "offset", "type": "integer", "required": False,
             "description": "Skips scopes only; the project list is never paged."},
        ],
    },
    {
        "name": "scope_reindex",
        "tool": "scope_reindex",
        "effect": "write",
        "description": (
            "Bring the scopes service's search index of scopes and projects in line "
            "with the tables: embeds what is missing, changed or made by another "
            "model, and drops what no longer exists. Returns counts per type. "
            "Posts are indexed by journal_reindex. "
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
        "effect": "write",
        "description": (
            "Complete (retire) a scope. Optionally merge, and remove the worktree "
            "with cleanup=true. The branch stays unless delete_branch=true, which "
            "needs cleanup=true. The reply then names the branch and its last SHA."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "merge", "type": "boolean", "required": False},
            {"name": "cleanup", "type": "boolean", "required": False},
            {"name": "delete_branch", "type": "boolean", "required": False,
             "description": "Also delete the scope's git branch. Default false: "
                            "the branch is kept."},
        ],
    },
    {
        "name": "scope_delete",
        "tool": "scope_delete",
        "effect": "write",
        "description": (
            "Delete a scope and remove its worktree. The branch stays unless "
            "delete_branch=true. The reply then names the branch and its last "
            "SHA. Refuses when the worktree has uncommitted changes -- which "
            "covers data as well as code, since a data chunk is committed via "
            "its .dvc pin; pass force=true to discard them."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
            {"name": "force", "type": "boolean", "required": False},
            {"name": "delete_branch", "type": "boolean", "required": False,
             "description": "Also delete the scope's git branch. Default false: "
                            "the branch is kept."},
        ],
        # rmtree over a fully materialised data tree.
        "timeout": 600.0,
    },
    {
        "name": "scope_heal",
        "tool": "scope_heal",
        "effect": "write",
        "description": (
            "Idempotent repair pass over active scopes: enforce tier-3 = .awm/ "
            "only (strip @.awm/context.md imports, drop untracked AGENTS.md / "
            "CLAUDE.md, recreate context.md and the per-scope opencode config) "
            "and bring the scope's data view to whatever its checkout implies "
            "— installing the DVC merge driver + hooks and materialising the "
            "pinned chunks, or leaving an unconverted project on the legacy "
            "shared symlink. Pass dry_run=true to preview. "
            "The reply also lists worktrees under projects/<project>/ that no scope "
            "row names (unregistered_worktrees) and active or allocated rows whose "
            "worktree is gone (orphaned_rows). Heal never registers or retires a "
            "row for either list."
        ),
        "params": [
            {"name": "project", "type": "string", "required": False},
            {"name": "dry_run", "type": "boolean", "required": False},
        ],
        # may provision every scope in a project.
        "timeout": 1800.0,
    },
    {
        "name": "scope_private",
        "tool": "scope_private",
        "effect": "write",
        "description": (
            "Mark a project, or one scope of it, private on this node, or clear "
            "the mark. A private project or scope, with every scope nested under "
            "it and every path under its directory, is invisible to calls from "
            "other nodes; callers on this node see it as before. Refuses a mark "
            "whose path would also hide another project's or scope's transcripts "
            "(projects 'awm' and 'awm-dev', for example). Returns the project's "
            "marks."
        ),
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": False,
             "description": "Mark only this scope. Omit to mark the whole project."},
            {"name": "private", "type": "boolean", "required": True,
             "description": "true sets the mark, false clears it."},
        ],
    },
    {
        "name": "scope_repair",
        "tool": "scope_repair",
        "effect": "write",
        "description": "Reconcile an on-disk worktree with a missing agents DB row.",
        "params": [
            {"name": "project", "type": "string", "required": True},
            {"name": "scope", "type": "string", "required": True},
        ],
    },
    {
        "name": "scope_sync",
        "tool": "scope_sync",
        "effect": "write",
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
        "tool": "scope_git_gather",
        "effect": "write",
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
        "tool": "scope_git_scatter",
        "effect": "write",
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
]


def _handle_scope_create(args: dict) -> dict:
    req = ScopeCreateRequest(
        project=args["project"],
        scope=args.get("scope"),
        from_branch=args.get("from_branch"),
        branch_name=args.get("branch_name"),
        context=args.get("context"),
        new_project=bool(args.get("new_project", False)),
        clone_url=args.get("clone_url"),
        fork_url=args.get("fork_url"),
    )
    result = scopes.create_scope(req)
    return result.model_dump()


def _handle_scope_search(args: dict, as_: str | None = None) -> dict:
    result = scopes.search_scopes(
        query=args.get("query"),
        status=args.get("status", "active"),
        project=args.get("project"),
        limit=int(args.get("limit", 50)),
        offset=int(args.get("offset", 0)),
        as_=as_,
    )
    unset = {k for k in ("degraded", "projects") if getattr(result, k) is None}
    return result.model_dump(exclude=unset or None)


def _handle_scope_complete(args: dict) -> dict:
    req = ScopeUpdateRequest(
        action="complete",
        merge=bool(args.get("merge", False)),
        cleanup=bool(args.get("cleanup", False)),
        delete_branch=bool(args.get("delete_branch", False)),
    )
    result = scopes.update_scope(args["project"], args["scope"], req)
    return result.model_dump()


def _handle_scope_delete(args: dict) -> dict:
    result = scopes.delete_scope(
        args["project"], args["scope"], force=bool(args.get("force", False)),
        delete_branch=bool(args.get("delete_branch", False)),
    )
    return result.model_dump()


def _handle_scope_heal(args: dict, as_: str | None = None) -> dict:
    dry_run = bool(args.get("dry_run", False))
    if args.get("project") and privacy.from_peer(as_) and privacy.hidden(args["project"]):
        return {"scopes": [], "count": 0, "dry_run": dry_run, "unregistered_worktrees": [],
                "orphaned_rows": [], "audit_errors": []}
    report = privacy.visible_rows(
        scopes.heal_scopes(project=args.get("project"), dry_run=dry_run), as_)
    audit = scopes.audit_worktrees(project=args.get("project"))
    return {"scopes": report, "count": len(report),
            "dry_run": dry_run,
            "unregistered_worktrees": privacy.visible_rows(audit["unregistered"], as_),
            "orphaned_rows": privacy.visible_rows(audit["orphaned_rows"], as_),
            "audit_errors": privacy.visible_rows(audit["errors"], as_)}


def _handle_scope_private(args: dict, as_: str | None = None) -> dict:
    if privacy.from_peer(as_):
        raise RuntimeError("unknown function 'scope_private'")
    flag = args["private"]
    if isinstance(flag, str):
        flag = {"true": True, "false": False}.get(flag.strip().lower(), flag)
    if not isinstance(flag, bool):
        raise ValueError(f"private must be true or false, not {args['private']!r}")
    return private.set_private(args["project"], args.get("scope"), flag)


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


SCOPE_HANDLERS = {
    "scope_create": _handle_scope_create,
    "scope_search": _handle_scope_search,
    "scope_reindex": _handle_scope_reindex,
    "scope_complete": _handle_scope_complete,
    "scope_delete": _handle_scope_delete,
    "scope_heal": _handle_scope_heal,
    "scope_private": _handle_scope_private,
    "scope_repair": _handle_scope_repair,
    "scope_sync": _handle_scope_sync,
    "scope_gather": _handle_scope_gather,
    "scope_scatter": _handle_scope_scatter,
}
