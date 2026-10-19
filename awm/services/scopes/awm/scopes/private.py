"""Set and clear this node's private marks on projects and scopes.

The marks file belongs to :mod:`awm.config.privacy`; this service is its only
writer, so it validates each target against the projects it knows and refuses a
mark whose transcript slug would also cover some other project or scope.
"""

from __future__ import annotations

import threading
from pathlib import Path

from awm.config import privacy
from awm.scopes import scopes as _scopes
from awm.scopes.dao import ScopesDAO
from awm.scopes.identity import agent_record_for_scope

_write_lock = threading.Lock()


def _known_projects() -> set[str]:
    names = {r["name"] for r in ScopesDAO().query_all("SELECT name FROM projects")}
    root = _scopes.PROJECTS_DIR
    if root.is_dir():
        names |= {b.parent.name for b in root.glob("*/.bare") if b.is_dir()}
    return names


def _scope_worktree(project: str, scope: str) -> Path:
    rec = agent_record_for_scope(project, scope, active_only=False)
    if rec is not None:
        return _scopes._resolve_worktree(project, scope, rec.get("worktree"))
    main = _scopes.main_checkouts(project).get(project)
    if main is not None and main.scope == scope:
        return Path(main.worktree)
    raise FileNotFoundError(f"Scope '{project}/{scope}' not found")


def _target_path(project: str, scope: str | None) -> Path:
    if project not in _known_projects():
        raise FileNotFoundError(f"Project '{project}' not found")
    if scope is None:
        return _scopes.PROJECTS_DIR / project
    return _scope_worktree(project, scope)


def _other_dirs(project: str) -> list[Path]:
    """Every registered project directory and scope worktree but ``project``'s own directory."""
    dirs = [_scopes.PROJECTS_DIR / name for name in _known_projects() if name != project]
    rows = ScopesDAO().query_all(
        "SELECT p.name AS project, a.scope, a.worktree "
        "FROM agents a JOIN projects p ON p.id = a.project_id")
    dirs += [_scopes._resolve_worktree(r["project"], r["scope"], r["worktree"]) for r in rows]
    return dirs


def slug_collisions(project: str, path: Path) -> list[str]:
    """Directories outside ``path`` whose transcript slug the mark at ``path`` would also cover."""
    prefix = privacy.slug(path)
    clashes = set()
    for other in _other_dirs(project):
        if other == path or other.is_relative_to(path):
            continue
        if privacy.slug_covers(prefix, privacy.slug(other)):
            clashes.add(str(other))
    return sorted(clashes)


def set_private(project: str, scope: str | None, private: bool) -> dict:
    """Mark or unmark a project, or one scope of it, private; returns the project's marks."""
    scope = scope or None
    with _write_lock:
        current = privacy.marks()
        rest = [m for m in current if (m["project"], m["scope"]) != (project, scope)]
        if private:
            path = _target_path(project, scope)
            clashes = slug_collisions(project, path)
            if clashes:
                raise ValueError(
                    f"cannot mark {path} private: transcripts of {', '.join(clashes)} "
                    f"share its slug prefix and would be hidden too")
            rest.append({"project": project, "scope": scope, "path": str(path)})
        elif len(rest) == len(current):
            _target_path(project, scope)
        privacy.write_marks(rest)
    return {
        "project": project,
        "scope": scope,
        "private": privacy.hidden(project, scope),
        "marks": [m for m in privacy.marks() if m["project"] == project],
    }
