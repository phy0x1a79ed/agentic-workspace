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
    projects_dir = workspace / "projects"
    projects_dir.mkdir()
    main_dir = workspace / "main"
    main_dir.mkdir()
    skills_dir = workspace / "skills"
    skills_dir.mkdir()
    data_dir = workspace / "data"
    data_dir.mkdir()

    # Patch config module constants
    monkeypatch.setattr("awm.config.WORKSPACE_ROOT", workspace)
    monkeypatch.setattr("awm.config.AWM_DIR", awm_dir)
    monkeypatch.setattr("awm.config.DB_PATH", db_path)
    monkeypatch.setattr("awm.config.PID_FILE", awm_dir / "awm.pid")
    monkeypatch.setattr("awm.config.LOG_FILE", awm_dir / "awm.log")
    monkeypatch.setattr("awm.config.PROJECTS_DIR", projects_dir)
    monkeypatch.setattr("awm.config.MAIN_DIR", main_dir)
    monkeypatch.setattr("awm.config.DATA_DIR", data_dir)
    monkeypatch.setattr("awm.config.SKILLS_DIR", skills_dir)

    # Patch where imported
    monkeypatch.setattr("awm.db.DB_PATH", db_path)
    monkeypatch.setattr("awm.db.AWM_DIR", awm_dir)
    monkeypatch.setattr("awm.services.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("awm.services.sessions.MAIN_DIR", main_dir)
    monkeypatch.setattr("awm.services.sessions.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("awm.services.scopes.PROJECTS_DIR", projects_dir)
    monkeypatch.setattr("awm.services.scopes.WORKSPACE_ROOT", workspace)
    monkeypatch.setattr("awm.services.scopes.DATA_DIR", data_dir)
    monkeypatch.setattr("awm.services.scopes.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("awm.services.artifacts.WORKSPACE_ROOT", workspace)
    monkeypatch.setattr("awm.services.locks.HEARTBEAT_STALE_THRESHOLD", 120)
    monkeypatch.setattr("awm.services.agents.PROJECTS_DIR", projects_dir)

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
        "projects_dir": projects_dir,
        "main_dir": main_dir,
        "skills_dir": skills_dir,
        "data_dir": data_dir,
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

    # AWM skills
    awm_dir = skills_dir / "awm"
    awm_dir.mkdir()
    (awm_dir / "create-scope.md").write_text(
        "---\nname: create-scope\ntype: awm\ntags: [scope, workflow]\n"
        "description: Create and complete a scope\n---\n\n# Create Scope\n\nUse feature branches.\n"
    )
    (awm_dir / "debrief.md").write_text(
        "---\nname: debrief\ntype: awm\ntags: [session, debrief]\n"
        "description: End-of-session debrief\n---\n\n# Debrief\n\nLog the session.\n"
    )

    # Tools
    tools_dir = skills_dir / "tools"
    tools_dir.mkdir()
    (tools_dir / "git.md").write_text(
        "---\nname: git\ntype: tool\ntags: [git, workflow]\n"
        "description: Git bare-repo + worktree reference\n---\n\n# Git\n\nUse feature branches.\n"
    )
    (tools_dir / "mamba.md").write_text(
        "---\nname: Mamba\ntype: tool\ntags: [conda, environments]\n"
        "description: Mamba package manager guide\n---\n\n# Mamba\n\nUse mamba for speed.\n"
    )

    # No frontmatter file
    (tools_dir / "plain.md").write_text("# Plain Tool\n\nNo frontmatter here.\n")

    # Templates (should be excluded from scan)
    templates_dir = skills_dir / "templates"
    templates_dir.mkdir()
    (templates_dir / "scope-agents.md.template").write_text("# Template\n")

    # Index file (should be excluded from scan)
    (skills_dir / "_index.md").write_text("# Old Index\n")

    return skills_dir


@pytest.fixture()
def seeded_locks(db_conn):
    """Insert a few locks for testing."""
    now = datetime.now(timezone.utc).isoformat()
    import os

    locks_data = [
        ("projects/proj-a/scope-1", "agent-1", os.getpid(), "exclusive", now, now, None),
        ("projects/proj-a/scope-2", "agent-2", os.getpid(), "shared", now, now, '{"info": "test"}'),
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
def seeded_scopes(db_conn, awm_workspace):
    """Insert scope rows and create matching workspace dirs."""
    now = datetime.now(timezone.utc).isoformat()
    main_dir = awm_workspace["main_dir"]
    projects_dir = awm_workspace["projects_dir"]

    scope_data = [
        ("proj-a", "scope-1", "active", "feat/scope-1", str(main_dir / "proj-a" / "scopes" / "scope-1"), str(projects_dir / "proj-a" / "scope-1"), 1, now, now),
        ("proj-a", "scope-2", "completed", "feat/scope-2", str(main_dir / "proj-a" / "scopes" / "scope-2"), str(projects_dir / "proj-a" / "scope-2"), 1, now, now),
        ("proj-b", "scope-3", "completed", "feat/scope-3", str(main_dir / "proj-b" / "scopes" / "scope-3"), str(projects_dir / "proj-b" / "scope-3"), 1, now, now),
    ]
    for t in scope_data:
        db_conn.execute(
            "INSERT INTO scopes (project, scope, status, branch, worktree, repo_path, session, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            t,
        )
    db_conn.commit()

    # Create workspace directories (main/)
    for t in scope_data:
        ws_path = Path(t[4])
        ws_path.mkdir(parents=True, exist_ok=True)
        (ws_path / "AGENTS.md").write_text(f"# {t[0]}/{t[1]}\n")
        (ws_path / "results").mkdir(exist_ok=True)

    # Create project directories (projects/) — bare stubs
    for t in scope_data:
        repo_path = Path(t[5])
        repo_path.mkdir(parents=True, exist_ok=True)

    # Create .bare directories for filesystem project discovery
    for project_name in ("proj-a", "proj-b"):
        (projects_dir / project_name / ".bare").mkdir(parents=True, exist_ok=True)

    return scope_data


@pytest.fixture()
def seeded_sessions(db_conn, seeded_scopes, awm_workspace):
    """Insert session log rows."""
    base = datetime.now(timezone.utc)
    main_dir = awm_workspace["main_dir"]
    t1 = base.isoformat()
    t2 = (base + timedelta(seconds=1)).isoformat()
    t3 = (base + timedelta(seconds=2)).isoformat()
    sessions_data = [
        ("proj-a", "scope-1", "", None, t1, "Initial exploration of dataset", "agent-1", None, "Initial exploration of dataset"),
        ("proj-a", "scope-1", "", "abc123", t2, "Built feature extraction pipeline", "agent-1", '{"decisions": ["Used pandas"]}', "Built feature extraction pipeline\n\nDecisions:\n- Used pandas"),
        ("proj-a", "scope-2", "", None, t3, "Reviewed results and documented findings", "agent-2", None, "Reviewed results and documented findings"),
    ]
    for s in sessions_data:
        db_conn.execute(
            "INSERT INTO session_logs (project, scope, file_path, git_commit, logged_at, summary, agent_id, metadata, content) VALUES (?,?,?,?,?,?,?,?,?)",
            s,
        )
    db_conn.commit()
    return sessions_data
