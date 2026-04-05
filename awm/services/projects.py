"""Project CRUD — ports new-project.sh."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from awm.config import (
    WORKSPACE_ROOT,
    PROJECTS_DIR,
    MAIN_DIR,
    DATA_DIR,
    SKILLS_DIR,
)
from awm.git_utils import run_git as _run, detect_default_branch as _detect_default_branch
from awm.models import ProjectCreateRequest, ProjectCreateResponse


def _find_template(stem: str) -> Path | None:
    """Locate a template file by a stable stem pattern (e.g. `project-agents`).

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


def create_project(req: ProjectCreateRequest) -> ProjectCreateResponse:
    """Create a new project with bare repository, worktree, and data dirs."""
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
            _run(["gh", "repo", "create", f"phy0x1a79ed/{req.name}", "--private"])
            _run(["git", "-C", str(bare_dir), "remote", "add", "origin",
                  f"https://github.com/phy0x1a79ed/{req.name}.git"])

        # Create initial commit via temp clone
        with tempfile.TemporaryDirectory() as tmp:
            init_dir = Path(tmp) / "init"
            _run(["git", "clone", str(bare_dir), str(init_dir)])
            _run(["git", "-C", str(init_dir), "checkout", "-b", "main"])
            _run(["git", "-C", str(init_dir), "commit", "--allow-empty",
                  "-m", f"Initial commit for {req.name}"])
            _run(["git", "-C", str(init_dir), "push", "origin", "main"])

    elif mode == "clone":
        _run(["git", "clone", "--bare", req.clone_url, str(bare_dir)], check=True)
        _run(["git", "-C", str(bare_dir), "config", "remote.origin.fetch",
              "+refs/heads/*:refs/remotes/origin/*"])

    elif mode == "fork":
        if not shutil.which("gh"):
            raise RuntimeError("gh CLI is required for --fork")
        _run(["gh", "repo", "fork", req.fork_url, "--clone=false"])
        repo_name = Path(req.fork_url).stem
        fork_clone_url = f"https://github.com/phy0x1a79ed/{repo_name}.git"
        _run(["git", "clone", "--bare", fork_clone_url, str(bare_dir)], check=True)
        _run(["git", "-C", str(bare_dir), "config", "remote.origin.fetch",
              "+refs/heads/*:refs/remotes/origin/*"])
        _run(["git", "-C", str(bare_dir), "remote", "add", "upstream", req.fork_url])

    # Create supporting directories
    for d in [
        DATA_DIR / req.name / "raw",
        DATA_DIR / req.name / "staged",
        MAIN_DIR / req.name,
    ]:
        d.mkdir(parents=True, exist_ok=True)

    # Create project-level data symlink → data/{project}
    data_link = MAIN_DIR / req.name / "data"
    if not data_link.exists():
        data_link.symlink_to(DATA_DIR / req.name)

    # Detect default branch and create worktree
    default_branch = _detect_default_branch(bare_dir)
    worktree_dir = PROJECTS_DIR / req.name / default_branch

    if not worktree_dir.exists():
        r = _run(["git", "-C", str(bare_dir), "worktree", "add",
                  f"../{default_branch}", default_branch])
        if r.returncode != 0:
            _run(["git", "-C", str(bare_dir), "worktree", "add",
                  f"../{default_branch}", "-b", default_branch])

    # Write project-level AGENTS.md from the project-agents template
    project_agents_template = _find_template("project-agents")
    project_agents_md = MAIN_DIR / req.name / "AGENTS.md"
    if project_agents_template and not project_agents_md.exists():
        content = project_agents_template.read_text().replace("{project}", req.name)
        project_agents_md.write_text(content)

    # Write per-worktree AGENTS.md from the scope-agents template
    scope_agents_template = _find_template("scope-agents")
    if scope_agents_template and worktree_dir.exists():
        content = scope_agents_template.read_text().replace("{project}", req.name)
        (worktree_dir / "AGENTS.md").write_text(content)

    return ProjectCreateResponse(
        name=req.name,
        bare_dir=str(bare_dir),
        worktree_dir=str(worktree_dir),
        data_dir=str(DATA_DIR / req.name),
        results_dir=str(MAIN_DIR / req.name),
        reports_dir=str(MAIN_DIR / req.name),
        mode=mode,
    )
