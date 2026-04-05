"""Shared fixtures for AWM tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from awm.db import init_db, get_connection


@pytest.fixture()
def awm_workspace(tmp_path, monkeypatch):
    """Set up a temporary workspace with all paths redirected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    awm_dir = workspace / ".awm"
    awm_dir.mkdir()
    db_path = awm_dir / "state.db"
    repos_dir = workspace / "repos"
    repos_dir.mkdir()
    main_dir = workspace / "main"
    main_dir.mkdir()
    skills_dir = workspace / "skills"
    skills_dir.mkdir()
    data_dir = workspace / "data"
    data_dir.mkdir()
    # Legacy dirs (for migration/compat tests)
    tasks_dir = workspace / "tasks"
    tasks_dir.mkdir()
    results_dir = workspace / "results"
    results_dir.mkdir()
    reports_dir = workspace / "reports"
    reports_dir.mkdir()

    # Patch config module constants
    monkeypatch.setattr("awm.config.WORKSPACE_ROOT", workspace)
    monkeypatch.setattr("awm.config.AWM_DIR", awm_dir)
    monkeypatch.setattr("awm.config.DB_PATH", db_path)
    monkeypatch.setattr("awm.config.PID_FILE", awm_dir / "awm.pid")
    monkeypatch.setattr("awm.config.LOG_FILE", awm_dir / "awm.log")
    monkeypatch.setattr("awm.config.REPOS_DIR", repos_dir)
    monkeypatch.setattr("awm.config.MAIN_DIR", main_dir)
    monkeypatch.setattr("awm.config.DATA_DIR", data_dir)
    monkeypatch.setattr("awm.config.SKILLS_DIR", skills_dir)
    # Legacy
    monkeypatch.setattr("awm.config.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("awm.config.RESULTS_DIR", results_dir)
    monkeypatch.setattr("awm.config.REPORTS_DIR", reports_dir)

    # Patch where imported
    monkeypatch.setattr("awm.db.DB_PATH", db_path)
    monkeypatch.setattr("awm.db.AWM_DIR", awm_dir)
    monkeypatch.setattr("awm.services.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("awm.services.sessions.MAIN_DIR", main_dir)
    monkeypatch.setattr("awm.services.scopes.REPOS_DIR", repos_dir)
    monkeypatch.setattr("awm.services.scopes.WORKSPACE_ROOT", workspace)
    monkeypatch.setattr("awm.services.scopes.DATA_DIR", data_dir)
    monkeypatch.setattr("awm.services.scopes.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("awm.services.locks.HEARTBEAT_STALE_THRESHOLD", 120)
    monkeypatch.setattr("awm.services.agents.REPOS_DIR", repos_dir)

    # Server patches
    monkeypatch.setattr("awm.server.WORKSPACE_ROOT", workspace)
    monkeypatch.setattr("awm.server.PID_FILE", awm_dir / "awm.pid")
    monkeypatch.setattr("awm.server.LOG_FILE", awm_dir / "awm.log")
    monkeypatch.setattr("awm.server.IDLE_SHUTDOWN_SECONDS", 0)
    monkeypatch.setattr("awm.server.REAPER_INTERVAL", 9999)

    # Initialize DB
    init_db(db_path)

    return {
        "workspace": workspace,
        "awm_dir": awm_dir,
        "db_path": db_path,
        "repos_dir": repos_dir,
        "main_dir": main_dir,
        "skills_dir": skills_dir,
        "data_dir": data_dir,
        # Legacy
        "tasks_dir": tasks_dir,
        "results_dir": results_dir,
        "reports_dir": reports_dir,
    }


@pytest.fixture()
def db_conn(awm_workspace):
    """Return a DB connection to the temp database."""
    conn = get_connection(awm_workspace["db_path"])
    yield conn
    conn.close()


@pytest.fixture()
def sample_skills_dir(awm_workspace):
    """Create a sample skills directory structure with test files."""
    skills_dir = awm_workspace["skills_dir"]

    # SOPs
    sops_dir = skills_dir / "sops"
    sops_dir.mkdir()
    (sops_dir / "git-workflow.md").write_text(
        "---\nname: Git Workflow\ntype: sop\ntags: [git, workflow]\n"
        "description: Standard git workflow for projects\n---\n\n# Git Workflow\n\nUse feature branches.\n"
    )
    (sops_dir / "testing.md").write_text(
        "---\nname: Testing\ntype: sop\ntags: [testing, quality]\n"
        "description: Testing standards and practices\n---\n\n# Testing\n\nWrite tests first.\n"
    )

    # Tools
    tools_dir = skills_dir / "tools"
    tools_dir.mkdir()
    (tools_dir / "mamba.md").write_text(
        "---\nname: Mamba\ntype: tool\ntags: [conda, environments]\n"
        "description: Mamba package manager guide\n---\n\n# Mamba\n\nUse mamba for speed.\n"
    )

    # No frontmatter file
    (tools_dir / "plain.md").write_text("# Plain Tool\n\nNo frontmatter here.\n")

    # Templates (should be excluded from scan)
    templates_dir = skills_dir / "templates"
    templates_dir.mkdir()
    (templates_dir / "AGENTS.md.template").write_text("# Template\n")

    # Index file (should be excluded from scan)
    (skills_dir / "_index.md").write_text("# Old Index\n")

    return skills_dir


@pytest.fixture()
def seeded_locks(db_conn):
    """Insert a few locks for testing."""
    now = datetime.now(timezone.utc).isoformat()
    import os

    locks_data = [
        ("repos/proj-a/task-1", "agent-1", os.getpid(), "exclusive", now, now, None),
        ("repos/proj-a/task-2", "agent-2", os.getpid(), "shared", now, now, '{"info": "test"}'),
        ("data/shared.csv", "agent-1", os.getpid(), "shared", now, now, None),
    ]
    for l in locks_data:
        db_conn.execute(
            "INSERT INTO locks (resource_path, holder_id, holder_pid, lock_type, acquired_at, heartbeat_at, metadata) VALUES (?,?,?,?,?,?,?)",
            l,
        )
    db_conn.commit()
    return locks_data


@pytest.fixture()
def seeded_tasks(db_conn, awm_workspace):
    """Insert task rows and create matching workspace dirs."""
    now = datetime.now(timezone.utc).isoformat()
    main_dir = awm_workspace["main_dir"]
    repos_dir = awm_workspace["repos_dir"]

    task_data = [
        ("proj-a", "task-1", "active", "feat/task-1", str(main_dir / "proj-a" / "tasks" / "task-1"), str(repos_dir / "proj-a" / "task-1"), 1, now, now),
        ("proj-a", "task-2", "completed", "feat/task-2", str(main_dir / "proj-a" / "tasks" / "task-2"), str(repos_dir / "proj-a" / "task-2"), 1, now, now),
        ("proj-b", "task-3", "completed", "feat/task-3", str(main_dir / "proj-b" / "tasks" / "task-3"), str(repos_dir / "proj-b" / "task-3"), 1, now, now),
    ]
    for t in task_data:
        db_conn.execute(
            "INSERT INTO scopes (project, scope, status, branch, worktree, repo_path, session, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            t,
        )
    db_conn.commit()

    # Create workspace directories (main/)
    for t in task_data:
        ws_path = Path(t[4])
        ws_path.mkdir(parents=True, exist_ok=True)
        (ws_path / "AGENTS.md").write_text(f"# {t[0]}/{t[1]}\n")
        (ws_path / "results").mkdir(exist_ok=True)

    # Create repo directories (repos/) — bare stubs
    for t in task_data:
        repo_path = Path(t[5])
        repo_path.mkdir(parents=True, exist_ok=True)

    # Create .bare directories for filesystem project discovery
    for project_name in ("proj-a", "proj-b"):
        (repos_dir / project_name / ".bare").mkdir(parents=True, exist_ok=True)

    return task_data


@pytest.fixture()
def seeded_sessions(db_conn, seeded_tasks, awm_workspace):
    """Insert session log rows."""
    base = datetime.now(timezone.utc)
    main_dir = awm_workspace["main_dir"]
    t1 = base.isoformat()
    t2 = (base + timedelta(seconds=1)).isoformat()
    t3 = (base + timedelta(seconds=2)).isoformat()
    sessions_data = [
        ("proj-a", "task-1", "", None, t1, "Initial exploration of dataset", "agent-1", None, "Initial exploration of dataset"),
        ("proj-a", "task-1", "", "abc123", t2, "Built feature extraction pipeline", "agent-1", '{"decisions": ["Used pandas"]}', "Built feature extraction pipeline\n\nDecisions:\n- Used pandas"),
        ("proj-a", "task-2", "", None, t3, "Reviewed results and documented findings", "agent-2", None, "Reviewed results and documented findings"),
    ]
    for s in sessions_data:
        db_conn.execute(
            "INSERT INTO session_logs (project, task, file_path, git_commit, logged_at, summary, agent_id, metadata, content) VALUES (?,?,?,?,?,?,?,?,?)",
            s,
        )
    db_conn.commit()
    return sessions_data
