"""Project CRUD — ports new-project.sh (v1 modular).

All SQL goes through ScopesDAO. Embeddings indexing is reimplemented
against the scopes service's own embeddings table.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from awm.config import (
    WORKSPACE_ROOT,
    PROJECTS_DIR,
    DATA_DIR,
    SKILLS_DIR,
    GITHUB_USER,
    VAGRANT_PROJECT,
)
from awm.scopes.git_utils import run_git as _run, detect_default_branch as _detect_default_branch
from awm.scopes.dao import ScopesDAO
from awm.scopes.models import (
    ProjectCreateRequest, ProjectCreateResponse,
    ProjectListInfo, ProjectListResponse, ProjectScopeCounts,
)
from awm.scopes._validation import validate_name


def _find_template(stem: str) -> Path | None:
    roots = []
    workspace_skills = WORKSPACE_ROOT / "skills"
    if workspace_skills.exists():
        roots.append(workspace_skills)
    if SKILLS_DIR.exists() and SKILLS_DIR not in roots:
        roots.append(SKILLS_DIR)
    for root in roots:
        for path in sorted(root.rglob("*.template")):
            if stem in path.name:
                return path
    return None


def _branch_exists(bare_dir: Path, branch: str) -> bool:
    r = _run(["git", "-C", str(bare_dir), "rev-parse", "--verify",
              f"refs/heads/{branch}"])
    return r.returncode == 0


def _index_project(name: str) -> None:
    """Upsert a project embedding in the scopes DB. Silently no-ops on failure."""
    try:
        from awm.persistence.embeddings import upsert_embedding
        from awm.persistence.databases import get_connection
        text = name
        conn = get_connection("scopes")
        try:
            upsert_embedding(conn, "project", name, text)
        finally:
            conn.close()
    except Exception:
        pass


def create_project(req: ProjectCreateRequest) -> ProjectCreateResponse:
    """Create a new project with bare repository, worktree, and data dirs."""
    validate_name(req.name, kind="project name")
    if req.name == VAGRANT_PROJECT:
        raise ValueError(
            f"project name {req.name!r} is reserved for vagrant scopes; "
            f"use `awm vagrant-init` to bootstrap the unified vagrant repo"
        )

    bare_dir = PROJECTS_DIR / req.name / ".bare"

    if bare_dir.exists():
        raise FileExistsError(f"Project '{req.name}' already exists at {bare_dir}")

    mode = "fresh"
    if req.clone_url:
        mode = "clone"
    elif req.fork_url:
        mode = "fork"

    if mode == "fresh":
        _run(["git", "init", "--bare", str(bare_dir)], check=True)

        if shutil.which("gh"):
            _run(["gh", "repo", "create", f"{GITHUB_USER}/{req.name}", "--private"],
                 check=True)
            _run(["git", "-C", str(bare_dir), "remote", "add", "origin",
                  f"https://github.com/{GITHUB_USER}/{req.name}.git"], check=True)

        with tempfile.TemporaryDirectory() as tmp:
            init_dir = Path(tmp) / "init"
            _run(["git", "clone", str(bare_dir), str(init_dir)])
            _run(["git", "-C", str(init_dir), "checkout", "-b", "main"])
            _run(["git", "-C", str(init_dir), "commit", "--allow-empty",
                  "-m", f"Initial commit for {req.name}"])
            _run(["git", "-C", str(init_dir), "push", "origin", "main"], check=True)

    elif mode == "clone":
        _run(["git", "clone", "--bare", req.clone_url, str(bare_dir)], check=True)
        _run(["git", "-C", str(bare_dir), "config", "remote.origin.fetch",
              "+refs/heads/*:refs/remotes/origin/*"])

    elif mode == "fork":
        if not shutil.which("gh"):
            raise RuntimeError("gh CLI is required for --fork")
        _run(["gh", "repo", "fork", req.fork_url, "--clone=false"])
        repo_name = Path(req.fork_url).stem
        fork_clone_url = f"https://github.com/{GITHUB_USER}/{repo_name}.git"
        _run(["git", "clone", "--bare", fork_clone_url, str(bare_dir)], check=True)
        _run(["git", "-C", str(bare_dir), "config", "remote.origin.fetch",
              "+refs/heads/*:refs/remotes/origin/*"])
        _run(["git", "-C", str(bare_dir), "remote", "add", "upstream", req.fork_url])

    for d in [
        DATA_DIR / req.name / "raw",
        DATA_DIR / req.name / "staged",
    ]:
        d.mkdir(parents=True, exist_ok=True)

    default_branch = _detect_default_branch(bare_dir)
    worktree_dir = PROJECTS_DIR / req.name / default_branch

    if not worktree_dir.exists():
        if _branch_exists(bare_dir, default_branch):
            r = _run(["git", "-C", str(bare_dir), "worktree", "add",
                      f"../{default_branch}", default_branch])
        else:
            r = _run(["git", "-C", str(bare_dir), "worktree", "add",
                      f"../{default_branch}", "-b", default_branch])
        if r.returncode != 0:
            raise RuntimeError(
                f"Failed to create worktree for {default_branch}: {r.stderr}"
            )

    _index_project(req.name)

    return ProjectCreateResponse(
        name=req.name,
        bare_dir=str(bare_dir),
        worktree_dir=str(worktree_dir),
        data_dir=str(DATA_DIR / req.name),
        mode=mode,
    )


def search_projects(
    query: str | None = None,
    active_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> ProjectListResponse:
    """Search projects from the agents table + on-disk project dirs."""
    dao = ScopesDAO()
    rows = dao.query_all(
        "SELECT p.name AS project, a.status, COUNT(*) AS n "
        "FROM agents a JOIN projects p ON p.id = a.project_id "
        "GROUP BY p.name, a.status ORDER BY p.name"
    )

    by_project: dict[str, ProjectScopeCounts] = {}
    for r in rows:
        counts = by_project.setdefault(r["project"], ProjectScopeCounts())
        if r["status"] in ("allocated", "active"):
            counts.active += r["n"]
        elif r["status"] == "retired":
            counts.completed += r["n"]

    if PROJECTS_DIR.is_dir():
        for child in sorted(PROJECTS_DIR.iterdir()):
            if child.is_dir() and (child / ".bare").is_dir():
                by_project.setdefault(child.name, ProjectScopeCounts())

    items = [
        ProjectListInfo(name=n, scope_counts=c)
        for n, c in sorted(by_project.items())
    ]

    if active_only:
        items = [p for p in items if p.scope_counts.active > 0]
    if query:
        q = query.lower()
        items = [p for p in items if q in p.name.lower()]

    paged = items[offset:offset + limit]

    if not query:
        return ProjectListResponse(projects=paged)

    keyword_keys = {p.name for p in paged}

    def _materialize(name: str):
        if active_only and by_project.get(name, ProjectScopeCounts()).active <= 0:
            return None
        if name not in by_project:
            return None
        return ProjectListInfo(name=name, scope_counts=by_project[name])

    try:
        from awm.persistence.embeddings import hybrid_augment
        from awm.persistence.databases import get_connection
        conn = get_connection("scopes")
        try:
            merged = hybrid_augment(
                conn, query,
                source_type="project",
                keyword_hits=paged, keyword_keys=keyword_keys,
                materialize=_materialize,
            )
        finally:
            conn.close()
    except Exception:
        merged = paged
    return ProjectListResponse(projects=merged)
