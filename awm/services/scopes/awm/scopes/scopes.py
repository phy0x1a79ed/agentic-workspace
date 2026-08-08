"""Scope CRUD — create, complete, delete, list, refresh (v1 modular).

A "scope" is represented by a row in the ``agents`` table
(status ∈ allocated|active|retired) within the scopes service's own DB.
All SQL goes through ScopesDAO.

Cross-service: ``scopes._default_context`` previously called
``awm.services.skills.find_by_name`` — that import is replaced by a
graceful fallback hint (skills service is out-of-process; calling it via
gatewayclient is non-essential for context.md generation).

A scope IS the channel: history.md is rendered from ``scope_posts`` with
``kind='journal'`` (in-process via ``awm.scopes.channel``), and the vagrant
session helper subscribes the user to the scope's channel — there is no
separate rooms/messages/session_logs machinery.
"""

from __future__ import annotations

import logging
import os
import re as _re
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
from awm.scopes import data_dvc
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
    ScopeCreateRequest,
    ScopeUpdateRequest,
    ScopeSyncRequest,
    ScatterGatherResponse,
    ScopeInfo,
    ScopeListResponse,
    ScopeActionResponse,
)
from awm.scopes._validation import validate_name

log = logging.getLogger("awm.scopes")


def _get_awm_dir(repo_dir: Path) -> Path:
    return repo_dir / ".awm"


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
    """
    guard: dict = {"result": "ok", "path": str(worktree_dir)}
    if worktree_dir.is_dir():
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
    meta_dir = bare_dir / "worktrees" / worktree_dir.name
    if meta_dir.exists():
        shutil.rmtree(meta_dir, ignore_errors=True)
    run_git(["git", "-C", str(bare_dir), "branch", "-D", feature_branch])
    return guard


def _default_context(project: str, scope: str) -> str:
    """Generate a default .awm/context.md for a new scope.

    Debrief is a native Claude Code skill (~/.claude/skills/debrief/), so the
    context just names it — no skill-service lookup is needed.
    """
    return (
        f"# {project}/{scope}\n\n"
        f"## Startup\n\n"
        f"1. Run `scope(verb=\"refresh\", args={{project:\"{project}\", scope:\"{scope}\"}})` to update the local history index\n"
        f"2. Read `.awm/history.md` for session history, open issues, and resolved items\n\n"
        f"## Work\n\n"
        f"- Code is in the current directory (this IS the git worktree)\n"
        f"- Project data is at `data/` (`.awm/data` is a symlink to it)\n"
        f"- Reference protocols (git, mamba, etc.) are on disk at `.awm/skills/` if you need them\n"
        f"- Do NOT edit `.awm/history.md` — use MCP tools\n\n"
        f"## Data\n\n"
        f"Data may be **versioned with DVC** "
        f"(`scope_data_status project={project} scope={scope}` says which). When it is, "
        f"the thing to internalise is that **there is only one lever**: a commit records "
        f"your code and the exact data it was built against, together.\n\n"
        f"- `data/<chunk>` holds the files; `data/<chunk>.dvc` is a ~110-byte **pin** "
        f"tracked in this repo. The bytes live once in a workspace-shared cache.\n"
        f"- To save data you wrote: `dvc add data/<chunk>`, then commit the changed "
        f"`.dvc` pin **alongside your code**. That is the whole snapshot-and-publish "
        f"story — there is no separate data verb, no data branch, no promote.\n"
        f"- To take a sibling's data: merge their branch. The pin comes with it and "
        f"the files are checked out for you.\n"
        f"- Materialised files are **read-only** — they are hardlinks to the shared "
        f"cache, so editing in place would corrupt it for every other scope. Write a "
        f"new file, or `dvc unprotect <path>` first.\n"
        f"- **Delete superseded data.** That is the point of versioning: an old version "
        f"stays reachable from the commit that pinned it, so you never need two live "
        f"copies to answer 'which one is current?'.\n"
        f"- You need not hold every chunk on disk — `scope_data_mount` picks which ones "
        f"materialise here. Unmounted chunks stay pinned and backed up regardless.\n"
        f"- Never run a bare `dvc gc`: the cache is shared by every project.\n"
        f"- If data is still the legacy shared directory, none of the above applies — "
        f"it behaves exactly as it always has.\n\n"
        f"## Debrief\n\n"
        f"When the user asks you to debrief (or says \"debrief\"), run the `debrief` skill —\n"
        f"the end-of-session protocol that commits, journals, and refreshes.\n"
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


_TAG_RUN_RE = _re.compile(r"<[^>\n]*>")


def _neutralise_title(text: str) -> str:
    """Make an arbitrary string safe to render as a one-line markdown title.

    A journal post with no ``meta.title`` falls back to its raw body, which can
    contain newlines and — when a tool call was mangled in transport — literal
    tool-call tag fragments (``</invoke>``, ``<meta>…</meta>``). Strip tag-like
    runs and collapse whitespace so neither can smuggle markup or break the line
    in the generated ``history.md``. Presentation-only; the stored row is intact.
    """
    text = _TAG_RUN_RE.sub("", text or "")
    text = _re.sub(r"\s+", " ", text).strip()
    return (text[:80] + "…") if len(text) > 80 else text


def _generate_history_md(project: str, scope: str) -> str:
    from awm.scopes.channel import _coerce_meta

    preface = (
        f"<!-- AUTO-GENERATED by AWM. Do NOT edit this file directly.\n"
        f"     To refresh: `scope(verb=\"refresh\", args={{project:\"{project}\", scope:\"{scope}\"}})`\n"
        f"     To add lessons: `scope_post kind=journal` (see debrief skill)\n"
        f"     To search: `scope_fetch project={project} kind=journal` -->\n\n"
        f"# Project History: {project}\n\n"
    )

    dao = ScopesDAO()
    siblings = dao.query_all(
        "SELECT a.scope FROM agents a "
        "JOIN projects p ON p.id = a.project_id "
        "WHERE p.name=? AND a.scope!=? AND a.status='active'",
        (project, scope),
    )
    # Journal entries are scope_posts with kind='journal' (a scope IS the
    # channel; the debrief is a self-post). Structured fields live in meta.
    journals = dao.query_all(
        "SELECT id, owner_scope, body, meta, ts FROM scope_posts "
        "WHERE owner_project=? AND kind='journal' "
        "ORDER BY ts DESC LIMIT 50",
        (project,),
    )

    def _parse(row):
        meta = _coerce_meta(row["meta"])
        body = row["body"] or ""
        title = _neutralise_title(meta.get("title") or body)
        return meta, title

    sections = []
    if siblings:
        lines = ["## Active Sibling Scopes\n"]
        for s in siblings:
            lines.append(f"- **{s['scope']}**")
        sections.append("\n".join(lines))

    if journals:
        by_skill: dict[str, list] = {}
        for row in journals:
            meta, _ = _parse(row)
            key = meta.get("skill_path") or "(freeform)"
            by_skill.setdefault(key, []).append(row)
        lines = ["## Journal\n"]
        for skill_key, entries in by_skill.items():
            lines.append(f"### Skill: {skill_key}\n")
            for row in entries[:10]:
                meta, title = _parse(row)
                outcome = f" [{meta['outcome']}]" if meta.get("outcome") else ""
                scope_tag = f" ({row['owner_scope']})" if row["owner_scope"] != scope else ""
                lines.append(f"**[{row['id']}] {title}**{outcome}{scope_tag}")
                if meta.get("deviations"):
                    lines.append(f"- Deviations: {meta['deviations']}")
                if meta.get("suggestions"):
                    lines.append(f"- Suggestions: {meta['suggestions']}")
                lines.append("")
        sections.append("\n".join(lines))

    if not sections:
        sections.append(
            "*No journal entries yet. They appear here after agents post them "
            "via `scope_post kind=journal`.*\n"
        )

    return preface + "\n".join(sections)


def refresh_history(project: str, scope: str) -> str:
    validate_name(project, kind="project name")
    validate_name(scope, kind="scope name")
    repo_dir = PROJECTS_DIR / project / scope
    awm_dir = _get_awm_dir(repo_dir)
    content = _generate_history_md(project, scope)
    awm_dir.mkdir(parents=True, exist_ok=True)
    (awm_dir / "history.md").write_text(content)
    # Retired generated files — removed on refresh so existing scopes self-clean.
    (awm_dir / "knowledge.md").unlink(missing_ok=True)
    (awm_dir / "artifacts.md").unlink(missing_ok=True)
    return content


def awm_refresh(project: str, scope: str) -> dict:
    validate_name(project, kind="project name")
    validate_name(scope, kind="scope name")
    h = refresh_history(project, scope)
    return {
        "message": f"Refreshed .awm/ for {project}/{scope}",
        "history_lines": len(h.splitlines()),
    }


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
        from awm.scopes import channel
        channel.subscribe(VAGRANT_PROJECT, scope_name, user_as)
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
    workspace_md = WORKSPACE_ROOT / "WORKSPACE.md"
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
        wsmd = WORKSPACE_ROOT / "WORKSPACE.md"
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
    third-party repo (e.g. via ``project_create --clone``) does not — so the
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
    (awm_dir / "history.md").write_text(_generate_history_md(project, scope))
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

    # Embeddings index
    try:
        from awm.persistence.embeddings import upsert_embedding
        from awm.persistence.databases import get_connection
        text = f"{project}/{scope} {context_content[:500]}"
        conn = get_connection("scopes")
        try:
            upsert_embedding(conn, "scope", f"{project}/{scope}", text[:500])
        finally:
            conn.close()
    except Exception:
        pass

    return context_content


def create_scope(req: ScopeCreateRequest) -> ScopeActionResponse:
    """Create a new scope: git worktree + .awm/ metadata + agents row."""
    validate_name(req.project, kind="project name")
    validate_name(req.scope, kind="scope name")
    bare_dir = PROJECTS_DIR / req.project / ".bare"
    if not bare_dir.exists():
        if req.project == VAGRANT_PROJECT:
            raise FileNotFoundError(
                f"Vagrant-scopes repo not initialized at {bare_dir}. "
                f"Run `awm vagrant-init` first."
            )
        raise FileNotFoundError(f"Project '{req.project}' not found (expected {bare_dir})")

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

    if repo_dir.exists():
        _cleanup_worktree(bare_dir, repo_dir, feature_branch,
                          project=req.project, scope=req.scope)

    r = run_git(["git", "-C", str(bare_dir), "worktree", "add",
                 f"../{req.scope}", "-b", feature_branch, from_branch])
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
    validate_name(scope, kind="scope name")

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

    try:
        from awm.persistence.embeddings import upsert_embedding
        from awm.persistence.databases import get_connection
        text = f"{project}/{scope}"
        conn = get_connection("scopes")
        try:
            upsert_embedding(conn, "scope", f"{project}/{scope}", text)
        finally:
            conn.close()
    except Exception:
        pass

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
    validate_name(scope, kind="scope name")
    bare_dir = PROJECTS_DIR / project / ".bare"
    repo_dir = PROJECTS_DIR / project / scope

    if not bare_dir.exists():
        raise FileNotFoundError(f"Bare repository not found at {bare_dir}")

    feature_branch = f"feat/{scope}"

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
    validate_name(scope, kind="scope name")
    bare_dir = PROJECTS_DIR / project / ".bare"
    repo_dir = PROJECTS_DIR / project / scope
    feature_branch = f"feat/{scope}"

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
    validate_name(hub, kind="scope name")
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
        validate_name(p, kind="scope name")
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
    validate_name(hub, kind="scope name")
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
        validate_name(p, kind="scope name")
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


# ---------------------------------------------------------------------------
# Data surface
# ---------------------------------------------------------------------------

def _scope_worktree(project: str, scope: str) -> Path:
    rec = agent_record_for_scope(project, scope, active_only=False)
    return _resolve_worktree(project, scope, rec["worktree"] if rec else None)


def data_status(project: str, scope: str) -> dict:
    """Report a scope's data view: mode, the commit that pins it, and drift.

    Three verbs remain — this, ``data_mount``, ``data_gc`` — and the shrinkage is
    the result rather than a casualty of it. Snapshotting was ``git commit`` on a
    second history; promoting was a push into a canonical data branch; converting
    a project turned a directory into a repo. With the pin living in the code
    repo, the first two *are* ``git commit`` and ``git merge``, and the third is
    ``dvc add`` on whatever you want tracked. Thin wrappers around git would only
    re-create the two-lever confusion this replaced.
    """
    validate_name(project, kind="project name")
    validate_name(scope, kind="scope name")
    return data_dvc.data_status(project, scope, _scope_worktree(project, scope))


def data_gc(projects: list[str], dry_run: bool = True,
            keep: str = "all-commits") -> dict:
    """Reclaim cache objects no listed project references. Dry by default.

    Deliberately takes a *list* of projects and has no "just this one" mode: the
    cache is shared workspace-wide, so collecting against an incomplete set is
    how you delete another project's data. See ``data_dvc.collect_garbage``.
    """
    repos: list[Path] = []
    for proj in projects:
        validate_name(proj, kind="project name")
        for wt in sorted((PROJECTS_DIR / proj).glob("*")):
            if wt.is_dir() and data_dvc.is_dvc_repo(wt):
                repos.append(wt)
    return data_dvc.collect_garbage(repos, dry_run=dry_run, keep=keep)


def data_mount(project: str, scope: str, chunks: list[str] | None = None) -> dict:
    """Choose which chunks this scope materialises on disk.

    Mounting is deliberately **not** a property of the commit. Every chunk the
    branch pins stays pinned, hashed and backed up regardless of this setting —
    the list only decides what costs inodes and checkout time *here*. That is
    what lets a figure scope pin a 30 GB cold archive it never reads while a
    sibling working on it has it materialised, from the same commit.

    Passing no chunks clears the list, which means "materialise everything".
    """
    validate_name(project, kind="project name")
    validate_name(scope, kind="scope name")
    worktree = _scope_worktree(project, scope)
    awm_dir = worktree / ".awm"
    if chunks:
        data_dvc.write_mounts(awm_dir, chunks)
    else:
        (awm_dir / data_dvc.MOUNTS_FILE).unlink(missing_ok=True)
    report = data_dvc.provision_scope_data(project, scope, awm_dir)
    return {"project": project, "scope": scope, **report}


def delete_scope(project: str, scope: str, force: bool = False) -> ScopeActionResponse:
    validate_name(project, kind="project name")
    validate_name(scope, kind="scope name")
    bare_dir = PROJECTS_DIR / project / ".bare"
    repo_dir = PROJECTS_DIR / project / scope
    feature_branch = f"feat/{scope}"

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

    try:
        from awm.persistence.embeddings import delete_embedding
        from awm.persistence.databases import get_connection
        conn = get_connection("scopes")
        try:
            delete_embedding(conn, "scope", f"{project}/{scope}")
        finally:
            conn.close()
    except Exception:
        pass

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


def search_scopes(
    query: str | None = None,
    status: str = "active",
    project: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ScopeListResponse:
    """Search scopes. Defaults to status='active'."""
    dao = ScopesDAO()
    sql = (
        "SELECT a.id, a.scope, a.status, a.branch, a.worktree, "
        "       a.created_at, p.name AS project_name, "
        "       (SELECT COUNT(*) FROM agents a2 "
        "        JOIN projects p2 ON p2.id = a2.project_id "
        "        WHERE p2.name = p.name AND a2.scope = a.scope "
        "        AND a2.created_at <= a.created_at) AS session "
        "FROM agents a JOIN projects p ON p.id = a.project_id "
        "WHERE 1=1"
    )
    params: list = []
    if status and status != "all":
        if status == "active":
            sql += " AND a.status IN ('allocated','active')"
        elif status == "completed":
            sql += " AND a.status='retired'"
        elif status == "deleted":
            return ScopeListResponse(scopes=[], total=0)
        else:
            sql += " AND a.status = ?"
            params.append(status)
    if project:
        sql += " AND p.name = ?"
        params.append(project)
    if query:
        sql += " AND a.scope LIKE ?"
        params.append(f"%{query}%")
    sql += " ORDER BY p.name, a.scope LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = dao.query_all(sql, params)

    keyword_hits = [_v37_render_scope(r, session=r["session"] or 1) for r in rows]

    if not query:
        return ScopeListResponse(scopes=keyword_hits, total=len(keyword_hits))

    keyword_keys = {f"{s.project}/{s.scope}" for s in keyword_hits}

    def _materialize(source_id: str):
        if "/" not in source_id:
            return None
        proj, scp = source_id.split("/", 1)
        if project and proj != project:
            return None
        d = ScopesDAO()
        extra_sql = (
            "SELECT a.id, a.scope, a.status, a.branch, a.worktree, "
            "       a.created_at, p.name AS project_name, "
            "       (SELECT COUNT(*) FROM agents a2 "
            "        JOIN projects p2 ON p2.id = a2.project_id "
            "        WHERE p2.name = p.name AND a2.scope = a.scope "
            "        AND a2.created_at <= a.created_at) AS session "
            "FROM agents a JOIN projects p ON p.id = a.project_id "
            "WHERE p.name=? AND a.scope=?"
        )
        extra_params: list = [proj, scp]
        if status and status != "all":
            if status == "active":
                extra_sql += " AND a.status IN ('allocated','active')"
            elif status == "completed":
                extra_sql += " AND a.status='retired'"
            elif status == "deleted":
                return None
            else:
                extra_sql += " AND a.status = ?"
                extra_params.append(status)
        row = d.query_one(extra_sql, extra_params)
        return _v37_render_scope(row, session=row["session"] or 1) if row else None

    try:
        from awm.persistence.embeddings import hybrid_augment
        from awm.persistence.databases import get_connection
        conn = get_connection("scopes")
        try:
            merged = hybrid_augment(
                conn, query,
                source_type="scope",
                keyword_hits=keyword_hits, keyword_keys=keyword_keys,
                materialize=_materialize,
            )
        finally:
            conn.close()
    except Exception:
        merged = keyword_hits
    return ScopeListResponse(scopes=merged, total=len(merged))
