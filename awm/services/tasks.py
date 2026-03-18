"""Task CRUD — create, complete, delete, list."""

from __future__ import annotations

import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

from awm.config import (
    WORKSPACE_ROOT,
    REPOS_DIR,
    MAIN_DIR,
    DATA_DIR,
    SKILLS_DIR,
)
from awm.db import get_connection
from awm.git_utils import run_git, detect_default_branch
from awm.models import (
    TaskCreateRequest,
    TaskUpdateRequest,
    TaskInfo,
    TaskListResponse,
    TaskActionResponse,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_workspace_dir(project: str, task: str) -> Path:
    """Return the agent workspace directory for a task."""
    return MAIN_DIR / project / "tasks" / task


def _cleanup_worktree(bare_dir: Path, worktree_dir: Path, feature_branch: str) -> None:
    """Remove a task's worktree and branch, ignoring errors."""
    # Remove worktree via git
    r = run_git(["git", "-C", str(bare_dir), "worktree", "remove", str(worktree_dir), "--force"])
    if r.returncode != 0 and worktree_dir.exists():
        # Fallback: force-remove the directory
        shutil.rmtree(worktree_dir, ignore_errors=True)

    # Prune worktree list
    run_git(["git", "-C", str(bare_dir), "worktree", "prune"])

    # Delete the feature branch (may already be merged/deleted)
    run_git(["git", "-C", str(bare_dir), "branch", "-D", feature_branch])


def create_task(req: TaskCreateRequest) -> TaskActionResponse:
    """Create a new task worktree + agent workspace."""
    bare_dir = REPOS_DIR / req.project / ".bare"
    if not bare_dir.exists():
        raise FileNotFoundError(f"Project '{req.project}' not found (expected {bare_dir})")

    from_branch = req.from_branch or detect_default_branch(bare_dir)
    repo_dir = REPOS_DIR / req.project / req.task
    workspace_dir = _get_workspace_dir(req.project, req.task)
    feature_branch = f"feat/{req.task}"

    # Check for existing sessions
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT MAX(session) as max_session FROM tasks WHERE project=? AND task=?",
            (req.project, req.task),
        ).fetchone()
        session_num = (row["max_session"] or 0) + 1 if row and row["max_session"] else 1

        # Check no active session exists
        active = conn.execute(
            "SELECT id FROM tasks WHERE project=? AND task=? AND status='active'",
            (req.project, req.task),
        ).fetchone()
        if active:
            raise FileExistsError(
                f"Task '{req.task}' already has an active session in project '{req.project}'"
            )
    finally:
        conn.close()

    # Clean up old worktree if it exists from a previous session
    if repo_dir.exists():
        _cleanup_worktree(bare_dir, repo_dir, feature_branch)

    # 1. Create git worktree at repos/{project}/{task}/
    r = run_git(["git", "-C", str(bare_dir), "worktree", "add",
              f"../{req.task}", "-b", feature_branch, from_branch])
    if r.returncode != 0:
        raise RuntimeError(f"Failed to create worktree: {r.stderr}")

    # 2. Create agent workspace at main/{project}/{task}/
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # 3. Create symlinks: repo, skills (re-create if stale from previous session)
    repo_link = workspace_dir / "repo"
    if repo_link.is_symlink() or repo_link.exists():
        repo_link.unlink()
    repo_link.symlink_to(repo_dir)
    skills_link = workspace_dir / "skills"
    if skills_link.is_symlink() or skills_link.exists():
        skills_link.unlink()
    skills_link.symlink_to(SKILLS_DIR)

    # 4. Create results/ and inbox/ directories
    (workspace_dir / "results").mkdir(exist_ok=True)
    (workspace_dir / "inbox").mkdir(exist_ok=True)

    # 5. Write AGENTS.md from context or default task agent persona
    if req.context:
        agents_content = req.context
    else:
        agents_content = (
            f"# {req.project}/{req.task} — Task Agent\n\n"
            f"@../../../../AGENTS.md\n\n"
            f"## Role\n\n"
            f"You are the **task agent** for `{req.project}/{req.task}`. Execute the plan from your inbox.\n\n"
            f"## Startup Ritual\n\n"
            f"1. Check inbox: `inbox_search scope=task:{req.project}/{req.task}`\n"
            f"2. Read and acknowledge the plan message: `inbox_read id=N`\n"
            f"3. Review this file and `experiences.md` for prior context\n\n"
            f"## Work\n\n"
            f"- Code changes go in `repo/` (symlink to git worktree)\n"
            f"- Results go in `results/`\n"
            f"- Data is at `../../data` (project-level symlink)\n\n"
            f"## Completion\n\n"
            f"1. Send a reflection: `inbox_send scope=project:{req.project} sender=task:{req.project}/{req.task} msg_type=reflection subject=\"Task complete\" body=\"...\"`\n"
            f"2. Log session: `session_log project={req.project} task={req.task} --summary \"...\"`\n"
            f"3. Complete: `task_complete project={req.project} task={req.task}`\n\n"
            f"Workspace SOPs and tool guides are in `skills/`.\n"
        )
    (workspace_dir / "AGENTS.md").write_text(agents_content)

    # 6. Initialize experiences.md
    (workspace_dir / "experiences.md").write_text(
        f"# Experiences -- {req.project}/{req.task}\n\n## Log\n\n"
    )

    # 7. Record in DB (worktree → main/ workspace, repo_path → git worktree)
    now = _now_iso()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO tasks (project, task, status, branch, worktree, repo_path, session, created_at, updated_at) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)",
            (req.project, req.task, feature_branch, str(workspace_dir), str(repo_dir), session_num, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    return TaskActionResponse(
        project=req.project,
        task=req.task,
        status="active",
        session=session_num,
        message=f"Created task at repos/{req.project}/{req.task} (workspace: main/{req.project}/tasks/{req.task}) on branch {feature_branch}, session {session_num}",
    )


def update_task(project: str, task: str, req: TaskUpdateRequest) -> TaskActionResponse:
    """Complete a task."""
    bare_dir = REPOS_DIR / project / ".bare"
    repo_dir = REPOS_DIR / project / task
    workspace_dir = _get_workspace_dir(project, task)

    if not bare_dir.exists():
        raise FileNotFoundError(f"Bare repo not found at {bare_dir}")

    feature_branch = f"feat/{task}"
    now = _now_iso()

    if req.action != "complete":
        raise ValueError(f"Unknown action: {req.action}. Only 'complete' is supported.")

    # Update DB
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE tasks SET status='completed', updated_at=? WHERE project=? AND task=? AND status='active'",
            (now, project, task),
        )
        conn.commit()
    finally:
        conn.close()

    # Merge if requested
    merge_msg = ""
    if req.merge:
        default_branch = detect_default_branch(bare_dir)
        main_worktree = REPOS_DIR / project / default_branch
        if not main_worktree.exists():
            raise FileNotFoundError(f"Main worktree not found at {main_worktree}")
        run_git(["git", "-C", str(main_worktree), "checkout", default_branch])
        r = run_git(["git", "-C", str(main_worktree), "merge", feature_branch,
                  "-m", f"Merge {feature_branch} into {default_branch}"])
        if r.returncode != 0:
            raise RuntimeError(f"Merge failed: {r.stderr}")
        run_git(["git", "-C", str(main_worktree), "push"])
        merge_msg = f", merged into {default_branch}"

    # Append completion entry to experiences.md in workspace
    exp_file = workspace_dir / "experiences.md"
    if exp_file.exists():
        today = date.today().isoformat()
        entry = (
            f"\n## Completed: {today}\n\n"
            f"- Task marked as completed on {today}\n"
            f"- Branch: {feature_branch}\n"
        )
        if req.merge:
            entry += f"- Merged into {detect_default_branch(bare_dir)}\n"
        with open(exp_file, "a") as f:
            f.write(entry)

    # Cleanup worktree/branch if requested
    if req.cleanup:
        _cleanup_worktree(bare_dir, repo_dir, feature_branch)

    return TaskActionResponse(
        project=project, task=task, status="completed",
        message=f"Task completed{merge_msg}",
    )


