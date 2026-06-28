"""Workspace unit lifecycle — create / retain / destroy / resolve.

A unit is the DAG execution sandbox an agents-service placement runs in. It is a
node's **one canonical filesystem home**, laid out under the top-level
``TASKS_DIR`` (``<workspace_root>/tasks/<unit_slug>/``, gitignored — the node-side
analog of ``projects/``). A unit is keyed on its ``unit_slug`` ALONE (a task has
no project), so there is no project path segment:

    CLAUDE.md               the rendered brief (auto-loaded by the claude harness)
    .awm/                   metadata dir
    inputs/<name>           read-only materialized pre-readings (set 0444)
    deliverable/<contract>/ deliverable staging (one dir per output contract)
    scratch/                free scratch space for the agent
    repos/<name> -> ../../../projects/<project>/<scope>   symlinks to scopes

The unit is **one-per-task, reused across the lifecycle**: the same slug carries
a task through PLANNING → VERIFYING_PLAN → ACTIVE, each stage spawning a fresh
agent with a different tool profile into the same directory. ``retain`` is the
between-stages / audit state (free the unit but keep its contents); ``destroy``
only happens at a terminal outcome.

This service deliberately has NO git, NO branch, NO channel, and NO cross-service
calls. A repo a node works on is an **existing scope** (owned by the ``scopes``
service); the unit only *links* to it under ``repos/<name>`` — the caller passes
``{name, project, scope}`` (the scope's OWN scopes-service coordinates) and we
build the symlink to the deterministic ``projects/<project>/<scope>`` worktree
path (a path construction, not an RPC). Pre-readings arrive already resolved
(inline ``content`` or a source ``path`` to copy); ref→content resolution is the
caller's job upstream.
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

from awm import config

from awm.workspace.dao import WorkspaceDAO, init

_dao = WorkspaceDAO()

# Subdirectories every unit gets at create time.
_DELIVERABLE = "deliverable"
_INPUTS = "inputs"
_SCRATCH = "scratch"
_REPOS = "repos"
_BRIEF = "CLAUDE.md"


def _units_root() -> Path:
    # Read TASKS_DIR off the config module at call time so a test harness that
    # monkeypatches awm.config.TASKS_DIR (or WORKSPACE_ROOT) redirects units too.
    return config.TASKS_DIR


def unit_path(unit_slug: str) -> Path:
    """Deterministic on-disk path of a unit. The DB stores this verbatim."""
    return _units_root() / unit_slug


def _read_only(path: Path) -> None:
    """Best-effort: strip write bits so a materialized input can't be edited."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass


def _link(link: Path, target: str) -> None:
    """Best-effort relative symlink: replace any prior link, never raise.

    The link may be dangling (the target scope dir is created elsewhere and may
    not exist yet) — that is fine; a symlink is just a pointer."""
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
    except OSError:
        pass
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    except OSError:
        pass


def _scaffold_awm(path: Path) -> None:
    """Create the unit's ``.awm/`` metadata dir.

    A task has no single project, so there is no shared-data dir to point at —
    no ``.awm/data`` symlink (documented choice). No skills symlink either — the
    skills service is retired; reference files are read directly from disk."""
    (path / ".awm").mkdir(parents=True, exist_ok=True)


def _link_repos(path: Path, repos: list) -> list[str]:
    """Symlink each requested repo to its existing scope worktree under ``repos/``.

    Each item is ``{"name", "project", "scope"}`` — the ``project``/``scope`` are
    the SCOPE's own scopes-service coordinates (the only legitimate ``project``
    here). The link points at the deterministic ``projects/<project>/<scope>``
    worktree (relative, so the unit tree is relocatable). Unknown shapes are
    skipped. Returns the linked names."""
    names: list[str] = []
    for item in repos or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        rproject = item.get("project")
        rscope = item.get("scope")
        if not name or not rproject or not rscope:
            continue
        # The unit dir is tasks/<slug>/repos/<name>, so ../../../ is the
        # workspace root → ../../../projects/<rproject>/<rscope>.
        _link(path / _REPOS / name, f"../../../projects/{rproject}/{rscope}")
        names.append(name)
    return names


