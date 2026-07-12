"""Shared fixtures for the awm-workspace service tests.

Self-contained: redirects ``awm.persistence.databases.SERVICES_DIR`` (read by
both the DB factory and the unit-path resolver) into a tmp workspace so the
workspace DB and the units tree land under ``tmp_path``, and resets the DAO
``_initialized`` flag between tests so each starts fresh.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def awm_workspace(tmp_path, monkeypatch):
    """Temporary workspace with workspace-service paths redirected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    awm_dir = workspace / ".awm"
    awm_dir.mkdir()
    services_dir = awm_dir / "services"
    services_dir.mkdir()
    tasks_dir = workspace / "tasks"

    monkeypatch.setattr("awm.config.WORKSPACE_ROOT", workspace)
    monkeypatch.setattr("awm.config.AWM_DIR", awm_dir)
    monkeypatch.setattr("awm.config.SERVICES_DIR", services_dir)
    # Units live under TASKS_DIR now (the top-level tasks/ dir); redirect it too.
    monkeypatch.setattr("awm.config.TASKS_DIR", tasks_dir)
    monkeypatch.setattr("awm.persistence.databases.SERVICES_DIR", services_dir)

    import awm.workspace.dao as dao_mod
    monkeypatch.setattr(dao_mod, "_initialized", False)

    return {
        "workspace": workspace,
        "awm_dir": awm_dir,
        "services_dir": services_dir,
        "tasks_dir": tasks_dir,
    }
