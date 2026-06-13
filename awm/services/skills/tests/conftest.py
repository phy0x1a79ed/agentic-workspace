"""Shared fixtures for the awm-skills service tests.

This conftest is self-contained: it does NOT depend on the monolithic
``awm.db`` / ``awm.services.*`` imports. All paths and module attributes are
patched against ``awm.skills.*`` and ``awm.config``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from awm.persistence.databases import get_connection, init_service_db
from awm.persistence.embeddings import EMBEDDINGS_DDL
from awm.skills.dao import SERVICE as SKILLS_SERVICE


@pytest.fixture()
def awm_workspace(tmp_path, monkeypatch):
    """Set up a temporary workspace with all AWM paths redirected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    awm_dir = workspace / ".awm"
    awm_dir.mkdir()
    services_dir = awm_dir / "services"
    services_dir.mkdir()
    db_path = awm_dir / "state.db"
    projects_dir = workspace / "projects"
    projects_dir.mkdir()
    skills_dir = workspace / "skills"
    skills_dir.mkdir()
    data_dir = workspace / "data"
    data_dir.mkdir()

    # Redirect awm.config paths so service_db_path writes to tmp
    monkeypatch.setattr("awm.config.WORKSPACE_ROOT", workspace)
    monkeypatch.setattr("awm.config.AWM_DIR", awm_dir)
    monkeypatch.setattr("awm.config.DB_PATH", db_path)
    monkeypatch.setattr("awm.config.SERVICES_DIR", services_dir)
    monkeypatch.setattr("awm.config.SKILLS_DIR", skills_dir)
    # Redirect the persistence layer's copy of SERVICES_DIR
    monkeypatch.setattr("awm.persistence.databases.SERVICES_DIR", services_dir)
    # Redirect where skills.py reads SKILLS_DIR from
    monkeypatch.setattr("awm.skills.skills.SKILLS_DIR", skills_dir)

    return {
        "workspace": workspace,
        "awm_dir": awm_dir,
        "db_path": db_path,
        "services_dir": services_dir,
        "projects_dir": projects_dir,
        "skills_dir": skills_dir,
        "data_dir": data_dir,
    }


@pytest.fixture(autouse=True)
def _reset_service_init_flags(awm_workspace):
    """Reset per-service _initialized flags before each test.

    Both the skills DAO and the config service cache an _initialized flag to
    avoid re-running init_service_db. That flag must be cleared between tests
    so each test starts with the fresh tmp workspace paths in effect.
    """
    import awm.skills.dao as dao_mod
    import awm.persistence.config_service as cfg_mod
    dao_mod._initialized = False
    cfg_mod._initialized = False
    yield
    dao_mod._initialized = False
    cfg_mod._initialized = False


@pytest.fixture()
def skills_db_conn(awm_workspace):
    """Return a raw sqlite3 connection to the skills service DB (created on demand)."""
    import awm.skills.dao as dao_mod
    dao_mod._initialized = False
    dao_mod.init()
    conn = get_connection(SKILLS_SERVICE)
    yield conn
    conn.close()
    dao_mod._initialized = False


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
