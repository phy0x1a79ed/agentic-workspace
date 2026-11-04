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
    """Every scope exists: no test reaches a gateway."""
    from awm.journal import owners
    owners._known.clear()
    monkeypatch.setattr(owners, "require_scope", lambda project, scope: None)


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


#: The journal schema before tags (v1): posts carry a kind column.
OLD_DDL = """
CREATE TABLE scope_posts (id TEXT PRIMARY KEY, owner_project TEXT NOT NULL, owner_scope TEXT NOT NULL,
    author TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'message', body TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}', ts INTEGER NOT NULL);
CREATE INDEX idx_scope_posts_kind ON scope_posts(owner_project, owner_scope, kind, ts);
CREATE TABLE scope_subscribers (owner_project TEXT NOT NULL, owner_scope TEXT NOT NULL,
    guest_kind TEXT NOT NULL, guest_ref TEXT NOT NULL, display_name TEXT NOT NULL,
    joined_at INTEGER NOT NULL, PRIMARY KEY (owner_project, owner_scope, guest_kind, guest_ref));
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (1);
"""


def insert_post(conn, pid, project, scope, author, tag, body, meta, ts):
    """A raw post row and its one tag, as a seed or an older release wrote it."""
    conn.execute("INSERT INTO scope_posts (id, owner_project, owner_scope, author, body, meta, ts)"
                 " VALUES (?,?,?,?,?,?,?)", (pid, project, scope, author, body, meta, ts))
    conn.execute("INSERT INTO post_tags (post_id, tag_id) VALUES (?, ?)", (pid, f"builtin:{tag}"))
