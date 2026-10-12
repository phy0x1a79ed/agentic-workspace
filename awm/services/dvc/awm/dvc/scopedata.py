"""Per-scope DVC wiring verbs — data view, chunk mounts and shared-cache gc.

These resolve a scope from ``(project, scope)`` through the workspace layout
and git, never the scopes database, so the dvc service answers without the
scopes service. The engine itself lives in :mod:`awm.dvc.data_dvc`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from awm import config as _config
from awm.dvc import data_dvc


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _check_name(name: str, kind: str, *, nested: bool = False) -> str:
    """Refuse a name that could escape ``projects/`` once joined onto it."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"{kind} must be a non-empty string")
    segments = name.split("/") if nested else [name]
    for seg in segments:
        if not seg or seg.startswith(".") or "/" in seg or "\\" in seg or "\x00" in seg:
            raise ValueError(f"{kind} is not a safe path segment (got {name!r})")
    return name


def _projects_dir() -> Path:
    return _config.PROJECTS_DIR


def project_worktrees(project: str) -> list[Path]:
    """Every worktree of a project's bare repo, at whatever depth.

    Enumerated from git, not by globbing: a nested scope such as ``fabfos/dev``
    sits two levels down, and a worktree missing from this list is a worktree
    whose pinned objects fall out of gc's keep-set. Raises rather than
    degrading to a short list, for that same reason.
    """
    bare = _projects_dir() / project / ".bare"
    if not bare.exists():
        raise FileNotFoundError(f"Project '{project}' has no bare repo at {bare}")
    r = _git("-C", str(bare), "worktree", "list", "--porcelain")
    if r.returncode != 0:
        raise RuntimeError(
            f"Could not enumerate worktrees of '{project}': {(r.stderr or '').strip()}"
        )
    out: list[Path] = []
    path: Path | None = None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line.strip() == "bare":
            path = None
        elif not line.strip() and path is not None:
            out.append(path)
            path = None
    if path is not None:
        out.append(path)
    return out


def _scope_worktree(project: str, scope: str) -> Path:
    return _projects_dir() / project / scope


def _dvc_wired_projects() -> set[str]:
    """Every project whose objects may live in the shared cache.

    Wired means the common git config carries awm's merge driver, or any
    worktree tracks a ``.dvc/config``. A project whose worktrees cannot be
    enumerated counts as wired: the caller then has to name it either way.
    """
    wired: set[str] = set()
    for bare in sorted(_projects_dir().glob("*/.bare")):
        project = bare.parent.name
        r = _git("-C", str(bare), "config", "--get", "merge.dvc.driver")
        if r.returncode == 0 and r.stdout.strip():
            wired.add(project)
            continue
        try:
            worktrees = project_worktrees(project)
        except (FileNotFoundError, RuntimeError):
            wired.add(project)
            continue
        if any(data_dvc.is_dvc_repo(wt) for wt in worktrees):
            wired.add(project)
    return wired


def scope_status(project: str, scope: str) -> dict:
    """Report a scope's data view: mode, the commit that pins it, and drift."""
    _check_name(project, "project name")
    _check_name(scope, "scope name", nested=True)
    return data_dvc.data_status(project, scope, _scope_worktree(project, scope))


def mount(project: str, scope: str, chunks: list[str] | None = None) -> dict:
    """Choose which chunks this scope materialises on disk; no chunks means all.

    Mounting is deliberately not a property of the commit. Every chunk the
    branch pins stays pinned, hashed and backed up regardless of this setting.
    """
    _check_name(project, "project name")
    _check_name(scope, "scope name", nested=True)
    worktree = _scope_worktree(project, scope)
    awm_dir = worktree / ".awm"
    if chunks:
        data_dvc.write_mounts(awm_dir, chunks)
    else:
        (awm_dir / data_dvc.MOUNTS_FILE).unlink(missing_ok=True)
    report = data_dvc.provision_scope_data(project, scope, awm_dir)
    return {"project": project, "scope": scope, **report}


def gc(projects: list[str], dry_run: bool = True,
       keep: str = "all-commits", exclude: list[str] | None = None,
       accept_missing: list[str] | None = None) -> dict:
    """Reclaim cache objects no listed project references. Dry by default.

    Every DVC-wired project must appear in ``projects`` (its data survives) or
    in ``exclude`` (its data is dropped on purpose). See
    ``data_dvc.collect_garbage``.
    """
    listed: list[data_dvc.GcProject] = []
    for proj in projects:
        _check_name(proj, "project name")
        listed.append(data_dvc.GcProject(
            proj, _projects_dir() / proj / ".bare", sorted(project_worktrees(proj))))
    for proj in exclude or []:
        _check_name(proj, "project name")
    return data_dvc.collect_garbage(
        listed, wired=_dvc_wired_projects(), exclude=exclude,
        accept_missing=accept_missing, dry_run=dry_run, keep=keep)
