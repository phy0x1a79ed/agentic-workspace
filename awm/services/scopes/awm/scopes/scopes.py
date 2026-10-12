"""Scope CRUD — create, complete, delete, search, heal (v1 modular).

A "scope" is represented by a row in the ``agents`` table
(status ∈ allocated|active|retired) within the scopes service's own DB.
All SQL goes through ScopesDAO.

Cross-service: ``scopes._default_context`` previously called
``awm.services.skills.find_by_name`` — that import is replaced by a
graceful fallback hint (skills service is out-of-process; calling it via
gatewayclient is non-essential for context.md generation).

A scope IS the channel, held by the journal service: history.md is rendered
there from the scope's journal posts (``journal_render``), and the vagrant
session helper subscribes the user to the scope's channel.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import uuid as _uuid
from pathlib import Path

from awm.config import (
    WORKSPACE_ROOT,
    PROJECTS_DIR,
    DATA_DIR,
    SKILLS_DIR,
    GITHUB_USER,
    VAGRANT_PROJECT,
)
from awm.scopes.git_utils import run_git, detect_default_branch
from awm.dvc import data_dvc
from awm.scopes import search_index
from awm.scopes.dao import ScopesDAO
from awm.scopes.identity import (
    agent_id_for_scope,
    agent_record_for_scope,
    ensure_agent,
    ensure_project,
    project_id_for_name,
    ms_to_iso,
    now_ms,
    retire_agent,
)
from awm.scopes.models import (
    ProjectCreateRequest,
    ScopeCreateRequest,
    ScopeUpdateRequest,
    ScopeSyncRequest,
    ScatterGatherResponse,
    ScopeInfo,
    ScopeListResponse,
    ScopeActionResponse,
)
from awm.scopes._validation import validate_name, validate_scope_name

log = logging.getLogger("awm.scopes")


def _get_awm_dir(repo_dir: Path) -> Path:
    return repo_dir / ".awm"


def _worktree_admin_dir(bare_dir: Path, worktree_dir: Path) -> Path | None:
    """git's per-worktree admin directory under ``.bare/worktrees/``, or None.

    Asked of git rather than derived from the worktree's basename. git names the
    admin dir after the basename and disambiguates on collision (``dev``,
    ``dev1``, …), so a nested scope ``fabfos/dev`` and a flat scope ``dev`` share
    a basename while owning different admin dirs — deriving it would have
    deleting either one silently destroy the other's.
    """
    r = run_git(["git", "-C", str(worktree_dir), "rev-parse", "--absolute-git-dir"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    admin = Path(r.stdout.strip())
    try:
        admin.relative_to(bare_dir / "worktrees")
    except ValueError:
        return None  # not a worktree of this bare repo — leave it alone
    return admin


def _project_worktrees(project: str) -> list[Path]:
    """Every worktree of a project's bare repo, at whatever depth.

    Enumerated from git, not by globbing ``projects/<project>/*``: a nested scope
    such as ``fabfos/dev`` sits two levels down, and for ``data_gc`` a worktree
    missing from this list is a worktree whose pinned objects fall out of the
    keep-set — i.e. get collected. Raises rather than degrading to a short list,
    for that same reason.
    """
    bare = PROJECTS_DIR / project / ".bare"
    if not bare.exists():
        raise FileNotFoundError(f"Project '{project}' has no bare repo at {bare}")
    r = run_git(["git", "-C", str(bare), "worktree", "list", "--porcelain"])
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
            path = None  # the bare repo's own entry — not a scope
        elif not line.strip():
            if path is not None:
                out.append(path)
            path = None
    if path is not None:
        out.append(path)
    return out


def _live_worktrees_under(bare_dir: Path, path: Path) -> list[Path]:
    """Registered worktrees of ``bare_dir`` that live *beneath* ``path``.

    Non-empty means ``path`` is a container for nested scopes rather than a scope
    — ``projects/metasmith/fabfos`` holding ``fabfos/dev`` — and nothing may
    remove it wholesale.
    """
    r = run_git(["git", "-C", str(bare_dir), "worktree", "list", "--porcelain"])
    if r.returncode != 0:
        return []
    out: list[Path] = []
    for line in r.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        wt = Path(line[len("worktree "):])
        if wt != path and wt.is_relative_to(path):
            out.append(wt)
    return out


def _prune_empty_parents(stop_at: Path, path: Path) -> None:
    """Remove now-empty container directories left by a nested scope's removal,
    up to but never including ``stop_at`` (the project directory).

    A container exists only to hold the scopes beneath it, so once the last one
    goes the directory is noise. ``rmdir`` is the whole safety argument: it
    refuses a directory that still has anything in it.
    """
    stop_at = stop_at.resolve()
    try:
        current = path.resolve()
    except OSError:
        return
    while current != stop_at and stop_at in current.parents:
        try:
            current.rmdir()
        except OSError:
            return  # not empty, or gone — either way, stop
        current = current.parent


def _scope_branch(project: str, scope: str, worktree: Path | None = None) -> str:
    """The branch a scope is actually on.

    Recomputing ``feat/<scope>`` is wrong for any scope created with an explicit
    ``branch_name`` — which is every nested scope, whose branch is its own name
    with no ``feat/`` prefix. The DB row is authoritative; the worktree's own HEAD
    is the fallback for a scope with no row.
    """
    rec = agent_record_for_scope(project, scope, active_only=False)
    if rec is not None and rec["branch"]:
        return rec["branch"]
    if worktree is not None and worktree.exists():
        r = run_git(["git", "-C", str(worktree), "branch", "--show-current"])
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return f"feat/{scope}"


def _assert_branch_available(bare_dir: Path, branch: str) -> None:
    """Refuse a branch name git cannot hold, naming the reason.

    Two failures matter. A malformed ref, and — the one nesting introduces — a
    directory/file conflict: git stores refs as paths, so once ``feat/fabfos``
    exists no ref may be named ``feat/fabfos/dev``, and vice versa. git's own
    error for the second names neither branch.
    """
    r = run_git(["git", "check-ref-format", f"refs/heads/{branch}"])
    if r.returncode != 0:
        raise ValueError(f"'{branch}' is not a valid git branch name")

    r = run_git(["git", "-C", str(bare_dir), "for-each-ref",
                 "--format=%(refname:short)", "refs/heads/"])
    if r.returncode != 0:
        return  # let worktree add report whatever is wrong with the repo
    existing = set(r.stdout.split())
    if branch in existing:
        return  # a plain collision — create_scope's own path reports it

    parts = branch.split("/")
    for i in range(1, len(parts)):
        prefix = "/".join(parts[:i])
        if prefix in existing:
            raise ValueError(
                f"Cannot create branch '{branch}': branch '{prefix}' already exists "
                f"and git cannot hold both (refs are stored as paths). Rename or "
                f"delete '{prefix}' first."
            )
    beneath = sorted(b for b in existing if b.startswith(branch + "/"))
    if beneath:
        raise ValueError(
            f"Cannot create branch '{branch}': "
            f"{', '.join(beneath[:3])}{' …' if len(beneath) > 3 else ''} "
            f"already exist beneath it and git cannot hold both "
            f"(refs are stored as paths)."
        )


def _cleanup_worktree(bare_dir: Path, worktree_dir: Path, feature_branch: str,
                      *, project: str | None = None, scope: str | None = None,
                      force: bool = False) -> dict:
    """Remove a scope's worktree and branch.

    Deleting a worktree unlinks names, never bytes: a scope's data is hardlinks
    into the shared cache and its pins are commits on a branch, so the cache
    object survives on its own link.

    What does not survive is work that was never committed — and that is now the
    *same* hazard for data as for code. An un-added chunk or an un-committed pin
    dies with the worktree exactly as un-committed source does, so the guard is
    simply ``git status``, which covers both at once. That is the whole point of
    collapsing them onto one lever. ``force=True`` overrides it.

    :func:`chmod_dirs_writable` is the fallback after ``worktree remove`` fails:
    ``rmtree`` needs write permission on containing directories. It deliberately
    never touches *files* — a materialised file shares its inode with the shared
    cache object, so ``chmod +w`` on it would unprotect that object for every
    other scope and project on the machine.

    Nesting adds a shape this has to refuse outright: ``projects/<p>/fabfos`` is
    not a scope, it is the *directory* the scope ``fabfos/dev`` lives in. It has
    no worktree of its own, so every check below reads it as "a stale leftover" —
    the dirty guard finds no repo, ``worktree remove`` declines, and the rmtree
    fallback takes the live scopes beneath it with the directory.
    """
    guard: dict = {"result": "ok", "path": str(worktree_dir)}
    admin_dir = _worktree_admin_dir(bare_dir, worktree_dir)

    contained = _live_worktrees_under(bare_dir, worktree_dir)
    if contained:
        raise RuntimeError(
            f"Refusing to remove {worktree_dir}: it is not a scope, it contains "
            f"{len(contained)} live worktree(s) "
            f"({', '.join(p.name for p in contained[:3])}). Delete the nested "
            f"scopes individually; the directory goes when the last one does."
        )

    # Only probe a directory that is genuinely the top of a worktree. Run inside
    # anything else and git walks *up* to the first enclosing repo — the
    # workspace root, typically — and answers about that instead, so a stale
    # directory full of real files reads back as clean.
    if worktree_dir.is_dir() and admin_dir is not None:
        st = run_git(["git", "-C", str(worktree_dir), "status", "--porcelain"])
        pending = [ln for ln in (st.stdout or "").splitlines() if ln.strip()]
        if pending:
            guard = {"result": "dirty", "path": str(worktree_dir),
                     "pending": len(pending), "sample": pending[:5]}
            if not force:
                raise RuntimeError(
                    f"Refusing to remove {worktree_dir}: {len(pending)} uncommitted "
                    f"change(s) would be lost (e.g. {', '.join(p.strip() for p in pending[:3])}). "
                    f"Commit them — `dvc add` any new data chunk first — or pass force=true."
                )
            guard["detail"] = f"forced: {len(pending)} uncommitted change(s) discarded"

    r = run_git(["git", "-C", str(bare_dir), "worktree", "remove", str(worktree_dir), "--force"])
    if r.returncode != 0 and worktree_dir.exists():
        # Directories only — see chmod_dirs_writable. Unlinking a hardlink
        # cannot harm the cache object; only `dvc gc` removes those.
        data_dvc.chmod_dirs_writable(worktree_dir)
        shutil.rmtree(worktree_dir, ignore_errors=True)
    if admin_dir is not None and admin_dir.exists():
        shutil.rmtree(admin_dir, ignore_errors=True)
    # Catches the admin dir of a worktree whose directory was already gone — the
    # one case _worktree_admin_dir cannot resolve. git prunes only entries it has
    # established are dead, so this can never reach a live sibling.
    run_git(["git", "-C", str(bare_dir), "worktree", "prune"])
    run_git(["git", "-C", str(bare_dir), "branch", "-D", feature_branch])
    _prune_empty_parents(bare_dir.parent, worktree_dir.parent)
    return guard


def _default_context(project: str, scope: str) -> str:
    return (
        f"# {project}/{scope}\n\n"
        f"## Startup\n\n"
        f"1. `journal(verb=\"render\", args={{project:\"{project}\", scope:\"{scope}\"}})`\n"
        f"2. Read `.awm/history.md` — session history, open issues, resolved items.\n"
        f"3. `journal(verb=\"fetch\", args={{scope:\"{scope}\", kind:\"message\"}})`\n\n"
        f"## This scope\n\n"
        f"_Why this scope exists._\n\n"
        f"## Working here\n\n"
        f"The current directory is the git worktree. Data is at `data/`. Do not edit "
        f"`.awm/history.md` — use the `journal` verbs.\n\n"
        f"Workspace rules — data versioning, environments, the git model — are in "
        f"the workspace `AGENTS.md` and are not repeated here.\n\n"
        f"## Debrief\n\n"
        f"Run the `debrief` skill when the work is done. When a plan drives the work, "
        f"its last task is the debrief.\n"
    )


def _ensure_project_row(project: str, *, conn=None) -> str:
    bare = PROJECTS_DIR / project / ".bare"
    return ensure_project(project, repo_path=str(bare), conn=conn)


def _row_to_info(row) -> ScopeInfo:
    return ScopeInfo(
        project=row["project_name"] if "project_name" in row.keys() else row["project"],
        scope=row["scope"],
        status=row["status"],
        branch=row["branch"],
        worktree=row["worktree"],
        repo_path=row["worktree"],
        session=row["session"] or 1,
    )


def _generate_history_md(project: str, scope: str) -> str:
    """The scope's ``history.md`` text, rendered by the journal service."""
    from awm.gatewayclient import call_sync

    # 'allocated' is how every scope is born and nothing promotes it to
    # 'active', so filtering on 'active' alone named ~1 sibling in 23. Every
    # other live-scope predicate in this service uses the pair; this was the
    # sole outlier.
    siblings = ScopesDAO().query_all(
        "SELECT a.scope FROM agents a "
        "JOIN projects p ON p.id = a.project_id "
        "WHERE p.name=? AND a.scope!=? AND a.status IN ('allocated','active')",
        (project, scope),
    )
    reply = call_sync(
        "journal", "render",
        {"project": project, "scope": scope, "write": False,
         "siblings": [r["scope"] for r in siblings]},
        timeout=60.0,
    )
    return reply["content"]


def ensure_vagrant_repo() -> Path:
    """Bootstrap the unified vagrant-scopes bare repo + matching data dirs."""
    from awm.persistence.config_service import get_vagrant_scopes_repo_url

    bare_dir = PROJECTS_DIR / VAGRANT_PROJECT / ".bare"
    project_dir = bare_dir.parent
    project_dir.mkdir(parents=True, exist_ok=True)

    bare_exists = bare_dir.exists()
    if not bare_exists:
        r = run_git(["git", "init", "--bare", str(bare_dir)])
        if r.returncode != 0:
            raise RuntimeError(f"git init --bare failed: {r.stderr}")

    repo_url = get_vagrant_scopes_repo_url()

    if shutil.which("gh"):
        repo_slug = f"{GITHUB_USER}/vagrant-scopes"
        view = run_git(["gh", "repo", "view", repo_slug])
        if view.returncode != 0:
            create = run_git(["gh", "repo", "create", repo_slug, "--private"])
            if create.returncode != 0 and "already exists" not in (create.stderr or ""):
                raise RuntimeError(f"gh repo create failed: {create.stderr}")

    existing = run_git(["git", "-C", str(bare_dir), "remote", "get-url", "origin"])
    if existing.returncode != 0:
        run_git(["git", "-C", str(bare_dir), "remote", "add", "origin", repo_url])
    elif (existing.stdout or "").strip() != repo_url:
        run_git(["git", "-C", str(bare_dir), "remote", "set-url", "origin", repo_url])

    has_main = run_git(["git", "-C", str(bare_dir), "rev-parse", "--verify",
                        "refs/heads/main"])
    if has_main.returncode != 0:
        with tempfile.TemporaryDirectory() as tmp:
            init_dir = Path(tmp) / "init"
            run_git(["git", "clone", str(bare_dir), str(init_dir)])
            run_git(["git", "-C", str(init_dir), "checkout", "-b", "main"])
            run_git(["git", "-C", str(init_dir), "commit", "--allow-empty",
                     "-m", "Initial commit for vagrant-scopes"])
            run_git(["git", "-C", str(init_dir), "push", "origin", "main"])
            if shutil.which("gh"):
                run_git(["git", "-C", str(bare_dir), "push", "origin", "main"])

    run_git(["git", "-C", str(bare_dir), "symbolic-ref", "HEAD", "refs/heads/main"])

    for d in [
        DATA_DIR / VAGRANT_PROJECT / "raw",
        DATA_DIR / VAGRANT_PROJECT / "staged",
    ]:
        d.mkdir(parents=True, exist_ok=True)

    dao = ScopesDAO()
    with dao.transaction() as conn:
        _ensure_project_row(VAGRANT_PROJECT, conn=conn)

    return bare_dir


def _vagrant_scope_name(user_as: str) -> str:
    import re
    bare = user_as.removeprefix("user:") if user_as.startswith("user:") else user_as
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", bare).strip("-")
    return f"{safe}-handler" if safe else "anon-handler"


def vagrant_scope_identifier(user_as: str) -> str:
    return f"{VAGRANT_PROJECT}/{_vagrant_scope_name(user_as)}"


def ensure_vagrant_session(user_as: str) -> tuple[str, str]:
    """Ensure a vagrant scope exists for ``user_as`` and the requesting user is
    subscribed to its channel. A scope IS the channel, so there is no separate
    room. Returns ``(agent_id, scope_key)`` where ``scope_key`` is
    ``'<project>/<scope>'``."""
    scope_name = _vagrant_scope_name(user_as)
    bare_dir = PROJECTS_DIR / VAGRANT_PROJECT / ".bare"
    if not bare_dir.exists():
        raise FileNotFoundError(
            f"Vagrant-scopes repo not initialized at {bare_dir}. "
            f"Run `awm vagrant-init` first."
        )

    agent_id = agent_id_for_scope(VAGRANT_PROJECT, scope_name)
    if agent_id is None:
        create_scope(ScopeCreateRequest(project=VAGRANT_PROJECT, scope=scope_name))
        agent_id = agent_id_for_scope(VAGRANT_PROJECT, scope_name)

    # Subscribe the requesting user to the scope's channel (idempotent).
    try:
        from awm.gatewayclient import call_sync
        call_sync("journal", "subscribe",
                  {"project": VAGRANT_PROJECT, "scope": scope_name, "guest": user_as})
    except Exception:
        pass
    return agent_id, f"{VAGRANT_PROJECT}/{scope_name}"


_CONTEXT_IMPORT_LINE = "@.awm/context.md"


def _write_scope_opencode_config(awm_dir: Path) -> None:
    import json
    out: dict
    workspace_cfg = WORKSPACE_ROOT / ".awm" / "mcp-opencode.json"
    if workspace_cfg.is_file():
        try:
            out = json.loads(workspace_cfg.read_text())
        except (OSError, json.JSONDecodeError):
            out = {"$schema": "https://opencode.ai/config.json", "mcp": {}}
    else:
        out = {"$schema": "https://opencode.ai/config.json", "mcp": {}}
    instructions: list[str] = []
    workspace_md = WORKSPACE_ROOT / "AGENTS.md"
    if workspace_md.is_file():
        instructions.append(str(workspace_md))
    instructions.append(".awm/context.md")
    out["instructions"] = instructions
    awm_dir.mkdir(parents=True, exist_ok=True)
    (awm_dir / "mcp-opencode.json").write_text(json.dumps(out, indent=2) + "\n")


def _is_tracked(worktree_dir: Path, path: str) -> bool:
    res = subprocess.run(
        ["git", "-C", str(worktree_dir), "ls-files", "--error-unmatch", path],
        capture_output=True,
    )
    return res.returncode == 0


def _strip_context_import(text: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    changed = False
    while lines and lines[-1].strip() == _CONTEXT_IMPORT_LINE:
        lines.pop()
        changed = True
        if lines and lines[-1].strip() == "":
            lines.pop()
    if not changed:
        return text, False
    new = "".join(lines)
    if new and not new.endswith("\n"):
        new += "\n"
    return new, True


def _heal_worktree(worktree_dir: Path, *, project: str, scope: str, dry_run: bool) -> dict:
    actions: dict[str, str | None] = {
        "import_line": None, "agents_md": None,
        "claude_md": None, "context_md": None,
        "opencode_config": None, "data": None,
    }

    agents_path = worktree_dir / "AGENTS.md"
    if agents_path.is_file() and not agents_path.is_symlink():
        if _is_tracked(worktree_dir, "AGENTS.md"):
            body = agents_path.read_text()
            new_body, changed = _strip_context_import(body)
            if changed:
                actions["import_line"] = "stripped"
                if not dry_run:
                    agents_path.write_text(new_body)
        else:
            actions["agents_md"] = "deleted:untracked"
            if not dry_run:
                agents_path.unlink()

    claude_path = worktree_dir / "CLAUDE.md"
    if claude_path.is_symlink():
        try:
            target = os.readlink(str(claude_path))
        except OSError:
            target = ""
        if target == "AGENTS.md":
            actions["claude_md"] = "deleted:symlink"
            if not dry_run:
                claude_path.unlink()
        elif not _is_tracked(worktree_dir, "CLAUDE.md"):
            actions["claude_md"] = "deleted:untracked-symlink"
            if not dry_run:
                claude_path.unlink()
    elif claude_path.exists():
        if not _is_tracked(worktree_dir, "CLAUDE.md"):
            actions["claude_md"] = "deleted:untracked"
            if not dry_run:
                claude_path.unlink()

    awm_dir = worktree_dir / ".awm"
    context_path = awm_dir / "context.md"
    if not context_path.exists():
        actions["context_md"] = "created"
        if not dry_run:
            awm_dir.mkdir(parents=True, exist_ok=True)
            context_path.write_text(_default_context(project, scope))

    opencode_cfg = awm_dir / "mcp-opencode.json"
    existing = opencode_cfg.read_text() if opencode_cfg.is_file() else None
    if not dry_run:
        awm_dir.mkdir(parents=True, exist_ok=True)
        _write_scope_opencode_config(awm_dir)
        new = opencode_cfg.read_text()
        if existing is None:
            actions["opencode_config"] = "created"
        elif new != existing:
            actions["opencode_config"] = "rewritten"
    else:
        import json as _json
        workspace_cfg = WORKSPACE_ROOT / ".awm" / "mcp-opencode.json"
        if workspace_cfg.is_file():
            try:
                preview = _json.loads(workspace_cfg.read_text())
            except (OSError, _json.JSONDecodeError):
                preview = {"$schema": "https://opencode.ai/config.json", "mcp": {}}
        else:
            preview = {"$schema": "https://opencode.ai/config.json", "mcp": {}}
        instr: list[str] = []
        wsmd = WORKSPACE_ROOT / "AGENTS.md"
        if wsmd.is_file():
            instr.append(str(wsmd))
        instr.append(".awm/context.md")
        preview["instructions"] = instr
        would = _json.dumps(preview, indent=2) + "\n"
        if existing is None:
            actions["opencode_config"] = "would-create"
        elif would != existing:
            actions["opencode_config"] = "would-rewrite"

    # Data view. Idempotent by construction, which is what makes heal the
    # migration path for scopes that pre-date this layer: one still on the
    # legacy shared symlink is brought to whatever its checkout now says the
    # first time heal runs over it.
    actions["data"] = _heal_data(awm_dir, project=project, scope=scope, dry_run=dry_run)

    return actions


# `.awm/data` shapes heal has to recognise. Named explicitly rather than guessed
# at with a chain of `if exists`, because the transition heal reports is only
# meaningful if the starting point was identified rather than assumed.
def _classify_data_path(dest: Path) -> str:
    if dest.is_symlink():
        target = os.readlink(str(dest))
        return "compat-symlink" if target in ("../data", "..\\data") else "legacy-symlink"
    if not dest.exists():
        return "absent"
    return "plain-dir"


def _heal_data(awm_dir: Path, *, project: str, scope: str, dry_run: bool) -> str | None:
    """Bring ``.awm/data`` to whatever this scope's checkout now implies.

    Three shapes are live at once — a legacy shared symlink, the compat symlink,
    and nothing at all — so this reports the transition it made rather than
    assuming a starting point.
    """
    dest = awm_dir / "data"
    before = _classify_data_path(dest)
    wants_dvc = data_dvc.is_dvc_project(awm_dir.parent)

    if dry_run:
        if wants_dvc and before != "compat-symlink":
            return f"would-convert:{before}->dvc"
        if not wants_dvc and before == "absent":
            return "would-symlink"
        return None

    report = data_dvc.provision_scope_data(project, scope, awm_dir)
    mode = report.get("mode")
    if mode == "unknown":
        return f"error:{report.get('detail', '')[:120]}"
    after = _classify_data_path(dest)
    if after == before:
        return None
    return f"{before}->{after}"


def _resolve_worktree(project: str, scope: str, recorded: str | None) -> Path:
    """A scope's worktree as an absolute path, from a possibly-legacy DB row.

    Two shapes in the ``agents`` table are hazardous read literally. An empty
    ``worktree`` becomes ``Path('.')`` — which *exists*, so an existence check
    accepts it and every caller then operates on whatever directory the process
    is standing in. A workspace-relative value resolves the same way.

    Both fall back to the conventional ``projects/<project>/<scope>``, which is
    where ``git worktree`` actually put them. Note the distinction that matters:
    the fallback is an absolute path derived from the scope's own identity, so
    it is wrong-or-missing but never *someone else's directory*.
    """
    raw = (recorded or "").strip()
    if not raw:
        return PROJECTS_DIR / project / scope
    worktree = Path(raw)
    return worktree if worktree.is_absolute() else PROJECTS_DIR.parent / worktree


def heal_scopes(project: str | None = None, dry_run: bool = False) -> list[dict]:
    dao = ScopesDAO()
    sql = (
        "SELECT p.name AS project, a.scope, a.worktree "
        "FROM agents a JOIN projects p ON p.id = a.project_id "
        "WHERE a.status IN ('allocated','active')"
    )
    params: list = []
    if project:
        sql += " AND p.name=?"
        params.append(project)
    rows = dao.query_all(sql, params)

    report: list[dict] = []
    for row in rows:
        # Legacy rows carry two shapes that a bare Path() turns into a live
        # hazard: an empty string, which becomes Path('.') and therefore
        # *exists* — so an existence check alone waves it through and heal then
        # operates on the process's current directory — and a workspace-
        # relative path, which resolves against wherever the operator happened
        # to be standing. Anchor the relative form on the workspace root and
        # reject anything that still isn't a real directory.
        worktree = _resolve_worktree(row["project"], row["scope"], row["worktree"])
        if not worktree.is_dir():
            report.append({
                "project": row["project"], "scope": row["scope"],
                "worktree": str(worktree), "ok": False,
                "error": f"worktree missing: {worktree}",
            })
            continue
        try:
            actions = _heal_worktree(
                worktree, project=row["project"], scope=row["scope"],
                dry_run=dry_run,
            )
            report.append({
                "project": row["project"], "scope": row["scope"],
                "worktree": str(worktree), "ok": True,
                "dry_run": dry_run, "actions": actions,
            })
        except OSError as exc:
            report.append({
                "project": row["project"], "scope": row["scope"],
                "worktree": str(worktree), "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
    return report


def _ensure_awm_gitignored(repo_dir: Path) -> None:
    """Make ``.awm/`` invisible to git in ``repo_dir`` without touching any
    tracked file.

    awm's own project repos gitignore ``.awm/`` already, but a freshly cloned
    third-party repo (e.g. one cloned by ``scope_create`` with ``new_project``) does not — so the
    scaffolded ``.awm/`` would show up as untracked and leave the worktree
    "dirty". Appending ``.awm/`` to the repo's common ``info/exclude`` (shared
    by every worktree, never committed) fixes that cleanly. No-op when git
    already ignores ``.awm``.
    """
    check = run_git(["git", "-C", str(repo_dir), "check-ignore", "-q", ".awm"])
    if check.returncode == 0:
        return  # already ignored — tracked .gitignore or a prior exclude entry
    cd = run_git(["git", "-C", str(repo_dir), "rev-parse", "--git-common-dir"])
    if cd.returncode != 0:
        return  # not a git worktree we can reason about; leave it alone
    common = Path(cd.stdout.strip())
    if not common.is_absolute():
        common = (repo_dir / common).resolve()
    info = common / "info"
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    prior = exclude.read_text() if exclude.exists() else ""
    if ".awm/" in prior.split():
        return
    with exclude.open("a") as fh:
        if prior and not prior.endswith("\n"):
            fh.write("\n")
        fh.write(".awm/\n")


def _scaffold_awm_dir(
    project: str,
    scope: str,
    repo_dir: Path,
    *,
    branch: str,
    context: str | None = None,
    is_vagrant: bool = False,
) -> str:
    """Scaffold a worktree's ``.awm/`` metadata + agents DB row.

    Shared by :func:`create_scope` and :func:`create_project` so a project's
    initial default-branch worktree gets the exact same treatment as any
    scope. Idempotent: symlinks are unlink-before-relink and the agents row is
    get-or-create (``ensure_agent``), so a repair path can safely re-run it.
    Returns the ``context.md`` content that was written.
    """
    awm_dir = _get_awm_dir(repo_dir)
    awm_dir.mkdir(parents=True, exist_ok=True)
    _ensure_awm_gitignored(repo_dir)

    # The scope's data view. For a DVC-backed project this is wiring plus a
    # checkout of what the branch already pins — the base branch threaded into
    # `worktree add` above carries the data with it, which is the bug that
    # started this whole exercise, fixed by construction rather than by a
    # second code path. Unconverted projects keep the legacy shared symlink.
    data_report = data_dvc.provision_scope_data(project, scope, awm_dir)
    if data_report.get("mode") == "unknown":
        log.warning("scope %s/%s: %s", project, scope, data_report.get("detail"))

    skills_link = awm_dir / "skills"
    if skills_link.is_symlink() or skills_link.exists():
        skills_link.unlink()
    skills_link.symlink_to(SKILLS_DIR)

    context_content = context or _default_context(project, scope)
    (awm_dir / "context.md").write_text(context_content)
    try:
        (awm_dir / "history.md").write_text(_generate_history_md(project, scope))
    except Exception as exc:  # noqa: BLE001 — a scope is still usable; `journal render` renders it later
        log.warning("scope %s/%s: history.md not rendered: %s", project, scope, exc)
    _write_scope_opencode_config(awm_dir)

    dao = ScopesDAO()
    with dao.transaction() as conn:
        # Ensure the project row exists first — ensure_agent requires it, and
        # create_project reaches here before any scope has created that row.
        _ensure_project_row(project, conn=conn)
        ensure_agent(
            project, scope,
            branch=branch,
            worktree=str(repo_dir),
            agent_cli="claude",
            status="allocated",
            is_vagrant=is_vagrant,
            conn=conn,
        )

    search_index.index_scope(project, scope)

    return context_content


def create_scope(req: ScopeCreateRequest) -> ScopeActionResponse:
    """Create a new scope: git worktree + .awm/ metadata + agents row.

    With ``new_project`` the project is created first when it does not exist,
    and ``scope`` may then be omitted to create the project alone.
    """
    validate_name(req.project, kind="project name")
    if req.scope is None:
        if not req.new_project:
            raise ValueError("scope is required; omit it only with new_project=true, "
                             "to create the project alone")
    else:
        validate_scope_name(req.scope)
    bare_dir = PROJECTS_DIR / req.project / ".bare"
    if not bare_dir.exists():
        if req.project == VAGRANT_PROJECT:
            raise FileNotFoundError(
                f"Vagrant-scopes repo not initialized at {bare_dir}. "
                f"Run `awm vagrant-init` first."
            )
        if not req.new_project:
            raise FileNotFoundError(
                f"Project '{req.project}' not found (expected {bare_dir}). "
                f"Pass new_project=true to create it."
            )
        from awm.scopes import projects
        created = projects.create_project(ProjectCreateRequest(
            name=req.project, clone_url=req.clone_url, fork_url=req.fork_url))
        if req.scope is None:
            main = Path(created.worktree_dir)
            return ScopeActionResponse(
                project=req.project, scope=main.name, status="active", session=1,
                message=f"Created project at projects/{req.project} "
                        f"(main checkout {main.name}, .awm/ initialized)")
    elif req.scope is None:
        raise FileExistsError(f"Project '{req.project}' already exists at {bare_dir}")

    from_branch = req.from_branch or detect_default_branch(bare_dir)
    repo_dir = PROJECTS_DIR / req.project / req.scope
    feature_branch = req.branch_name or f"feat/{req.scope}"

    dao = ScopesDAO()
    with dao.transaction() as conn:
        _ensure_project_row(req.project, conn=conn)
        prior = ScopesDAO(conn=conn).query_one(
            "SELECT COUNT(*) AS n FROM agents a "
            "JOIN projects p ON p.id = a.project_id "
            "WHERE p.name=? AND a.scope=?",
            (req.project, req.scope),
        )
        session_num = (prior["n"] or 0) + 1 if prior else 1
        active = agent_id_for_scope(req.project, req.scope, conn=conn, active_only=True)
        if active:
            raise FileExistsError(
                f"Scope '{req.scope}' already has an active session in project '{req.project}'"
            )

    # Before anything destructive: the stale-directory cleanup below removes a
    # tree, and a request that was never going to succeed must not get that far.
    _assert_branch_available(bare_dir, feature_branch)

    if repo_dir.exists():
        _cleanup_worktree(bare_dir, repo_dir, feature_branch,
                          project=req.project, scope=req.scope)

    r = run_git(["git", "-C", str(bare_dir), "worktree", "add",
                 str(repo_dir), "-b", feature_branch, from_branch])
    if r.returncode != 0:
        raise RuntimeError(f"Failed to create worktree: {r.stderr}")

    _scaffold_awm_dir(
        req.project, req.scope, repo_dir,
        branch=feature_branch,
        context=req.context,
        is_vagrant=(req.project == VAGRANT_PROJECT),
    )

    return ScopeActionResponse(
        project=req.project,
        scope=req.scope,
        status="active",
        session=session_num,
        message=(
            f"Created scope at projects/{req.project}/{req.scope} (.awm/ initialized) "
            f"on branch {feature_branch}, session {session_num}"
        ),
    )


def repair_scope(project: str, scope: str) -> ScopeActionResponse:
    """Reconcile an on-disk worktree+.awm/ with a missing DB row."""
    validate_name(project, kind="project name")
    validate_scope_name(scope)

    bare_dir = PROJECTS_DIR / project / ".bare"
    if not bare_dir.exists():
        raise FileNotFoundError(
            f"Project '{project}' has no bare repo at {bare_dir} — nothing to repair against"
        )

    repo_dir = PROJECTS_DIR / project / scope
    if not repo_dir.is_dir():
        raise FileNotFoundError(f"Worktree not found at {repo_dir}")
    awm_dir = _get_awm_dir(repo_dir)
    context_path = awm_dir / "context.md"
    if not context_path.exists():
        raise FileNotFoundError(
            f"No .awm/context.md at {context_path} — refusing to repair "
            f"a worktree that wasn't initialized by scope_create"
        )

    r = run_git(["git", "-C", str(repo_dir), "branch", "--show-current"])
    branch = (r.stdout or "").strip()
    if r.returncode != 0 or not branch:
        raise RuntimeError(
            f"Could not read branch from worktree {repo_dir}: {r.stderr or 'empty branch name'}"
        )

    dao = ScopesDAO()
    with dao.transaction() as conn:
        _ensure_project_row(project, conn=conn)
        existing = agent_id_for_scope(project, scope, conn=conn, active_only=True)
        if existing:
            return ScopeActionResponse(
                project=project,
                scope=scope,
                status="skipped",
                message=(
                    f"Scope {project}/{scope} already has an active agent row "
                    f"({existing}); nothing to repair"
                ),
            )
        aid = ensure_agent(
            project, scope,
            branch=branch,
            worktree=str(repo_dir),
            agent_cli="claude",
            status="allocated",
            is_vagrant=(project == VAGRANT_PROJECT),
            conn=conn,
        )

    search_index.index_scope(project, scope)

    return ScopeActionResponse(
        project=project,
        scope=scope,
        status="repaired",
        message=(
            f"Backfilled agents row for {project}/{scope} from on-disk worktree "
            f"({repo_dir}) on branch {branch}, agent_id={aid}"
        ),
    )


def update_scope(project: str, scope: str, req: ScopeUpdateRequest) -> ScopeActionResponse:
    """Complete a scope (mark agent retired). Optionally merges + cleans up."""
    validate_name(project, kind="project name")
    validate_scope_name(scope)
    bare_dir = PROJECTS_DIR / project / ".bare"
    repo_dir = PROJECTS_DIR / project / scope

    if not bare_dir.exists():
        raise FileNotFoundError(f"Bare repository not found at {bare_dir}")

    feature_branch = _scope_branch(project, scope, repo_dir)

    if req.action != "complete":
        raise ValueError(f"Unknown action: {req.action}. Only 'complete' is supported.")

    aid = agent_id_for_scope(project, scope, active_only=True)
    if aid:
        retire_agent(aid)

    merge_msg = ""
    if req.merge:
        default_branch = detect_default_branch(bare_dir)
        main_worktree = PROJECTS_DIR / project / default_branch
        if not main_worktree.exists():
            raise FileNotFoundError(f"Main worktree not found at {main_worktree}")
        run_git(["git", "-C", str(main_worktree), "checkout", default_branch])
        r = run_git(["git", "-C", str(main_worktree), "merge", feature_branch,
                     "-m", f"Merge {feature_branch} into {default_branch}"])
        if r.returncode != 0:
            raise RuntimeError(f"Merge failed: {r.stderr}")
        run_git(["git", "-C", str(main_worktree), "push"])
        merge_msg = f", merged into {default_branch}"

    if req.cleanup:
        _cleanup_worktree(bare_dir, repo_dir, feature_branch,
                          project=project, scope=scope, force=req.force)

    return ScopeActionResponse(
        project=project, scope=scope, status="completed",
        message=f"Scope completed{merge_msg}",
    )


def sync_scope(project: str, scope: str, req: ScopeSyncRequest) -> ScopeActionResponse:
    validate_name(project, kind="project name")
    validate_scope_name(scope)
    bare_dir = PROJECTS_DIR / project / ".bare"
    repo_dir = PROJECTS_DIR / project / scope
    feature_branch = _scope_branch(project, scope, repo_dir)

    if not bare_dir.exists():
        raise FileNotFoundError(f"Bare repository not found at {bare_dir}")
    if not repo_dir.exists():
        raise FileNotFoundError(f"Worktree not found at {repo_dir}")

    base = req.from_branch or detect_default_branch(bare_dir)

    aid = agent_id_for_scope(project, scope, active_only=True)
    if aid is None:
        raise FileNotFoundError(f"No active scope '{scope}' found in project '{project}'")

    r = run_git(["git", "-C", str(repo_dir), "status", "--porcelain"])
    if r.returncode != 0:
        raise RuntimeError(f"git status failed: {r.stderr}")
    if r.stdout.strip():
        raise RuntimeError(
            f"Worktree has uncommitted changes — refusing to sync:\n{r.stdout}"
        )

    r = run_git(["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"])
    if r.returncode != 0:
        raise RuntimeError(f"git rev-parse failed: {r.stderr}")
    current = r.stdout.strip()
    if current != feature_branch:
        raise RuntimeError(
            f"Worktree HEAD is on '{current}', expected '{feature_branch}'. "
            f"Check out the feature branch before syncing."
        )

    if req.strategy == "merge":
        r = run_git([
            "git", "-C", str(repo_dir), "merge", base,
            "-m", f"Sync {base} into {feature_branch}",
        ])
    else:
        r = run_git(["git", "-C", str(repo_dir), "rebase", base])

    if r.returncode != 0:
        raise RuntimeError(
            f"{req.strategy} failed: {r.stderr or r.stdout}\n"
            f"Worktree left mid-{req.strategy}; resolve and re-run, "
            f"or `git {req.strategy} --abort` to undo."
        )

    return ScopeActionResponse(
        project=project,
        scope=scope,
        status="active",
        message=f"Synced {feature_branch} with {base} via {req.strategy}",
    )


def _has_merge_in_progress(worktree: Path) -> bool:
    """True iff a merge is mid-flight (MERGE_HEAD present) in ``worktree``."""
    r = run_git(["git", "-C", str(worktree), "rev-parse", "--verify", "-q", "MERGE_HEAD"])
    return r.returncode == 0


def _merge_one(worktree: Path, scope: str, branch: str, source_ref: str,
               message: str) -> dict:
    """Merge ``source_ref`` into the branch checked out at ``worktree``.

    Batch-safe: on a conflict the merge is rolled back with ``git merge
    --abort`` (only when a merge is actually in progress, so an "Already up to
    date" / fast-forward is never misclassified) and the worktree is left
    clean. Returns ``{scope, branch, result, detail}`` with ``result`` ∈
    ``merged | up_to_date | conflict | error``.
    """
    r = run_git(["git", "-C", str(worktree), "merge", source_ref, "-m", message])
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        if "Already up to date" in out:
            return {"scope": scope, "branch": branch, "result": "up_to_date",
                    "detail": "already up to date"}
        return {"scope": scope, "branch": branch, "result": "merged",
                "detail": out.strip().splitlines()[-1] if out.strip() else "merged"}
    # Non-zero: a real conflict (or other merge failure). Roll back only when a
    # merge is genuinely in progress, else we'd misreport a no-op/FF as a failed
    # abort and could leave a stray MERGE_HEAD (see MERGE_HEAD-lingers footgun).
    if _has_merge_in_progress(worktree):
        run_git(["git", "-C", str(worktree), "merge", "--abort"])
        return {"scope": scope, "branch": branch, "result": "conflict",
                "detail": "merge conflict — aborted, worktree left clean"}
    return {"scope": scope, "branch": branch, "result": "error",
            "detail": (r.stderr or out).strip()}


def _hub_record(project: str, hub: str, bare_dir: Path) -> dict:
    """Resolve the hub scope's DB row (branch + worktree). Falls back to
    ``feat/<hub>`` / ``PROJECTS_DIR/project/hub`` if no row exists — but a hub
    is normally a real scope, so the row is the authoritative source for legacy
    flat branches like ``dev`` (branch ``dev``, not ``feat/dev``)."""
    rec = agent_record_for_scope(project, hub, active_only=False)
    if rec is not None:
        return {"branch": rec["branch"], "worktree": Path(rec["worktree"])}
    return {"branch": f"feat/{hub}", "worktree": PROJECTS_DIR / project / hub}


def _peripheral_record(project: str, p: str) -> dict:
    """Resolve a peripheral scope's branch + worktree (DB row, else default)."""
    rec = agent_record_for_scope(project, p, active_only=False)
    if rec is not None:
        return {"branch": rec["branch"], "worktree": Path(rec["worktree"])}
    return {"branch": f"feat/{p}", "worktree": PROJECTS_DIR / project / p}


def _summarize(results: list[dict]) -> dict:
    summary: dict[str, int] = {}
    for r in results:
        summary[r["result"]] = summary.get(r["result"], 0) + 1
    return summary


def gather_scope(project: str, hub: str, peripherals: list[str],
                 strategy: str = "merge") -> ScatterGatherResponse:
    """Fan-in: merge each peripheral's branch into the hub's branch.

    Runs in the hub's worktree. The hub worktree must be clean and on the hub
    branch (the whole fan-in needs a clean hub). A per-peripheral conflict is
    aborted and reported; the batch continues. Local-only — no push.

    There is no separate data leg, and its absence is the point: a peripheral's
    data pins ride its code branch, so **merging the branch merges the data**
    and the post-merge hook materialises it. One batch, still local-only.
    """
    validate_name(project, kind="project name")
    validate_scope_name(hub)
    if strategy != "merge":
        raise ValueError("gather only supports strategy='merge' (rebase is not "
                         "meaningful for a shared hub).")
    bare_dir = PROJECTS_DIR / project / ".bare"
    if not bare_dir.exists():
        raise FileNotFoundError(f"Bare repository not found at {bare_dir}")

    hub_rec = _hub_record(project, hub, bare_dir)
    hub_branch = hub_rec["branch"]
    hub_worktree = hub_rec["worktree"]
    if not hub_worktree.exists():
        raise FileNotFoundError(f"Hub worktree not found at {hub_worktree}")

    r = run_git(["git", "-C", str(hub_worktree), "status", "--porcelain"])
    if r.returncode != 0:
        raise RuntimeError(f"git status failed in hub worktree: {r.stderr}")
    if r.stdout.strip():
        raise RuntimeError(
            f"Hub worktree {hub_worktree} has uncommitted changes — refusing to "
            f"gather:\n{r.stdout}"
        )
    r = run_git(["git", "-C", str(hub_worktree), "rev-parse", "--abbrev-ref", "HEAD"])
    current = (r.stdout or "").strip()
    if current != hub_branch:
        raise RuntimeError(
            f"Hub worktree HEAD is on '{current}', expected hub branch "
            f"'{hub_branch}'. Check out the hub branch before gathering."
        )

    results: list[dict] = []
    for p in peripherals:
        validate_scope_name(p)
        prec = _peripheral_record(project, p)
        p_branch = prec["branch"]
        verify = run_git(["git", "-C", str(bare_dir), "rev-parse", "--verify",
                          "-q", f"refs/heads/{p_branch}"])
        if verify.returncode != 0:
            results.append({"scope": p, "branch": p_branch, "result": "skipped",
                            "detail": f"branch {p_branch} not found"})
            continue
        results.append(_merge_one(
            hub_worktree, p, p_branch, p_branch,
            f"Gather {p_branch} into {hub_branch}",
        ))

    return ScatterGatherResponse(
        project=project, hub=hub, hub_branch=hub_branch, direction="gather",
        results=results, summary=_summarize(results),
        data_results=None, data_summary=None,
    )


def scatter_scope(project: str, hub: str, peripherals: list[str],
                  strategy: str = "merge") -> ScatterGatherResponse:
    """Fan-out: merge the hub's branch into each peripheral's branch.

    Each merge runs in that peripheral's own worktree. A dirty or off-branch
    peripheral is skipped (one dirty sibling must not abort the batch); a
    conflict is aborted and reported. Local-only — no push.

    As with :func:`gather_scope` there is no separate data leg any more: the
    hub's data pins ride its branch, so the same merge carries them, and each
    peripheral's post-merge hook materialises what it mounts.
    """
    validate_name(project, kind="project name")
    validate_scope_name(hub)
    if strategy != "merge":
        raise ValueError("scatter only supports strategy='merge' (rebase is not "
                         "meaningful for fan-out).")
    bare_dir = PROJECTS_DIR / project / ".bare"
    if not bare_dir.exists():
        raise FileNotFoundError(f"Bare repository not found at {bare_dir}")

    hub_rec = _hub_record(project, hub, bare_dir)
    hub_branch = hub_rec["branch"]
    verify = run_git(["git", "-C", str(bare_dir), "rev-parse", "--verify",
                      "-q", f"refs/heads/{hub_branch}"])
    if verify.returncode != 0:
        raise FileNotFoundError(f"Hub branch '{hub_branch}' not found")

    results: list[dict] = []
    for p in peripherals:
        validate_scope_name(p)
        prec = _peripheral_record(project, p)
        p_branch = prec["branch"]
        p_worktree = prec["worktree"]
        if not p_worktree.exists():
            results.append({"scope": p, "branch": p_branch, "result": "skipped",
                            "detail": f"worktree not found at {p_worktree}"})
            continue
        r = run_git(["git", "-C", str(p_worktree), "status", "--porcelain"])
        if r.returncode != 0 or r.stdout.strip():
            results.append({"scope": p, "branch": p_branch, "result": "skipped",
                            "detail": "worktree dirty — skipped"})
            continue
        r = run_git(["git", "-C", str(p_worktree), "rev-parse", "--abbrev-ref", "HEAD"])
        current = (r.stdout or "").strip()
        if current != p_branch:
            results.append({"scope": p, "branch": p_branch, "result": "skipped",
                            "detail": f"HEAD on '{current}', expected '{p_branch}'"})
            continue
        results.append(_merge_one(
            p_worktree, p, p_branch, hub_branch,
            f"Scatter {hub_branch} into {p_branch}",
        ))

    return ScatterGatherResponse(
        project=project, hub=hub, hub_branch=hub_branch, direction="scatter",
        results=results, summary=_summarize(results),
        data_results=None, data_summary=None,
    )


def delete_scope(project: str, scope: str, force: bool = False) -> ScopeActionResponse:
    validate_name(project, kind="project name")
    validate_scope_name(scope)
    bare_dir = PROJECTS_DIR / project / ".bare"
    repo_dir = PROJECTS_DIR / project / scope
    feature_branch = _scope_branch(project, scope, repo_dir)

    if not bare_dir.exists():
        raise FileNotFoundError(f"Bare repository not found at {bare_dir}")

    dao = ScopesDAO()
    aid_row = dao.query_one(
        "SELECT a.id, a.status FROM agents a "
        "JOIN projects p ON p.id = a.project_id "
        "WHERE p.name=? AND a.scope=? "
        "AND a.status IN ('allocated','active','retired') "
        "ORDER BY a.created_at DESC LIMIT 1",
        (project, scope),
    )
    if aid_row is None:
        raise FileNotFoundError(f"No scope '{scope}' found in project '{project}'")

    _cleanup_worktree(bare_dir, repo_dir, feature_branch,
                      project=project, scope=scope, force=force)

    with dao.transaction() as conn:
        ScopesDAO(conn=conn).execute(
            "UPDATE agents SET status='retired', retired_at=? WHERE id=?",
            (now_ms(), aid_row["id"]),
        )

    search_index.index_scope(project, scope)

    return ScopeActionResponse(
        project=project, scope=scope, status="deleted",
        message="Scope deleted, worktree and branch cleaned up",
    )


def _v37_render_scope(r, session: int = 1) -> ScopeInfo:
    status_render = (
        "active" if r["status"] in ("allocated", "active")
        else ("completed" if r["status"] == "retired" else r["status"])
    )
    return ScopeInfo(
        project=r["project_name"], scope=r["scope"], status=status_render,
        branch=r["branch"], worktree=r["worktree"],
        repo_path=r["worktree"], session=session,
    )


_SCOPE_ROW_SQL = (
    "SELECT a.id, a.scope, a.status, a.branch, a.worktree, "
    "       a.created_at, p.name AS project_name, "
    "       (SELECT COUNT(*) FROM agents a2 "
    "        JOIN projects p2 ON p2.id = a2.project_id "
    "        WHERE p2.name = p.name AND a2.scope = a.scope "
    "        AND a2.created_at <= a.created_at) AS session "
    "FROM agents a JOIN projects p ON p.id = a.project_id "
    "WHERE 1=1"
)


def _head_branch(bare: Path) -> str | None:
    """The branch the bare repo's HEAD names, read from the file without spawning git."""
    try:
        head = (bare / "HEAD").read_text().strip()
    except OSError:
        return None
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref: refs/heads/") else None


def main_checkouts(project: str | None = None) -> dict[str, ScopeInfo]:
    """Each project's main checkout, keyed by project, where no agents row names it.

    The main checkout is the worktree of the bare repo's default branch. A
    project made by ``create_project`` has a row for it; one set up by hand has
    none, and without this it would be missing from every listing.
    """
    names = [project] if project else sorted(
        b.parent.name for b in PROJECTS_DIR.glob("*/.bare") if b.is_dir())
    rows = ScopesDAO().query_all(
        "SELECT p.name AS project, a.scope FROM agents a JOIN projects p ON p.id = a.project_id")
    named = {(r["project"], r["scope"]) for r in rows}
    found: dict[str, ScopeInfo] = {}
    for name in names:
        bare = PROJECTS_DIR / name / ".bare"
        if not bare.is_dir():
            continue
        branch = _head_branch(bare)
        if branch is None:
            continue
        worktree = PROJECTS_DIR / name / branch
        if worktree.is_dir() and (name, branch) not in named:
            found[name] = ScopeInfo(project=name, scope=branch, status="active", branch=branch,
                                    worktree=str(worktree), repo_path=str(worktree), session=1)
    return found


