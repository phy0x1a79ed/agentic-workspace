"""Project CRUD — ports new-project.sh."""

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
from awm.git_utils import run_git as _run, detect_default_branch as _detect_default_branch
from awm.models import ProjectCreateRequest, ProjectCreateResponse
from awm.services._validation import validate_name


def _find_template(stem: str) -> Path | None:
    """Locate a template file by a stable stem pattern (e.g. `scope-agents`).

    Templates are resolved by filename substring match against any `*.template`
    file under the workspace skills tree, so renaming or relocating templates
    does not require a code change. First hit wins; the override copy at
    `<workspace>/skills/` takes precedence over the package-bundled `SKILLS_DIR`.
    """
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

        # Try creating GitHub repo
        if shutil.which("gh"):
            _run(["gh", "repo", "create", f"{GITHUB_USER}/{req.name}", "--private"],
                 check=True)
            _run(["git", "-C", str(bare_dir), "remote", "add", "origin",
                  f"https://github.com/{GITHUB_USER}/{req.name}.git"], check=True)

        # Create initial commit via temp clone
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

    # Create supporting directories
    for d in [
        DATA_DIR / req.name / "raw",
        DATA_DIR / req.name / "staged",
    ]:
        d.mkdir(parents=True, exist_ok=True)

    # Detect default branch and create worktree
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

    # Write per-worktree AGENTS.md from the scope-agents template + wire
    # the harness auto-load (CLAUDE.md symlink, @.awm/context.md import
    # if a sibling .awm/context.md is present).
    if worktree_dir.exists():
        from awm.services.scopes import _ensure_harness_context
        _ensure_harness_context(worktree_dir, project_name=req.name)

    return ProjectCreateResponse(
        name=req.name,
        bare_dir=str(bare_dir),
        worktree_dir=str(worktree_dir),
        data_dir=str(DATA_DIR / req.name),
        mode=mode,
    )
