"""Per-conversation output folders on the host, kept in step with the workbench.

The sandbox reads all of ``projects/`` but writes only under ``OUTPUTS_ROOT``,
laid out like the workbench: ``<project-slug>/<conversation-id8>/``. The folders
are a staging area, untracked: one is created empty for every conversation and
deleted, contents and all, once its conversation is gone. Anything worth keeping
is copied out before then.

**Why the layout is shallow.** Before sandboxed code runs, the daemon scans
every granted folder for credential files, four levels deep and at most 5,000
folders. A writable grant whose scan stops short raises a ``grant_scan``
approval that ``--dangerously-skip-approvals`` does not answer, and every
conversation then hangs on its first command. Two levels of our own leave the
agents two more before they eat into that cap; ``ALERT_FOLDERS`` warns early.
The same scan is why a writable grant may not overlap a read grant: an overlap
makes the daemon judge the read grant too, and ``projects/`` is far past the cap.

**How a conversation finds its folder.** A kernel's working directory is
``workspaces/<root-frame-id>``, so each project's context (the instructions the
workbench gives every conversation in it) carries one line naming the folder in
terms of that directory. The line is also how a project keeps its slug across a
rename: the slug is read back from it rather than recomputed.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from awm.config import WORKSPACE_ROOT

from awm.claude_science.api import Owner, WorkbenchError

log = logging.getLogger("awm.claude_science.outputs")

OUTPUTS_ROOT = Path(os.environ.get("CLAUDE_SCIENCE_OUTPUTS_DIR")
                    or WORKSPACE_ROOT / "science-outputs")

SYNC_INTERVAL_S = float(os.environ.get("CLAUDE_SCIENCE_OUTPUTS_SYNC_S", "60"))

#: The daemon's credential scan: depth, folder cap, and the names it skips.
SCAN_DEPTH = 4
SCAN_CAP = 5000
SCAN_SKIP = frozenset({".git", "node_modules", ".venv", "__pycache__"})
ALERT_FOLDERS = 4000

MARKER = "Output folder (awm):"
ID8 = re.compile(r"^[0-9a-f]{8}$")


def count_folders(root: Path, *, depth: int = SCAN_DEPTH,
                  cap: int = SCAN_CAP) -> int:
    """Folders the daemon's scan would list under ``root``, stopping past ``cap``."""
    count = 0
    level = [Path(root)]
    for _ in range(depth):
        nxt: list[Path] = []
        for folder in level:
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                if entry.name in SCAN_SKIP:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    count += 1
                    if count > cap:
                        return count
                    nxt.append(Path(entry.path))
        level = nxt
    return count


