#!/usr/bin/env python3
"""One-off migration: move task dirs into tasks/ subdirectory and set up project-level structure.

Old layout:  main/{project}/{task}/
New layout:  main/{project}/tasks/{task}/

Also creates project-level data symlinks and inbox directories.

Usage:
    1. Stop any running agents/server: awm stop
    2. Run: python migrate_layout.py
    3. Restart server — DB migration v6 runs automatically via init_db
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Resolve workspace root
WORKSPACE_ROOT = Path(os.environ.get("AWM_WORKSPACE", Path(__file__).resolve().parent))
MAIN_DIR = WORKSPACE_ROOT / "main"
DATA_DIR = WORKSPACE_ROOT / "data"
REPOS_DIR = WORKSPACE_ROOT / "repos"
SKILLS_DIR = WORKSPACE_ROOT / "awm" / "skills"

# Directories at project level that are NOT task dirs
PROJECT_LEVEL_DIRS = {"data", "inbox", "tasks"}


def migrate():
    if not MAIN_DIR.exists():
        print("No main/ directory found — nothing to migrate.")
        return

    projects = [d for d in MAIN_DIR.iterdir() if d.is_dir()]
    if not projects:
        print("No projects found in main/ — nothing to migrate.")
        return

    moved = []
    for project_dir in sorted(projects):
        project = project_dir.name
        tasks_dir = project_dir / "tasks"
        inbox_dir = project_dir / "inbox"
        data_link = project_dir / "data"

        # 1. Create tasks/ and inbox/ directories
        tasks_dir.mkdir(exist_ok=True)
        inbox_dir.mkdir(exist_ok=True)

        # 2. Create project-level data symlink
        if data_link.is_symlink():
            # Remove old symlink (was pointing to all of data/)
            data_link.unlink()
        if not data_link.exists():
            target = DATA_DIR / project
            target.mkdir(parents=True, exist_ok=True)
            data_link.symlink_to(target)
            print(f"  Created data symlink: {data_link} -> {target}")

        # 3. Move task directories into tasks/
        for item in sorted(project_dir.iterdir()):
            if item.name in PROJECT_LEVEL_DIRS:
                continue
            if not item.is_dir():
                continue
            # This is a task directory — move it
            dest = tasks_dir / item.name
            if dest.exists():
                print(f"  SKIP {project}/{item.name} — already exists at tasks/{item.name}")
                continue

            shutil.move(str(item), str(dest))
            moved.append(f"{project}/{item.name}")
            print(f"  Moved {project}/{item.name} -> {project}/tasks/{item.name}")

            # 4. Remove stale task-level data symlink
            task_data = dest / "data"
            if task_data.is_symlink():
                task_data.unlink()
                print(f"    Removed stale data symlink from tasks/{item.name}")

            # 5. Fix repo symlink (extra depth: ../../../../repos/...)
            repo_link = dest / "repo"
            if repo_link.is_symlink():
                repo_link.unlink()
            repo_target = REPOS_DIR / project / item.name
            if repo_target.exists():
                repo_link.symlink_to(repo_target)
                print(f"    Fixed repo symlink -> {repo_target}")

            # 6. Fix skills symlink
            skills_link = dest / "skills"
            if skills_link.is_symlink():
                skills_link.unlink()
            skills_link.symlink_to(SKILLS_DIR)
            print(f"    Fixed skills symlink -> {SKILLS_DIR}")

    print(f"\nMigration complete. Moved {len(moved)} task(s).")
    if moved:
        print("Tasks moved:")
        for m in moved:
            print(f"  - {m}")
    print("\nNext steps:")
    print("  1. Start the server — DB migration v6 will run automatically")
    print("  2. Verify with: awm task list")


if __name__ == "__main__":
    migrate()
