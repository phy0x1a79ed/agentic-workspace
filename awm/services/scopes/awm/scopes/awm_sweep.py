"""The one-shot sweep that empties each worktree's ``.awm/`` folder (scopes.md R142).

The deploy runs it once per node. It removes what nothing reads any more, moves
the DVC hooks' local state into the worktree's git dir, and rewrites the
project's DVC hooks so they write there. It leaves ``context.md`` for the scope
briefs one-shot, and any file it does not know, and reports both. Idempotent.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from awm.dvc import data_dvc

log = logging.getLogger("awm.scopes.awm_sweep")

AWM_DIRNAME = ".awm"
DROPPED_FILES = ("history.md", "mcp-opencode.json")
DROPPED_LINKS = ("data", "skills")
HOOK_STATE = (data_dvc.MOUNTS_FILE, data_dvc.CHECKOUT_FAILED_FILE)


def sweep(projects_dir: Path | None = None, dry_run: bool = False) -> dict:
    """Sweep every worktree of every project under ``projects_dir``."""
    from awm.scopes.scopes import PROJECTS_DIR, _worktree_branches

    root = Path(projects_dir or PROJECTS_DIR)
    worktrees: list[dict] = []
    hooks: dict[str, object] = {}
    errors: list[dict] = []
    for bare in sorted(root.glob("*/.bare")):
        project = bare.parent.relative_to(root).as_posix()
        try:
            paths = sorted(p for p in _worktree_branches(bare) if p.is_dir())
        except (OSError, RuntimeError) as exc:
            errors.append({"project": project, "error": str(exc)})
            continue
        for wt in paths:
            try:
                did = sweep_worktree(wt, dry_run=dry_run)
            except OSError as exc:
                errors.append({"worktree": str(wt), "error": f"{type(exc).__name__}: {exc}"})
                continue
            if did:
                worktrees.append({"project": project, "worktree": str(wt), **did})
        dvc_wt = next((wt for wt in paths if data_dvc.is_dvc_repo(wt)), None)
        if dvc_wt is not None and data_dvc.dvc_available():
            hooks[project] = ("would-rewrite" if dry_run
                              else data_dvc.ensure_repo_wiring(dvc_wt).get("actions", {}))
        elif dvc_wt is not None:
            hooks[project] = "dvc not found; hooks left as they are"
    left = [w for w in worktrees if w.get("left")]
    return {"dry_run": dry_run, "worktrees": worktrees, "hooks": hooks, "errors": errors,
            "left_behind": len(left)}


def sweep_worktree(worktree: Path, *, dry_run: bool = False) -> dict:
    """Empty one worktree's ``.awm/``. Returns what it did; empty when there was nothing."""
    awm = worktree / AWM_DIRNAME
    if awm.is_symlink() or not awm.is_dir():
        return {}
    did: dict = {}
    removed: list[str] = []
    for name in DROPPED_LINKS:
        link = awm / name
        if link.is_symlink():
            removed.append(f"{name}->{os.readlink(link)}")
            if not dry_run:
                link.unlink()
    for name in DROPPED_FILES:
        f = awm / name
        if f.is_file() and not f.is_symlink():
            removed.append(name)
            if not dry_run:
                f.unlink()
    if removed:
        did["removed"] = removed

    moved = [name for name in HOOK_STATE if (awm / name).is_file()]
    if moved:
        did["moved"] = moved
        if not dry_run:
            _move_hook_state(worktree, awm, moved)

    rest = sorted(p.name for p in awm.iterdir()) if awm.is_dir() else []
    if dry_run:
        rest = [n for n in rest if n not in moved and n not in DROPPED_FILES
                and not (n in DROPPED_LINKS and (awm / n).is_symlink())]
    if rest:
        did["left"] = rest
    else:
        did["awm_dir"] = "removed"
        if not dry_run:
            awm.rmdir()
    return did


def _move_hook_state(worktree: Path, awm: Path, names: list[str]) -> None:
    state = data_dvc.state_dir(worktree)
    state.mkdir(parents=True, exist_ok=True)
    for name in names:
        src, dest = awm / name, state / name
        if name == data_dvc.CHECKOUT_FAILED_FILE and dest.exists():
            with dest.open("a") as fh:
                fh.write(src.read_text())
            src.unlink()
        elif dest.exists():
            log.warning("%s: %s already holds %s; keeping it and dropping the old copy",
                        worktree, state, name)
            src.unlink()
        else:
            shutil.move(str(src), str(dest))
