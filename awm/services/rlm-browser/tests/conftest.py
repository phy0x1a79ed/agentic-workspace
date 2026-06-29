"""Shared fixtures for the rlm-browser service tests.

Self-contained: redirects the persistence layer's ``SERVICES_DIR`` into a tmp
workspace so the rlm-browser DB lands under ``tmp_path``, and resets the DAO
``_initialized`` flag between tests so each starts fresh. No prod DB or on-disk
Chrome profile is ever touched.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def awm_workspace(tmp_path, monkeypatch):
    """A temporary workspace with rlm-browser DB paths redirected."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    awm_dir = workspace / ".awm"
    awm_dir.mkdir()
    services_dir = awm_dir / "services"
    services_dir.mkdir()

    monkeypatch.setattr("awm.config.WORKSPACE_ROOT", workspace)
    monkeypatch.setattr("awm.config.AWM_DIR", awm_dir)
    monkeypatch.setattr("awm.config.SERVICES_DIR", services_dir)
    monkeypatch.setattr("awm.persistence.databases.SERVICES_DIR", services_dir)

    # Reset the DAO init flag so init_service_db re-runs against tmp paths.
    import awm.rlm_browser.dao as dao_mod
    monkeypatch.setattr(dao_mod, "_initialized", False)

    return {"workspace": workspace, "awm_dir": awm_dir,
            "services_dir": services_dir}
