"""Redirect the auth DB into a tmp workspace for every test in this dist."""

from __future__ import annotations

import pytest


@pytest.fixture()
def awm_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    services_dir = workspace / ".awm" / "services"
    services_dir.mkdir(parents=True)
    monkeypatch.setattr("awm.config.WORKSPACE_ROOT", workspace, raising=False)
    monkeypatch.setattr("awm.config.AWM_DIR", workspace / ".awm", raising=False)
    monkeypatch.setattr("awm.config.SERVICES_DIR", services_dir, raising=False)
    monkeypatch.setattr("awm.persistence.databases.SERVICES_DIR", services_dir)
    return workspace
