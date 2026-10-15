"""What would be lost if this machine died — the inventory behind the backup.

Two remotes cover the workspace between them: GitHub holds the code, chinook
holds the DVC cache. Everything else is disposable *by decision*, not by
accident — and a decision like that is only honest if it can be checked. This
module is that check. It names, per scope worktree:

  - work that exists nowhere but this disk: uncommitted edits, untracked files,
    and commits on branches with no upstream or ahead of one;
  - pins whose objects are absent from the local cache, which are therefore also
    absent from the sync that carries the cache.

It is read-only and touches no network. ``git`` answers the code half locally
(``@{upstream}`` is a recorded ref, not a fetch), and the data half is the same
resolution :mod:`awm.dvc.cache` does for a pull.

The other consumer is eviction. An LRU pass over the cache may only delete an
object it can prove is on the remote, and may only be run against a full list of
what every project still pins — both of which are this report's job to produce.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from awm.dvc import cache as cachemod
from awm.dvc.config import SHARED_CACHE, WORKSPACE_ROOT

log = logging.getLogger("awm.dvc.coverage")

# Long enough for a cold-cache `git status` on a worktree with a large ignored
# scratch tree; short enough that one wedged repo cannot hang the report.
_GIT_TIMEOUT = 120

# Per worktree. The counts are always exact; the samples are for reading.
_SAMPLE = 10


def _git(worktree: Path, *args: str) -> str | None:
    """Run git in ``worktree``; None if it fails, rather than raising.

    A single unreadable repo must degrade to one incomplete row, not take the
    whole inventory down — the report's value is that it always renders.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("git %s failed in %s: %s", " ".join(args), worktree, exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _worktrees_of(bare: Path) -> list[Path] | None:
    """Worktree paths git itself reports for a bare repo, or None if it can't.

    Asking git is what makes depth a non-question: a nested scope like
    ``fabfos/dev`` sits two levels down, while a vendored checkout a scope
    happens to have cloned is not a worktree of this repo and never appears.
    """
    raw = _git(bare, "worktree", "list", "--porcelain")
    if raw is None:
        return None
    out, path = [], None
    for line in raw.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line.strip() == "bare":
            path = None  # the bare repo's own entry — not a scope
        elif not line.strip():
            if path is not None:
                out.append(path)
            path = None
    if path is not None:
        out.append(path)
    return out


def find_worktrees(root: Path) -> list[Path]:
    """Scope worktrees under ``projects/``, at whatever depth.

    Enumerated from each project's bare repo rather than walked to a fixed
    depth. The fixed walk predated nested scope names and could not see one, so
    a nested scope simply dropped out of the inventory — and an audit whose job
    is naming what is uncovered fails silently when it under-reports. The walk
    survives as the fallback for a project with no readable bare repo.
    """
    projects = root / "projects"
    if not projects.is_dir():
        return []
    out: list[Path] = []
    for project in sorted(p for p in projects.iterdir() if p.is_dir()):
        listed = _worktrees_of(project / ".bare") if (project / ".bare").is_dir() else None
        if listed is not None:
            out.extend(listed)
            continue
        out.extend(s for s in sorted(project.iterdir())
                   if s.is_dir() and (s / ".git").exists())
    return sorted(set(out))


def _uncommitted(worktree: Path) -> dict[str, Any]:
    """Modified, staged, and untracked paths — what no commit is holding."""
    raw = _git(worktree, "status", "--porcelain=v1", "--untracked-files=normal")
    if raw is None:
        return {"error": "git status failed"}
    modified, untracked = [], []
    for line in raw.splitlines():
        if not line[2:].strip():
            continue
        (untracked if line.startswith("??") else modified).append(line[3:].strip())
    return {
        "modified": len(modified),
        "untracked": len(untracked),
        "modified_sample": modified[:_SAMPLE],
        "untracked_sample": untracked[:_SAMPLE],
    }


def _unpushed(worktree: Path) -> dict[str, Any]:
    """Commits on this worktree's branch that GitHub does not have.

    ``no_upstream`` and ``ahead`` are different failures with the same
    consequence: a branch nobody has pushed is as lost as a branch pushed five
    commits ago. Only the checked-out branch is examined — a scope *is* its
    branch, and the bare repo's other branches belong to the scopes that hold
    them.
    """
    branch = (_git(worktree, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    if not branch or branch == "HEAD":
        return {"branch": branch or None, "detached": True}
    counts = _git(worktree, "rev-list", "--left-right", "--count", f"{branch}...{branch}@{{upstream}}")
    if counts is None:
        return {"branch": branch, "no_upstream": True}
    ahead, _, behind = counts.strip().partition("\t")
    return {
        "branch": branch,
        "no_upstream": False,
        "ahead": int(ahead or 0),
        "behind": int(behind.strip() or 0),
    }


def _pins(worktree: Path) -> dict[str, Any]:
    """Objects this scope pins that are not in the shared cache.

    An object absent locally is absent from the sync too — the sync carries the
    cache, so a pin the cache cannot satisfy has no copy anywhere. Reported
    against the shared cache rather than the scope's configured one so that a
    project whose ``.dvc/config`` still names a stale directory is measured
    against where its bytes will actually be looked for.
    """
    if not (worktree / ".dvc").is_dir():
        return {"dvc": False}
    try:
        res = cachemod.resolve(worktree, SHARED_CACHE)
    except OSError as exc:
        return {"dvc": True, "error": str(exc)}
    return {
        "dvc": True,
        "pins": res.pins,
        "objects_known": len(res.objects),
        "missing": len(res.missing),
        # Leaves under an absent manifest are not counted in `missing` because
        # they cannot be named yet — reporting only `missing` would understate
        # the gap for exactly the scopes in the worst shape.
        "unresolved_manifests": len(res.unresolved),
    }


def _at_risk(row: dict) -> bool:
    unc, unp, pins = row["uncommitted"], row["unpushed"], row["pins"]
    return bool(
        unc.get("modified")
        or unc.get("untracked")
        or unc.get("error")
        or unp.get("detached")
        or unp.get("no_upstream")
        or unp.get("ahead")
        or pins.get("missing")
        or pins.get("unresolved_manifests")
    )


def report(*, at_risk_only: bool = True) -> dict[str, Any]:
    """Inventory every scope worktree against the two remotes."""
    rows = []
    for worktree in find_worktrees(WORKSPACE_ROOT):
        row = {
            "scope": str(worktree.relative_to(WORKSPACE_ROOT)),
            "uncommitted": _uncommitted(worktree),
            "unpushed": _unpushed(worktree),
            "pins": _pins(worktree),
        }
        row["at_risk"] = _at_risk(row)
        rows.append(row)

    at_risk = [r for r in rows if r["at_risk"]]
    return {
        "workspace": str(WORKSPACE_ROOT),
        "cache": str(SHARED_CACHE),
        "cache_exists": SHARED_CACHE.is_dir(),
        "scopes": len(rows),
        "scopes_at_risk": len(at_risk),
        "covered_by": {
            "code": "github (per project bare repo)",
            "data": "chinook (append-only sync of the shared cache)",
        },
        # Said out loud because it is a decision, not an oversight: the
        # whole-workspace mirror that used to carry these was retired.
        "not_covered": (
            "anything in a worktree that is neither committed-and-pushed nor "
            "DVC-pinned — scratch dirs, run outputs, and .awm/ service state. "
            "This is deliberate; those are disposable by construction."
        ),
        "worktrees": at_risk if at_risk_only else rows,
        "truncated": at_risk_only and len(rows) != len(at_risk),
    }
