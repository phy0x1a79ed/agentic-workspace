"""Shared fixtures for the awm-gateway dist tests.

The gateway is de-DB'd — it owns no tables (per-service DBs live in each
feature dist). So this conftest does NOT init any database; it only
redirects the ``awm.config`` path constants into a tmp workspace so the
service journal, access log, etc. land under ``tmp_path`` instead of the
real ``.awm/``.

Provides an ``awm_workspace`` fixture with the same dict shape the legacy
monolith fixture returned, so ported hub/supervisor tests keep compiling
against ``awm_workspace["awm_dir"]`` / ``["workspace"]`` without edits.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def awm_workspace(tmp_path, monkeypatch):
    """Redirect gateway path constants into a tmp workspace (no DB)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    awm_dir = workspace / ".awm"
    awm_dir.mkdir()
    projects_dir = workspace / "projects"
    projects_dir.mkdir()
    skills_dir = workspace / "skills"
    skills_dir.mkdir()
    data_dir = workspace / "data"
    data_dir.mkdir()

    import awm.config as cfg
    monkeypatch.setattr(cfg, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(cfg, "AWM_DIR", awm_dir)
    monkeypatch.setattr(cfg, "PID_FILE", awm_dir / "awm.pid")
    monkeypatch.setattr(cfg, "LOG_FILE", awm_dir / "awm.log")
    monkeypatch.setattr(cfg, "PROJECTS_DIR", projects_dir)
    monkeypatch.setattr(cfg, "DATA_DIR", data_dir)
    monkeypatch.setattr(cfg, "SKILLS_DIR", skills_dir)
    if hasattr(cfg, "ACCESS_LOG"):
        monkeypatch.setattr(cfg, "ACCESS_LOG", awm_dir / "access.log")

    # The service supervisor reads AWM_DIR from awm.config at call time
    # via its own import; patch there too if it took a module-level copy.
    import awm.gateway.hub.supervisor as sup
    for attr in ("AWM_DIR",):
        if hasattr(sup, attr):
            monkeypatch.setattr(sup, attr, awm_dir)

    # Server module may hold copies of the lifecycle paths.
    try:
        import awm.gateway.server as srv
        for attr, val in [
            ("WORKSPACE_ROOT", workspace),
            ("PID_FILE", awm_dir / "awm.pid"),
            ("LOG_FILE", awm_dir / "awm.log"),
            ("IDLE_SHUTDOWN_SECONDS", 0),
        ]:
            if hasattr(srv, attr):
                monkeypatch.setattr(srv, attr, val)
    except Exception:
        pass

    return {
        "workspace": workspace,
        "awm_dir": awm_dir,
        "projects_dir": projects_dir,
        "skills_dir": skills_dir,
        "data_dir": data_dir,
    }
