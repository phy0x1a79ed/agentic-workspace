"""The scope hygiene check (scopes.md R6, R45).

Every 10th new scope triggers one pass. The count is its only trigger: no timer
runs it. The pass removes a scope's worktree only when the delete guard needs
no ``force``, the branch is merged, and no session used the scope for the idle
period. It keeps every branch, so a removed scope can be restored, and posts
one report that lists each held scope and why.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from awm.scopes import claims
from awm.scopes.dao import ScopesDAO
from awm.scopes.git_utils import detect_default_branch, run_git

log = logging.getLogger("awm.scopes.hygiene")

EVERY = 10
IDLE_DAYS = 14
#: Branches that are the project's own lines, never a scope to clean up.
LINES = ("dev", "release", "main", "master")
COUNTER = "scope_creates"


def _post(report: dict) -> None:
    from awm import gatewayclient
    from awm.config import node_name

    removed, held = report["removed"], report["held"]
    lines = [f"Removed {len(removed)} worktree(s); branches kept."]
    lines += [f"- removed {r['project']}/{r['scope']} (restore: scope create on branch {r['branch']})" for r in removed]
    lines += [f"- held {h['project']}/{h['scope']}: {h['why']}" for h in held]
    try:
        gatewayclient.call_sync("reporting", "ops_post", {
            "kind": "scope-hygiene", "node": node_name(),
            "title": f"Scope hygiene: {len(removed)} removed, {len(held)} held",
            "body": "\n".join(lines)}, timeout=60)
    except Exception as exc:  # noqa: BLE001 — the report is best effort; the pass already ran
        log.warning("scopes: hygiene report not posted: %s", exc)


#: Swapped in tests. Production writes the report to the board's Ops book.
post: Callable[[dict], None] = _post


def _merged(bare: Path, branch: str) -> str | None:
    """The line ``branch`` is merged into, among release, dev and the default branch, else None."""
    tip = run_git(["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"])
    if tip.returncode != 0:
        return None
    lines = ["release", "dev", detect_default_branch(bare)]
    refs = run_git(["git", "-C", str(bare), "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"])
    for ref in (refs.stdout or "").split():
        short = ref.removeprefix("refs/remotes/").split("/", 1)[-1] if ref.startswith("refs/remotes/") \
            else ref.removeprefix("refs/heads/")
        if short in lines and ref != f"refs/heads/{branch}":
            r = run_git(["git", "-C", str(bare), "merge-base", "--is-ancestor", tip.stdout.strip(), ref])
            if r.returncode == 0:
                return ref
    return None


def _idle_since(project: str, scope: str, worktree: Path, bare: Path, branch: str) -> float:
    """When a session last used the scope, in epoch seconds.

    Claims are new, so a scope without one falls back to the newest of its last
    commit and its ``.awm/`` metadata.
    """
    used = claims.last_used(project, scope)
    if used is not None:
        return used / 1000
    stamps = []
    r = run_git(["git", "-C", str(bare), "log", "-1", "--format=%ct", f"refs/heads/{branch}"])
    if r.returncode == 0 and (r.stdout or "").strip().isdigit():
        stamps.append(float(r.stdout.strip()))
    for p in (worktree / ".awm", worktree / ".awm" / "history.md"):
        try:
            stamps.append(p.stat().st_mtime)
        except OSError:
            pass
    return max(stamps, default=0.0)


def check(now: float | None = None, idle_days: float = IDLE_DAYS, dry_run: bool = False) -> dict:
    """One hygiene pass over every active scope on this node."""
    from awm.scopes import scopes as mod

    now = time.time() if now is None else now
    rows = ScopesDAO().query_all(
        "SELECT p.name AS project, a.scope, a.worktree, a.branch FROM agents a"
        " JOIN projects p ON p.id = a.project_id WHERE a.status IN ('allocated','active')"
        " AND a.is_vagrant = 0 ORDER BY p.name, a.scope")
    removed: list[dict] = []
    held: list[dict] = []
    for row in rows:
        project, scope, branch = row["project"], row["scope"], row["branch"]
        bare = mod.PROJECTS_DIR / project / ".bare"
        worktree = mod.PROJECTS_DIR / project / scope

        def hold(why: str) -> None:
            held.append({"project": project, "scope": scope, "why": why})

        if not bare.is_dir() or not worktree.is_dir():
            continue
        if branch in LINES or branch == detect_default_branch(bare):
            continue
        holder = claims.live_holder(project, scope)
        if holder is not None:
            hold(f"in use by session {holder['session_id']}")
            continue
        idle = (now - _idle_since(project, scope, worktree, bare, branch)) / 86400
        if idle < idle_days:
            hold(f"used {idle:.0f} day(s) ago")
            continue
        if _merged(bare, branch) is None:
            hold(f"branch {branch} is not merged into release, dev or the default branch")
            continue
        status = run_git(["git", "-C", str(worktree), "status", "--porcelain"])
        if (status.stdout or "").strip():
            hold("uncommitted or untracked files")
            continue
        ignored = mod._ignored_files(worktree)
        if ignored:
            hold(f"ignored files outside .awm/ ({', '.join(ignored[:3])})")
            continue
        if dry_run:
            removed.append({"project": project, "scope": scope, "branch": branch, "dry_run": True})
            continue
        try:
            mod.delete_scope(project, scope)
        except (RuntimeError, OSError, FileNotFoundError) as exc:
            hold(f"delete refused: {exc}")
            continue
        removed.append({"project": project, "scope": scope, "branch": branch})
    report = {"removed": removed, "held": held, "dry_run": dry_run}
    if not dry_run and (removed or held):
        post(report)
    return report


def _run() -> None:
    try:
        report = check()
        log.info("scopes: hygiene removed %d, held %d", len(report["removed"]), len(report["held"]))
    except Exception:  # noqa: BLE001 — a failed pass must never take the service down
        log.exception("scopes: hygiene pass failed")


def after_create(background: bool = True) -> int:
    """Count a new scope. Every :data:`EVERY` th one starts a pass off the request path."""
    n = claims.bump(COUNTER)
    if n % EVERY == 0:
        if background:
            threading.Thread(target=_run, name="scope-hygiene", daemon=True).start()
        else:
            _run()
    return n


def status() -> dict[str, Any]:
    row = ScopesDAO().query_one("SELECT value FROM scope_counters WHERE name=?", (COUNTER,))
    n = row["value"] if row else 0
    return {"creates": n, "next_check_after": EVERY - n % EVERY}
