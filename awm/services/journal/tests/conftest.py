"""Shared fixtures for the journal service tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def stub_embedder():
    """Index and search with a deterministic stub, never the real model."""
    from awm.persistence import embeddings
    from awm.persistence.search_testing import StubEmbedder
    emb = StubEmbedder()
    embeddings.use_embedder(emb)
    yield emb
    embeddings.use_embedder(None)


@pytest.fixture(autouse=True)
def no_scopes_service(monkeypatch):
    """Every scope exists and none has siblings: no test reaches a gateway."""
    from awm.journal import owners
    owners._known.clear()
    monkeypatch.setattr(owners, "require_scope", lambda project, scope: None)
    monkeypatch.setattr(owners, "scope_siblings", lambda project, scope: [])


@pytest.fixture()
def journal_workspace(tmp_path, monkeypatch):
    """A temporary workspace with the journal's database and the projects dir redirected."""
    workspace = tmp_path / "workspace"
    services_dir = workspace / ".awm" / "services"
    projects_dir = workspace / "projects"
    services_dir.mkdir(parents=True)
    projects_dir.mkdir()

    import awm.config as cfg
    monkeypatch.setattr(cfg, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(cfg, "AWM_DIR", workspace / ".awm")
    monkeypatch.setattr(cfg, "PROJECTS_DIR", projects_dir)

    import awm.persistence.databases as pdbs
    monkeypatch.setattr(pdbs, "SERVICES_DIR", services_dir)

    import awm.journal.dao as journal_dao
    monkeypatch.setattr(journal_dao, "_initialized", False)
    journal_dao.init()

    yield {"workspace": workspace, "services_dir": services_dir, "projects_dir": projects_dir}
    # Queued index writes resolve the DB path when they run: drain them while
    # it still points at this test's workspace.
    from awm.journal import search_index
    search_index.flush()


@pytest.fixture()
def journal_conn(journal_workspace):
    """A live connection to the journal DB for seeding rows."""
    from awm.persistence.databases import get_connection
    conn = get_connection("journal")
    yield conn
    conn.close()