def slug(name: str | None, project_id: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or project_id


def context_line(root: Path, project_slug: str) -> str:
    return (f"{MARKER} {root / project_slug}/<first 8 characters of your "
            f"working directory's name>/ — write files worth keeping there. "
            f"It is a staging area, deleted with this conversation.")


def _marker_slug(context: str | None, root: Path) -> str | None:
    match = re.search(re.escape(f"{MARKER} {root}/") + r"([^/\s]+)/",
                      context or "")
    return match.group(1) if match else None


def with_line(context: str | None, line: str) -> str:
    kept = [ln for ln in (context or "").splitlines()
            if not ln.startswith(MARKER)]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join([*kept, line]) if kept else line


def assign_slugs(projects: list[dict[str, Any]], root: Path) -> dict[str, str]:
    """``project_id → slug``: a slug already in a context line wins, then the
    oldest project takes the plain name and a later namesake gets a suffix."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for p in projects:
        have = _marker_slug(p.get("context"), root)
        if have and have not in taken:
            out[p["project_id"]] = have
            taken.add(have)
    for p in sorted(projects, key=lambda p: p.get("created_at") or ""):
        pid = p["project_id"]
        if pid in out:
            continue
        s = slug(p.get("name"), pid)
        if s in taken:
            s = f"{s}-{pid.removeprefix('proj_')[:6]}"
        out[pid] = s
        taken.add(s)
    return out


def list_projects(owner: Owner) -> list[dict[str, Any]]:
    out = owner.get("/api/projects", params={"include_archived": "true",
                                             "limit": 500})
    return list(out.get("projects") or [])


def list_project_conversations(owner: Owner, project_id: str) -> list[dict[str, Any]]:
    """Every root conversation in a project. ``/api/frames`` caps at 100 overall."""
    rows = owner.get(f"/api/projects/{project_id}/benches",
                     params={"limit": 100000})
    return [r for r in rows or [] if not r.get("parent_frame_id")]


def ensure_context(owner: Owner, project: dict[str, Any], project_slug: str,
                   root: Path = OUTPUTS_ROOT) -> bool:
    """Give the project's context the folder line. True when it changed."""
    line = context_line(root, project_slug)
    if line in (project.get("context") or "").splitlines():
        return False
    owner.request("PATCH", f"/api/projects/{project['project_id']}",
                  json={"context": with_line(project.get("context"), line)})
    return True


def ensure_project(owner: Owner, project_id: str,
                   root: Path = OUTPUTS_ROOT) -> Path:
    """Set up one project's folder line before its next conversation starts."""
    if not root.is_dir():
        raise WorkbenchError(f"{root} does not exist; create it and grant it rw")
    projects = list_projects(owner)
    slugs = assign_slugs(projects, root)
    project = next((p for p in projects if p["project_id"] == project_id), None)
    if project is None:
        raise WorkbenchError(f"project {project_id} is not listed")
    ensure_context(owner, project, slugs[project_id], root)
    folder = root / slugs[project_id]
    folder.mkdir(exist_ok=True)
    return folder


def _project_dirs(root: Path) -> list[Path]:
    """Folders a project may own. Dot-folders belong to the sandbox (``.claude``)."""
    return [d for d in root.iterdir()
            if d.is_dir() and not d.is_symlink() and not d.name.startswith(".")]


def _existing(root: Path) -> dict[str, Path]:
    """``id8 → folder`` for every conversation folder on disk."""
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    for project_dir in _project_dirs(root):
        for conv in project_dir.iterdir():
            if ID8.match(conv.name) and conv.is_dir() and not conv.is_symlink():
                found[conv.name] = conv
    return found


def _gone(owner: Owner, id8: str) -> bool:
    """Only a 404 from the daemon counts; any other failure keeps the folder."""
    try:
        owner.get(f"/api/frames/{id8}", params={"shallow": "true"})
    except WorkbenchError as exc:
        return exc.status == 404
    return False


def sync(owner: Owner, root: Path = OUTPUTS_ROOT) -> dict[str, Any]:
    """Make ``root`` match the workbench's projects and conversations."""
    if not root.is_dir():
        raise WorkbenchError(f"{root} does not exist; create it and grant it rw")

    projects = list_projects(owner)
    slugs = assign_slugs(projects, root)
    live: dict[str, Path] = {}
    for p in projects:
        for conv in list_project_conversations(owner, p["project_id"]):
            live[conv["id"][:8]] = root / slugs[p["project_id"]] / conv["id"][:8]

    report: dict[str, Any] = {"created": [], "moved": [], "deleted": [],
                              "contexts": []}
    for p in projects:
        if ensure_context(owner, p, slugs[p["project_id"]], root):
            report["contexts"].append(slugs[p["project_id"]])
        (root / slugs[p["project_id"]]).mkdir(exist_ok=True)

    existing = _existing(root)
    for id8, want in live.items():
        have = existing.pop(id8, None)
        if have is None:
            want.mkdir(parents=True, exist_ok=True)
            report["created"].append(str(want.relative_to(root)))
        elif have != want:
            want.parent.mkdir(parents=True, exist_ok=True)
            have.rename(want)
            report["moved"].append(str(want.relative_to(root)))

    for id8, folder in existing.items():
        if _gone(owner, id8):
            shutil.rmtree(folder)
            report["deleted"].append(str(folder.relative_to(root)))

    claimed = set(slugs.values())
    for project_dir in _project_dirs(root):
        if project_dir.name not in claimed and not any(project_dir.iterdir()):
            project_dir.rmdir()

    report["folders"] = count_folders(root)
    if report["folders"] >= ALERT_FOLDERS:
        log.warning("claude-science: %s holds %d folders within the daemon's "
                    "scan depth; at %d every conversation stalls on a grant_scan "
                    "approval", root, report["folders"], SCAN_CAP)
    return report


def check_writable_grant(path: str, grants: list[dict[str, Any]]) -> None:
    """Refuse a rw grant that would make the daemon ask before every command."""
    target = Path(path).resolve()
    for g in grants:
        if g.get("mode") != "ro":
            continue
        other = Path(g.get("hostPath") or g.get("id") or "").resolve()
        if target == other or target in other.parents or other in target.parents:
            raise ValueError(
                f"rw:{target} overlaps ro:{other}. The daemon then scans the "
                f"read grant for credential files too, and a scan that hits "
                f"its {SCAN_CAP}-folder cap stalls every conversation on an "
                f"approval nobody answers. Grant a folder outside it.")
    n = count_folders(target)
    if n > SCAN_CAP:
        raise ValueError(
            f"rw:{target} holds more than {SCAN_CAP} folders within "
            f"{SCAN_DEPTH} levels, so the daemon's credential scan cannot "
            f"finish and every conversation would stall on an approval. "
            f"Grant a smaller folder.")
