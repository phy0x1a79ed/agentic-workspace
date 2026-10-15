"""Shared fixtures for the awm-dvc service tests.

The only thing that really matters here: ``awm.persistence.databases`` resolves
the DB path from a *module-level* ``SERVICES_DIR`` captured at import, so a test
that forgets to redirect it writes run rows into the live service database and
its single-flight index then blocks the real backup. Redirect both that and
``awm.config.SERVICES_DIR``, and reset the module's ``_initialized`` latch so
each test gets a fresh schema.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def dvc_db(tmp_path, monkeypatch):
    """A throwaway dvc service DB under ``tmp_path``, initialised and empty."""
    services_dir = tmp_path / ".awm" / "services"
    services_dir.mkdir(parents=True)

    monkeypatch.setattr("awm.config.SERVICES_DIR", services_dir, raising=False)
    monkeypatch.setattr("awm.persistence.databases.SERVICES_DIR", services_dir)

    from awm.dvc import runs

    monkeypatch.setattr(runs, "_initialized", False)
    runs.init()
    return runs.RunsDAO()


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
