"""Task CRUD — ports new-task.sh, complete-task.sh, list-tasks.sh."""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

from awm.config import (
    WORKSPACE_ROOT,
    TASKS_DIR,
    TASKS_ACTIVE_DIR,
    DATA_DIR,
    RESULTS_DIR,
    REPORTS_DIR,
)
from awm.models import (
    TaskCreateRequest,
    TaskUpdateRequest,
    TaskInfo,
    TaskListResponse,
    TaskActionResponse,
)


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _detect_default_branch(bare_dir: Path) -> str:
    for branch in ("main", "master"):
        r = _run(["git", "-C", str(bare_dir), "rev-parse", "--verify", branch])
        if r.returncode == 0:
            return branch
    return "main"


def create_task(req: TaskCreateRequest) -> TaskActionResponse:
    """Create a new task worktree branching from the specified (or default) branch."""
    bare_dir = TASKS_DIR / req.project / ".bare"
    if not bare_dir.exists():
        raise FileNotFoundError(f"Project '{req.project}' not found (expected {bare_dir})")

    from_branch = req.from_branch or _detect_default_branch(bare_dir)
    worktree_dir = TASKS_DIR / req.project / req.task
    feature_branch = f"feat/{req.task}"

    if worktree_dir.exists():
        raise FileExistsError(f"Task worktree already exists at {worktree_dir}")

    # Create worktree with feature branch
    r = _run(["git", "-C", str(bare_dir), "worktree", "add",
              f"../{req.task}", "-b", feature_branch, from_branch])
    if r.returncode != 0:
        raise RuntimeError(f"Failed to create worktree: {r.stderr}")

    # Create results directory
    results_task_dir = RESULTS_DIR / req.project / req.task
    results_task_dir.mkdir(parents=True, exist_ok=True)

    # Create symlinks in the worktree
    (worktree_dir / "data").symlink_to(DATA_DIR)
    (worktree_dir / "results").symlink_to(results_task_dir)
    (worktree_dir / "reports").symlink_to(REPORTS_DIR / req.project)

    # Write .status
    (worktree_dir / ".status").write_text("active\n")

    # Initialize experiences.md
    (worktree_dir / "experiences.md").write_text(
        f"# Experiences -- {req.project}/{req.task}\n\n## Log\n\n"
    )

    # Create env/ directory with overlay
    env_dir = worktree_dir / "env"
    env_dir.mkdir(exist_ok=True)
    (env_dir / "environment.yml").write_text(
        f"# Per-task overlay — install into project env with:\n"
        f"#   mamba env update -n {req.project} --file env/environment.yml\n"
        f"name: {req.project}\n"
        f"channels:\n"
        f"  - conda-forge\n"
        f"  - bioconda\n"
        f"dependencies: []\n"
    )

    # Create symlink in tasks_active
    TASKS_ACTIVE_DIR.mkdir(exist_ok=True)
    link_path = TASKS_ACTIVE_DIR / f"{req.project}_{req.task}"
    link_target = Path("..") / "tasks" / req.project / req.task
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(link_target)

    return TaskActionResponse(
        project=req.project,
        task=req.task,
        status="active",
        message=f"Created task worktree at tasks/{req.project}/{req.task} on branch {feature_branch}",
    )


