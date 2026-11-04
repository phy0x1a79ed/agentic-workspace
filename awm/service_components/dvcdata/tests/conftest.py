"""Shared fixtures for the awm-dvcdata tests."""

from __future__ import annotations

import pytest


@pytest.fixture()
def dvc_workspace(tmp_path, monkeypatch):
    """A throwaway workspace with every `awm.config` path redirected into it."""
    workspace = tmp_path / "workspace"
    awm_dir = workspace / ".awm"
    projects_dir = workspace / "projects"
    data_dir = workspace / "data"
    for d in (awm_dir, projects_dir, data_dir):
        d.mkdir(parents=True)

    import awm.config as cfg

    monkeypatch.setenv("AWM_WORKSPACE", str(workspace))
    monkeypatch.setattr(cfg, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(cfg, "AWM_DIR", awm_dir)
    monkeypatch.setattr(cfg, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(cfg, "DATA_DIR", data_dir)

    return {
        "workspace": workspace,
        "awm_dir": awm_dir,
        "projects_dir": projects_dir,
        "data_dir": data_dir,
    }