def delete_task(project: str, task: str) -> TaskActionResponse:
    """Delete a task — clean up worktree, branch, and mark as deleted in DB."""
    bare_dir = REPOS_DIR / project / ".bare"
    repo_dir = REPOS_DIR / project / task
    workspace_dir = _get_workspace_dir(project, task)
    feature_branch = f"feat/{task}"

    if not bare_dir.exists():
        raise FileNotFoundError(f"Bare repo not found at {bare_dir}")

    # Find the task in DB (active or completed)
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, status FROM tasks WHERE project=? AND task=? AND status IN ('active', 'completed') ORDER BY updated_at DESC LIMIT 1",
            (project, task),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"No active or completed task '{task}' found in project '{project}'")

        # Clean up git resources
        _cleanup_worktree(bare_dir, repo_dir, feature_branch)

        # Clean up workspace
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir, ignore_errors=True)

        # Mark as deleted
        now = _now_iso()
        conn.execute(
            "UPDATE tasks SET status='deleted', updated_at=? WHERE id=?",
            (now, row["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    return TaskActionResponse(
        project=project, task=task, status="deleted",
        message=f"Task deleted, worktree and branch cleaned up",
    )


def list_tasks(status: str | None = None, project: str | None = None) -> TaskListResponse:
    """List tasks, optionally filtered by status and/or project."""
    conn = get_connection()
    try:
        query = "SELECT project, task, status, branch, worktree, repo_path, session FROM tasks WHERE 1=1"
        params: list[str] = []
        if status and status != "all":
            query += " AND status = ?"
            params.append(status)
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY project, task"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    tasks = [
        TaskInfo(
            project=r["project"], task=r["task"], status=r["status"],
            branch=r["branch"], worktree=r["worktree"],
            repo_path=r["repo_path"], session=r["session"],
        )
        for r in rows
    ]
    return TaskListResponse(tasks=tasks, total=len(tasks))