def _materialize_prereadings(inputs_dir: Path, prereadings: list) -> list[str]:
    """Write each pre-reading under ``inputs/<name>`` (read-only). Returns names.

    Each item is ``{"name", "content"}`` (inline text) OR ``{"name", "path"}``
    (a source file/dir to copy in). Unknown shapes are skipped, not fatal —
    materialization is best-effort so one bad input never sinks the placement."""
    names: list[str] = []
    for item in prereadings or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        dest = inputs_dir / name
        try:
            if "content" in item and item["content"] is not None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(str(item["content"]), encoding="utf-8")
                _read_only(dest)
                names.append(name)
            elif item.get("path"):
                src = Path(item["path"])
                if src.is_dir():
                    shutil.copytree(src, dest, dirs_exist_ok=True)
                    names.append(name)
                elif src.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    _read_only(dest)
                    names.append(name)
        except OSError:
            continue
    return names


def create(*, unit_slug: str, context_md: str = "",
           prereadings: list | None = None, repos: list | None = None) -> dict:
    """Provision (or re-activate) a unit directory + DB row.

    Idempotent: re-creating an existing slug rewrites ``CLAUDE.md``, re-scaffolds
    the ``.awm/`` + repo symlinks, and re-layers the pre-readings without wiping
    deliverables/scratch (so a re-place after a crash keeps prior work). ``repos``
    is an optional ``[{name, project, scope}]`` list of existing scopes to link
    under ``repos/<name>``. Returns ``{unit_slug, path, state, inputs, repos}``."""
    init()
    path = unit_path(unit_slug)
    inputs_dir = path / _INPUTS
    (path / _DELIVERABLE).mkdir(parents=True, exist_ok=True)
    inputs_dir.mkdir(parents=True, exist_ok=True)
    (path / _SCRATCH).mkdir(parents=True, exist_ok=True)

    (path / _BRIEF).write_text(context_md or "", encoding="utf-8")
    _scaffold_awm(path)
    repo_names = _link_repos(path, repos or [])
    names = _materialize_prereadings(inputs_dir, prereadings or [])

    row = _dao.upsert_unit(unit_slug=unit_slug, path=str(path), state="active")
    return {
        "unit_slug": unit_slug, "path": str(path),
        "state": row["state"], "inputs": names, "repos": repo_names,
    }


def link_repos(*, unit_slug: str, repos: list | None = None) -> dict:
    """Link (or refresh) existing scopes under the unit's ``repos/`` — no brief
    rewrite. The admin ``link_repo`` primitive: an attended agent gets a newly
    linked repo in its cwd without re-provisioning the unit. Idempotent; replaces
    each named symlink. Returns the linked names."""
    init()
    path = unit_path(unit_slug)
    names = _link_repos(path, repos or []) if path.exists() else []
    return {"unit_slug": unit_slug, "repos": names, "exists": path.exists()}


def retain(*, unit_slug: str) -> dict:
    """Free a unit but KEEP its contents (mark ``idle``).

    The ``scope_complete(cleanup=False)`` analog: partial work + the deliverable
    staging survive for audit or for the next lifecycle stage (planner reuse).
    No-op-safe if the unit is unknown."""
    init()
    matched = _dao.set_state(unit_slug, "idle")
    path = unit_path(unit_slug)
    return {"unit_slug": unit_slug, "path": str(path),
            "retained": matched, "state": "idle" if matched else "unknown"}


def destroy(*, unit_slug: str) -> dict:
    """Remove a unit directory and its row (terminal cleanup). Idempotent."""
    init()
    path = unit_path(unit_slug)
    removed_dir = False
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        removed_dir = True
    removed_row = _dao.delete_unit(unit_slug)
    return {"unit_slug": unit_slug, "destroyed": removed_dir or removed_row}


def resolve(*, unit_slug: str) -> dict:
    """Return a unit's path + state (or ``state='unknown'`` if there's no row).

    Used by the agents service to recover the workdir on respawn without
    hardcoding the on-disk layout."""
    init()
    row = _dao.get_unit(unit_slug)
    path = unit_path(unit_slug)
    if row is None:
        return {"unit_slug": unit_slug, "path": str(path),
                "state": "unknown", "exists": path.exists()}
    return {"unit_slug": unit_slug, "path": row["path"],
            "state": row["state"], "exists": Path(row["path"]).exists()}