def search_scopes(
    query: str | None = None,
    status: str = "active",
    project: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ScopeListResponse:
    """Search scopes. Defaults to status='active'.

    With a ``query``, scopes whose name contains it come first, then the rest
    ranked by relevance of their goals and ``context.md``. A project's main
    checkout counts as an active scope named for its default branch. With no
    ``project`` the reply also lists every project (see
    :func:`awm.scopes.projects.search_projects`).
    """
    where, params = "", []
    if status and status != "all":
        if status == "active":
            where += " AND a.status IN ('allocated','active')"
        elif status == "completed":
            where += " AND a.status='retired'"
        elif status == "deleted":
            return ScopeListResponse(scopes=[], total=0)
        else:
            where += " AND a.status = ?"
            params.append(status)
    if project:
        where += " AND p.name = ?"
        params.append(project)

    dao = ScopesDAO()
    name_sql, name_params = where, list(params)
    if query:
        name_sql += " AND a.scope LIKE ?"
        name_params.append(f"%{query}%")
    rows = dao.query_all(_SCOPE_ROW_SQL + name_sql + " ORDER BY p.name, a.scope", name_params)
    found = [_v37_render_scope(r, session=r["session"] or 1) for r in rows]
    if status in (None, "", "all", "active"):
        found += [m for m in main_checkouts(project).values()
                  if not query or query.lower() in m.scope.lower()]
    merged = sorted(found, key=lambda s: (s.project, s.scope))[offset:offset + limit]
    degraded = None
    if query:
        seen = {f"{s.project}/{s.scope}" for s in merged}
        try:
            res = search_index.search(
                "scope", query, params=params, limit=offset + limit,
                allowed="SELECT p.name || '/' || a.scope FROM agents a"
                        " JOIN projects p ON p.id = a.project_id WHERE 1=1" + where)
            degraded = res.degraded
            for h in res.hits[offset:]:
                if len(merged) >= limit:
                    break
                if h["source_id"] in seen:
                    continue
                proj, scp = h["source_id"].split("/", 1)
                row = dao.query_one(
                    _SCOPE_ROW_SQL + " AND p.name = ? AND a.scope = ?" + where
                    + " ORDER BY a.created_at DESC", [proj, scp, *params])
                if row is not None:
                    merged.append(_v37_render_scope(row, session=row["session"] or 1))
        except Exception as exc:  # noqa: BLE001 — reported, then answered by name match
            degraded = {"semantic": "error", "error": repr(exc)[:300], "fallback": "keyword"}

    listing = None
    if not project:
        from awm.scopes import projects as projects_mod
        listing = projects_mod.search_projects(query=query, limit=limit)
        degraded = degraded or listing.degraded
    return ScopeListResponse(scopes=merged, total=len(merged), degraded=degraded,
                             projects=listing.projects if listing else None)
