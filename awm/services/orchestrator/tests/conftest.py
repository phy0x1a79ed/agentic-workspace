"""Shared fixtures for the awm-orchestrator service tests.

Self-contained and **offline**: redirects ``SERVICES_DIR`` into a tmp workspace
so the orchestrator DB lands under ``tmp_path``, resets the DAO ``_initialized``
flag, and puts :mod:`dispatch` into synchronous test mode with a stub placement
seam — so the full plan → dispatch → flip flow runs with no event loop and the
process never reaches a live gateway (and never prod ``:7819``).
"""

from __future__ import annotations

import types

import pytest


@pytest.fixture()
def orch(tmp_path, monkeypatch):
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

    from awm.orchestrator import dao, dispatch, kernel, operations
    from awm.orchestrator.dao import OrchestratorDAO

    monkeypatch.setattr(dao, "_initialized", False)
    dao.init()

    # Synchronous dispatch with a deterministic stub for the agents seam.
    placements: dict[str, dict] = {}

    def stub_place(payload: dict) -> dict:
        placements[payload["task_id"]] = payload
        slug = payload["scope_slug"]
        return {
            "agent_ref": f"agent:{slug}",
            "scope": slug,
            "placement_token": f"tok-{payload['task_id'][:6]}",
        }

    dispatch.reset()
    dispatch.configure(place_fn=stub_place, sync=True)
    operations.configure(project_exists_fn=lambda project: True)

    yield types.SimpleNamespace(
        dao=dao, dispatch=dispatch, kernel=kernel, operations=operations,
        DAO=OrchestratorDAO, placements=placements,
        manifest=None, handlers=None,
    )

    dispatch.reset()
