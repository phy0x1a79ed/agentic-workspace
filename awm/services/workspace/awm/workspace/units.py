"""Workspace unit lifecycle — create / retain / destroy / resolve.

A unit is the DAG execution sandbox an agents-service placement runs in. Its
layout under ``SERVICES_DIR/workspace/units/<project>/<unit_slug>/``:

    CONTEXT.md              the rendered brief (what scopes put in .awm/context.md)
    inputs/<name>           read-only materialized pre-readings (set 0444)
    deliverable/<contract>/ deliverable staging (one dir per output contract)
    scratch/                free scratch space for the agent

The unit is **one-per-task, reused across the lifecycle**: the same slug carries
a task through PLANNING → VERIFYING_PLAN → ACTIVE, each stage spawning a fresh
agent with a different tool profile into the same directory. ``retain`` is the
between-stages / audit state (free the unit but keep its contents); ``destroy``
only happens at a terminal outcome.

This service deliberately has NO git, NO branch, NO channel, and NO cross-service
calls — that is exactly the weight we shed from the CLI-only ``scopes`` service.
Pre-readings arrive already resolved (inline ``content`` or a source ``path`` to
copy); ref→content resolution is the caller's job upstream.
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

from awm.persistence import databases

from awm.workspace.dao import WorkspaceDAO, init

_dao = WorkspaceDAO()

# Subdirectories every unit gets at create time.
_DELIVERABLE = "deliverable"
_INPUTS = "inputs"
_SCRATCH = "scratch"
_CONTEXT = "CONTEXT.md"


def _units_root() -> Path:
    # Read SERVICES_DIR off the module at call time so a test harness that
    # monkeypatches awm.persistence.databases.SERVICES_DIR redirects units too.
    return databases.SERVICES_DIR / "workspace" / "units"


def unit_path(project: str, unit_slug: str) -> Path:
    """Deterministic on-disk path of a unit. The DB stores this verbatim."""
    return _units_root() / project / unit_slug


def _read_only(path: Path) -> None:
    """Best-effort: strip write bits so a materialized input can't be edited."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass


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


def create(*, project: str, unit_slug: str, context_md: str = "",
           prereadings: list | None = None) -> dict:
    """Provision (or re-activate) a unit directory + DB row.

    Idempotent: re-creating an existing slug rewrites CONTEXT.md and re-layers
    the pre-readings without wiping deliverables/scratch (so a re-place after a
    crash keeps prior work). Returns ``{project, unit_slug, path, state,
    inputs}``."""
    init()
    path = unit_path(project, unit_slug)
    inputs_dir = path / _INPUTS
    (path / _DELIVERABLE).mkdir(parents=True, exist_ok=True)
    inputs_dir.mkdir(parents=True, exist_ok=True)
    (path / _SCRATCH).mkdir(parents=True, exist_ok=True)

    (path / _CONTEXT).write_text(context_md or "", encoding="utf-8")
    names = _materialize_prereadings(inputs_dir, prereadings or [])

    row = _dao.upsert_unit(
        project=project, unit_slug=unit_slug, path=str(path), state="active")
    return {
        "project": project, "unit_slug": unit_slug, "path": str(path),
        "state": row["state"], "inputs": names,
    }


def retain(*, project: str, unit_slug: str) -> dict:
    """Free a unit but KEEP its contents (mark ``idle``).

    The ``scope_complete(cleanup=False)`` analog: partial work + the deliverable
    staging survive for audit or for the next lifecycle stage (planner reuse).
    No-op-safe if the unit is unknown."""
    init()
    matched = _dao.set_state(project, unit_slug, "idle")
    path = unit_path(project, unit_slug)
    return {"project": project, "unit_slug": unit_slug, "path": str(path),
            "retained": matched, "state": "idle" if matched else "unknown"}


def destroy(*, project: str, unit_slug: str) -> dict:
    """Remove a unit directory and its row (terminal cleanup). Idempotent."""
    init()
    path = unit_path(project, unit_slug)
    removed_dir = False
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        removed_dir = True
    removed_row = _dao.delete_unit(project, unit_slug)
    return {"project": project, "unit_slug": unit_slug,
            "destroyed": removed_dir or removed_row}


def resolve(*, project: str, unit_slug: str) -> dict:
    """Return a unit's path + state (or ``state='unknown'`` if there's no row).

    Used by the agents service to recover the workdir on respawn without
    hardcoding the on-disk layout."""
    init()
    row = _dao.get_unit(project, unit_slug)
    path = unit_path(project, unit_slug)
    if row is None:
        return {"project": project, "unit_slug": unit_slug,
                "path": str(path), "state": "unknown", "exists": path.exists()}
    return {"project": project, "unit_slug": unit_slug, "path": row["path"],
            "state": row["state"], "exists": Path(row["path"]).exists()}
