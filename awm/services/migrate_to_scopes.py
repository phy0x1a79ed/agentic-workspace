"""One-time migration: move workspace metadata from main/ to .awm/ in worktrees."""

from __future__ import annotations

import shutil
from pathlib import Path

from awm.config import WORKSPACE_ROOT, REPOS_DIR, MAIN_DIR, DATA_DIR, SKILLS_DIR
from awm.db import get_connection


def migrate_workspaces(dry_run: bool = True) -> dict:
    """Migrate task workspaces from main/{project}/tasks/{scope}/ to .awm/ in git worktrees.

    Only migrates AGENTS.md → context.md, experiences.md, and creates symlinks.
    Extra files (scripts, .venv, etc.) are left in main/ for manual review.

    Returns stats dict.
    """
    stats = {
        "scopes_processed": 0,
        "awm_dirs_created": 0,
        "context_migrated": 0,
        "experiences_migrated": 0,
        "symlinks_created": 0,
        "db_rows_updated": 0,
        "skipped_no_worktree": [],
        "extra_files": {},  # scope -> list of non-standard files
    }

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, project, scope, worktree, repo_path FROM scopes WHERE status IN ('active', 'completed')"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        project = row["project"]
        scope = row["scope"]
        stats["scopes_processed"] += 1

        # Source: old workspace in main/
        old_workspace = MAIN_DIR / project / "tasks" / scope
        # Target: .awm/ in the git worktree
        repo_dir = REPOS_DIR / project / scope
        awm_dir = repo_dir / ".awm"

        if not repo_dir.exists():
            stats["skipped_no_worktree"].append(f"{project}/{scope}")
            continue

        if not dry_run:
            awm_dir.mkdir(parents=True, exist_ok=True)
            stats["awm_dirs_created"] += 1

            # Create symlinks
            data_link = awm_dir / "data"
            if not data_link.exists():
                data_target = DATA_DIR / project
                data_target.mkdir(parents=True, exist_ok=True)
                data_link.symlink_to(data_target)
                stats["symlinks_created"] += 1

            skills_link = awm_dir / "skills"
            if not skills_link.exists():
                skills_link.symlink_to(SKILLS_DIR)
                stats["symlinks_created"] += 1
        else:
            stats["awm_dirs_created"] += 1
            stats["symlinks_created"] += 2

        # Migrate AGENTS.md → context.md
        if old_workspace.exists():
            agents_md = old_workspace / "AGENTS.md"
            if agents_md.exists():
                if not dry_run:
                    content = agents_md.read_text()
                    (awm_dir / "context.md").write_text(content)
                stats["context_migrated"] += 1

            # Migrate experiences.md
            exp_md = old_workspace / "experiences.md"
            if exp_md.exists():
                if not dry_run:
                    content = exp_md.read_text()
                    (awm_dir / "experiences.md").write_text(content)
                stats["experiences_migrated"] += 1

            # Catalog extra files (not standard workspace files)
            standard = {"AGENTS.md", "experiences.md", "repo", "skills", "results",
                        "inbox", ".claude", ".awm", "__pycache__"}
            extras = []
            for child in old_workspace.iterdir():
                if child.name not in standard and not child.name.startswith("."):
                    extras.append(child.name)
            if extras:
                stats["extra_files"][f"{project}/{scope}"] = extras

        # Update DB worktree to point to repo dir
        if not dry_run:
            conn2 = get_connection()
            try:
                conn2.execute(
                    "UPDATE scopes SET worktree=?, repo_path=? WHERE id=?",
                    (str(repo_dir), str(repo_dir), row["id"]),
                )
                conn2.commit()
                stats["db_rows_updated"] += 1
            finally:
                conn2.close()
        else:
            stats["db_rows_updated"] += 1

    # Migrate DATA_INDEX.md files
    for project_dir in MAIN_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        data_index = project_dir / "DATA_INDEX.md"
        if data_index.exists():
            target = DATA_DIR / project_dir.name / "INDEX.md"
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(data_index, target)
            print(f"  DATA_INDEX.md → {target}")

    return stats


if __name__ == "__main__":
    import json
    print("=== DRY RUN ===")
    result = migrate_workspaces(dry_run=True)
    print(json.dumps(result, indent=2))
    print()

    if result["extra_files"]:
        print("Scopes with extra files (need manual review):")
        for scope, files in result["extra_files"].items():
            print(f"  {scope}: {', '.join(files)}")