def update_task(project: str, task: str, req: TaskUpdateRequest) -> TaskActionResponse:
    """Complete, pause, or resume a task."""
    worktree_dir = TASKS_DIR / project / task
    bare_dir = TASKS_DIR / project / ".bare"

    if not worktree_dir.exists():
        raise FileNotFoundError(f"Task worktree not found at {worktree_dir}")
    if not bare_dir.exists():
        raise FileNotFoundError(f"Bare repo not found at {bare_dir}")

    feature_branch = f"feat/{task}"
    active_link = TASKS_ACTIVE_DIR / f"{project}_{task}"

    if req.action == "complete":
        (worktree_dir / ".status").write_text("completed\n")

        # Remove active symlink
        if active_link.exists() or active_link.is_symlink():
            active_link.unlink()

        # Merge if requested
        merge_msg = ""
        if req.merge:
            default_branch = _detect_default_branch(bare_dir)
            main_worktree = TASKS_DIR / project / default_branch
            if not main_worktree.exists():
                raise FileNotFoundError(f"Main worktree not found at {main_worktree}")
            _run(["git", "-C", str(main_worktree), "checkout", default_branch])
            r = _run(["git", "-C", str(main_worktree), "merge", feature_branch,
                      "-m", f"Merge {feature_branch} into {default_branch}"])
            if r.returncode != 0:
                raise RuntimeError(f"Merge failed: {r.stderr}")
            _run(["git", "-C", str(main_worktree), "push"])
            merge_msg = f", merged into {default_branch}"

        # Append completion entry to experiences.md
        exp_file = worktree_dir / "experiences.md"
        if exp_file.exists():
            today = date.today().isoformat()
            entry = (
                f"\n## Completed: {today}\n\n"
                f"- Task marked as completed on {today}\n"
                f"- Branch: {feature_branch}\n"
            )
            if req.merge:
                entry += f"- Merged into {_detect_default_branch(bare_dir)}\n"
            with open(exp_file, "a") as f:
                f.write(entry)

        return TaskActionResponse(
            project=project, task=task, status="completed",
            message=f"Task completed{merge_msg}",
        )

    elif req.action == "pause":
        (worktree_dir / ".status").write_text("paused\n")
        if active_link.exists() or active_link.is_symlink():
            active_link.unlink()
        return TaskActionResponse(
            project=project, task=task, status="paused",
            message="Task paused",
        )

    elif req.action == "resume":
        (worktree_dir / ".status").write_text("active\n")
        TASKS_ACTIVE_DIR.mkdir(exist_ok=True)
        link_target = Path("..") / "tasks" / project / task
        if active_link.exists() or active_link.is_symlink():
            active_link.unlink()
        active_link.symlink_to(link_target)
        return TaskActionResponse(
            project=project, task=task, status="active",
            message="Task resumed",
        )

    else:
        raise ValueError(f"Unknown action: {req.action}")


def list_tasks(status: str | None = None, project: str | None = None) -> TaskListResponse:
    """List tasks, optionally filtered by status and/or project."""
    tasks: list[TaskInfo] = []

    # If filtering for active only, use fast path via tasks_active/
    if status == "active" and TASKS_ACTIVE_DIR.exists():
        for link in sorted(TASKS_ACTIVE_DIR.iterdir()):
            if not link.is_symlink() and not link.is_dir():
                continue
            target = link.resolve()
            if not target.is_dir():
                continue
            task_name = target.name
            project_name = target.parent.name
            if project and project_name != project:
                continue
            branch = "unknown"
            r = _run(["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"])
            if r.returncode == 0:
                branch = r.stdout.strip()
            tasks.append(TaskInfo(
                project=project_name, task=task_name,
                status="active", branch=branch, worktree=str(target),
            ))
        return TaskListResponse(tasks=tasks, total=len(tasks))

    # Full scan
    if not TASKS_DIR.exists():
        return TaskListResponse(tasks=[], total=0)

    for project_dir in sorted(TASKS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        project_name = project_dir.name
        if project and project_name != project:
            continue

        for task_dir in sorted(project_dir.iterdir()):
            if not task_dir.is_dir() or task_dir.name == ".bare":
                continue
            task_name = task_dir.name

            # Read status
            status_file = task_dir / ".status"
            task_status = "unknown"
            if status_file.exists():
                task_status = status_file.read_text().strip()

            if status and status != "all" and task_status != status:
                continue

            branch = "unknown"
            r = _run(["git", "-C", str(task_dir), "rev-parse", "--abbrev-ref", "HEAD"])
            if r.returncode == 0:
                branch = r.stdout.strip()

            tasks.append(TaskInfo(
                project=project_name, task=task_name,
                status=task_status, branch=branch, worktree=str(task_dir),
            ))

    return TaskListResponse(tasks=tasks, total=len(tasks))
