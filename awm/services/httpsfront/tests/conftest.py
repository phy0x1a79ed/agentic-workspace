"""Shared fixtures for httpsfront tests.

Redirects ``awm.persistence.databases.SERVICES_DIR`` into a tmp workspace so
the service DB lands under ``tmp_path``, and resets ``store``'s
``_initialized`` flag between tests so each starts fresh.
"""

from __future__ import annotations

import pytest


#: The fleet every test runs in: mira and capella hold an edge to altair,
#: shaula is a listed node with none, and nobody is not listed at all.
FLEET = """\
nodes:
  altair:  {role: homebase}
  capella: {role: homebase}
  mira:    {role: laptop}
  shaula:  {role: work}
edges:
  mira:    {altair: lan, capella: lan}
  capella: {altair: lan}
  altair:  {capella: lan}
"""


@pytest.fixture(autouse=True)
def fleet(tmp_path, monkeypatch):
    """A tmp edge list, and this node named ``altair`` unless a test says otherwise."""
    path = tmp_path / "edges.yml"
    path.write_text(FLEET)
    monkeypatch.setenv("AWM_EDGES_FILE", str(path))
    monkeypatch.setenv("AWM_NODE_NAME", "altair")
    return path


@pytest.fixture()
def awm_workspace(tmp_path, monkeypatch):
    """Set up a temporary workspace with httpsfront's DB path redirected."""
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

    import awm.httpsfront.store as store_mod
    monkeypatch.setattr(store_mod, "_initialized", False)

    return {
        "workspace": workspace,
        "awm_dir": awm_dir,
        "services_dir": services_dir,
    }
