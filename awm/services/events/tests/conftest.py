"""Shared fixtures for the awm-events service tests.

Self-contained: redirects ``awm.persistence.databases.SERVICES_DIR`` into a tmp
workspace so the events service DB lands under ``tmp_path``, and resets the DAO
``_initialized`` flag between tests so each starts fresh.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def awm_workspace(tmp_path, monkeypatch):
    """Set up a temporary workspace with events DB paths redirected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    awm_dir = workspace / ".awm"
    awm_dir.mkdir()
    services_dir = awm_dir / "services"
    services_dir.mkdir()

    monkeypatch.setattr("awm.config.WORKSPACE_ROOT", workspace, raising=False)
    monkeypatch.setattr("awm.config.AWM_DIR", awm_dir, raising=False)
    monkeypatch.setattr("awm.config.SERVICES_DIR", services_dir, raising=False)
    monkeypatch.setattr("awm.persistence.databases.SERVICES_DIR", services_dir)

    import awm.events.dao as dao_mod
    monkeypatch.setattr(dao_mod, "_initialized", False)

    return {
        "workspace": workspace,
        "awm_dir": awm_dir,
        "services_dir": services_dir,
    }
